package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"html/template"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type server struct {
	cfg      config
	store    *store
	webhooks *dispatcher
	metrics  *metrics
	log      *slog.Logger
	rand     func() float64 // injectable so tests are deterministic
}

func newServer(cfg config, st *store, webhooks *dispatcher, m *metrics, log *slog.Logger) *server {
	return &server{cfg: cfg, store: st, webhooks: webhooks, metrics: m, log: log, rand: rand.Float64}
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /metrics", s.metrics.serveHTTP)
	mux.Handle("POST /v1/payment-intents", s.requireAPIKey(http.HandlerFunc(s.createIntent)))
	mux.Handle("GET /v1/payment-intents/{id}", s.requireAPIKey(http.HandlerFunc(s.getIntent)))
	mux.HandleFunc("GET /checkout/{id}", s.checkoutPage)
	mux.HandleFunc("POST /checkout/{id}", s.completeCheckout)
	return s.logRequests(mux)
}

type createIntentRequest struct {
	MerchantRef string `json:"merchant_ref"`
	Amount      int64  `json:"amount"`
	Currency    string `json:"currency"`
	Description string `json:"description"`
}

func (s *server) createIntent(w http.ResponseWriter, r *http.Request) {
	if err := s.simulateLatency(r.Context()); err != nil {
		return // the client gave up (timeout): nothing to answer
	}
	if s.rand() < s.cfg.FailureRate {
		s.metrics.injectedFailures.Add(1)
		writeError(w, http.StatusServiceUnavailable, "temporarily_unavailable", "injected failure, retry later")
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header is required")
		return
	}

	var req createIntentRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	req.Currency = strings.ToUpper(req.Currency)
	if req.MerchantRef == "" || req.Amount <= 0 || len(req.Currency) != 3 {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request",
			"merchant_ref, a positive amount and a 3-letter currency are required")
		return
	}

	in, created := s.store.create(key, func(id string) intent {
		return intent{
			ID:          id,
			Status:      statusRequiresPayment,
			Amount:      req.Amount,
			Currency:    req.Currency,
			MerchantRef: req.MerchantRef,
			Description: req.Description,
			CheckoutURL: s.cfg.PublicURL + "/checkout/" + id,
			CreatedAt:   time.Now().UTC(),
		}
	})
	if !created {
		if in.MerchantRef != req.MerchantRef || in.Amount != req.Amount || in.Currency != req.Currency {
			writeError(w, http.StatusUnprocessableEntity, "idempotency_key_reused",
				"Idempotency-Key was already used with different parameters")
			return
		}
		w.Header().Set("Idempotent-Replayed", "true")
		writeJSON(w, http.StatusOK, in)
		return
	}
	s.metrics.intentsCreated.Add(1)
	writeJSON(w, http.StatusCreated, in)
}

func (s *server) getIntent(w http.ResponseWriter, r *http.Request) {
	in, ok := s.store.get(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", errNotFound.Error())
		return
	}
	writeJSON(w, http.StatusOK, in)
}

var checkoutPage = template.Must(template.New("checkout").Parse(`<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PayMock checkout</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;color:#1f2933}
.card{border:1px solid #d9e2ec;border-radius:12px;padding:24px}
.amount{font-size:28px;font-weight:700;margin:8px 0 16px}
button{width:100%;padding:12px;margin-top:8px;border:0;border-radius:8px;font-size:16px;cursor:pointer}
.ok{background:#0f766e;color:#fff}.fail{background:#e5e7eb;color:#111827}
</style></head>
<body><div class="card">
<p>PayMock · cổng thanh toán giả lập</p>
<div class="amount">{{.Amount}}</div>
<p>{{.Description}}</p>
{{if .Pending}}
<form method="post"><input type="hidden" name="result" value="succeeded"><button class="ok">Thanh toán thành công</button></form>
<form method="post"><input type="hidden" name="result" value="failed"><button class="fail">Giả lập thanh toán thất bại</button></form>
{{else}}<p>Giao dịch đã kết thúc: <b>{{.Status}}</b>. Bạn có thể đóng trang này.</p>{{end}}
</div></body></html>`))

func (s *server) checkoutPage(w http.ResponseWriter, r *http.Request) {
	in, ok := s.store.get(r.PathValue("id"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_ = checkoutPage.Execute(w, map[string]any{
		"Amount":      formatMoney(in.Amount, in.Currency),
		"Description": in.Description,
		"Pending":     in.Status == statusRequiresPayment,
		"Status":      in.Status,
	})
}

// completeCheckout plays the customer paying (or failing to pay) on the gateway's page.
func (s *server) completeCheckout(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	result := r.FormValue("result")
	if result != statusSucceeded && result != statusFailed {
		writeError(w, http.StatusBadRequest, "invalid_result", "result must be succeeded or failed")
		return
	}
	in, err := s.store.complete(id, result == statusSucceeded)
	switch {
	case errors.Is(err, errNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error())
		return
	case errors.Is(err, errAlreadyFinal):
		writeError(w, http.StatusConflict, "already_completed", err.Error())
		return
	}
	s.metrics.checkoutsCompleted.Add(1)
	s.webhooks.dispatch(webhookFor(in))

	if strings.Contains(r.Header.Get("Accept"), "application/json") {
		writeJSON(w, http.StatusOK, in)
		return
	}
	http.Redirect(w, r, "/checkout/"+id, http.StatusSeeOther) // post/redirect/get
}

func (s *server) requireAPIKey(next http.Handler) http.Handler {
	expected := []byte("Bearer " + s.cfg.APIKey)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Constant-time comparison: response timing must not reveal how much of the key matched.
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), expected) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid_api_key", "missing or invalid API key")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *server) simulateLatency(ctx context.Context) error {
	if s.cfg.MaxLatency <= 0 {
		return nil
	}
	delay := s.cfg.MinLatency + time.Duration(s.rand()*float64(s.cfg.MaxLatency-s.cfg.MinLatency))
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (s *server) logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" || r.URL.Path == "/metrics" {
			next.ServeHTTP(w, r)
			return
		}
		start := time.Now()
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(recorder, r)
		s.log.Info("http_request", "method", r.Method, "path", r.URL.Path,
			"status", recorder.status, "duration_ms", time.Since(start).Milliseconds())
	})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]string{"error": code, "message": message})
}

// formatMoney renders 2500000 VND as "2.500.000 VND" (Vietnamese thousands separator).
func formatMoney(amount int64, currency string) string {
	digits := strconv.FormatInt(amount, 10)
	var out strings.Builder
	for i, d := range digits {
		if i > 0 && (len(digits)-i)%3 == 0 {
			out.WriteByte('.')
		}
		out.WriteRune(d)
	}
	return out.String() + " " + currency
}
