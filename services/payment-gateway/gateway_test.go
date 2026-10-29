package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testAPIKey = "test-key"

func newTestGateway(t *testing.T, webhookURL string) (*server, *httptest.Server) {
	t.Helper()
	cfg := config{APIKey: testAPIKey, WebhookURL: webhookURL, WebhookSecret: "whsec_test", PublicURL: "http://pay.test"}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	m := &metrics{}
	d := newDispatcher(context.Background(), cfg.WebhookURL, cfg.WebhookSecret, false, m, logger)
	d.baseDelay = time.Millisecond
	s := newServer(cfg, newStore(), d, m, logger)
	s.rand = func() float64 { return 0.5 }
	ts := httptest.NewServer(s.routes())
	t.Cleanup(ts.Close)
	return s, ts
}

func createIntent(t *testing.T, ts *httptest.Server, key string, amount int64) (*http.Response, intent) {
	t.Helper()
	body := `{"merchant_ref":"order-1","amount":` + strconv.FormatInt(amount, 10) + `,"currency":"VND","description":"test"}`
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/v1/payment-intents", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+testAPIKey)
	req.Header.Set("Idempotency-Key", key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var in intent
	_ = json.NewDecoder(resp.Body).Decode(&in)
	return resp, in
}

// The same vector is asserted in services/api/tests/unit/test_signatures.py: signer and verifier
// live in different languages and must agree byte for byte.
func TestSignatureMatchesSharedVector(t *testing.T) {
	got := sign("whsec_test_vector", 1700000000, []byte(`{"id":"evt_test","type":"payment.succeeded"}`))
	want := "t=1700000000,v1=a8b1d1360fbc7664c5d95b259b4f3657daa0012dd7c8fbd903ac9fa192486916"
	if got != want {
		t.Fatalf("signature mismatch\n got: %s\nwant: %s", got, want)
	}
}

func TestCreateIntentRequiresAPIKey(t *testing.T) {
	_, ts := newTestGateway(t, "http://unused")
	resp, err := http.Post(ts.URL+"/v1/payment-intents", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", resp.StatusCode)
	}
}

func TestCreateIntentIsIdempotent(t *testing.T) {
	_, ts := newTestGateway(t, "http://unused")

	first, created := createIntent(t, ts, "order-1", 500000)
	if first.StatusCode != http.StatusCreated {
		t.Fatalf("first status = %d, want 201", first.StatusCode)
	}
	second, replayed := createIntent(t, ts, "order-1", 500000)
	if second.StatusCode != http.StatusOK || second.Header.Get("Idempotent-Replayed") != "true" {
		t.Fatalf("retry status = %d, replayed header = %q", second.StatusCode, second.Header.Get("Idempotent-Replayed"))
	}
	if replayed.ID != created.ID {
		t.Fatalf("retry created a second intent: %s != %s", replayed.ID, created.ID)
	}
	conflict, _ := createIntent(t, ts, "order-1", 999)
	if conflict.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("reused key with other amount: status = %d, want 422", conflict.StatusCode)
	}
}

func TestInjectedFailuresReturn503(t *testing.T) {
	s, ts := newTestGateway(t, "http://unused")
	s.cfg.FailureRate = 1
	resp, _ := createIntent(t, ts, "order-1", 500000)
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", resp.StatusCode)
	}
}

func TestCheckoutSendsSignedWebhookAndRetries(t *testing.T) {
	var calls atomic.Int32
	received := make(chan webhookEvent, 1)
	merchant := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if calls.Add(1) == 1 {
			w.WriteHeader(http.StatusInternalServerError) // first delivery fails: must be retried
			return
		}
		header := r.Header.Get(signatureHeader)
		ts, _ := strconv.ParseInt(strings.TrimPrefix(strings.Split(header, ",")[0], "t="), 10, 64)
		if header != sign("whsec_test", ts, body) {
			t.Errorf("invalid signature %q", header)
		}
		var event webhookEvent
		_ = json.Unmarshal(body, &event)
		received <- event
	}))
	defer merchant.Close()

	_, ts := newTestGateway(t, merchant.URL)
	_, in := createIntent(t, ts, "order-1", 500000)

	form := url.Values{"result": {"succeeded"}}
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/checkout/"+in.ID, strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("checkout status = %d, want 200", resp.StatusCode)
	}

	select {
	case event := <-received:
		if event.Type != "payment.succeeded" || event.Data.PaymentIntentID != in.ID || event.Data.Amount != 500000 {
			t.Fatalf("unexpected webhook %+v", event)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("webhook was not delivered")
	}
	if calls.Load() != 2 {
		t.Fatalf("deliveries = %d, want 2 (one failure + one retry)", calls.Load())
	}

	again, err := http.Post(ts.URL+"/checkout/"+in.ID, "application/x-www-form-urlencoded", strings.NewReader(form.Encode()))
	if err != nil {
		t.Fatal(err)
	}
	again.Body.Close()
	if again.StatusCode != http.StatusConflict {
		t.Fatalf("second checkout status = %d, want 409", again.StatusCode)
	}
}

func TestFormatMoney(t *testing.T) {
	for amount, want := range map[int64]string{5: "5 VND", 1000: "1.000 VND", 2500000: "2.500.000 VND"} {
		if got := formatMoney(amount, "VND"); got != want {
			t.Errorf("formatMoney(%d) = %q, want %q", amount, got, want)
		}
	}
}
