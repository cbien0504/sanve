package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strconv"
	"sync"
	"time"
)

const signatureHeader = "X-PayMock-Signature"

type webhookEvent struct {
	ID      string      `json:"id"`
	Type    string      `json:"type"`
	Created int64       `json:"created"`
	Data    webhookData `json:"data"`
}

type webhookData struct {
	PaymentIntentID string `json:"payment_intent_id"`
	MerchantRef     string `json:"merchant_ref"`
	Amount          int64  `json:"amount"`
	Currency        string `json:"currency"`
}

func webhookFor(in intent) webhookEvent {
	eventType := "payment.succeeded"
	if in.Status == statusFailed {
		eventType = "payment.failed"
	}
	return webhookEvent{
		ID:      "evt_" + randomHex(12),
		Type:    eventType,
		Created: time.Now().Unix(),
		Data: webhookData{
			PaymentIntentID: in.ID,
			MerchantRef:     in.MerchantRef,
			Amount:          in.Amount,
			Currency:        in.Currency,
		},
	}
}

// sign uses the scheme the SanVe API verifies: t=<unix>,v1=hex(hmac_sha256(secret, "<t>.<body>")).
// Signing the timestamp lets the receiver reject replays of old requests.
func sign(secret string, timestamp int64, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(strconv.FormatInt(timestamp, 10) + "."))
	mac.Write(body)
	return fmt.Sprintf("t=%d,v1=%s", timestamp, hex.EncodeToString(mac.Sum(nil)))
}

// dispatcher delivers webhooks in the background with exponential backoff, like real providers.
type dispatcher struct {
	ctx         context.Context
	url         string
	secret      string
	duplicate   bool
	client      *http.Client
	maxAttempts int
	baseDelay   time.Duration
	metrics     *metrics
	log         *slog.Logger
	wg          sync.WaitGroup
}

func newDispatcher(ctx context.Context, url, secret string, duplicate bool, m *metrics, log *slog.Logger) *dispatcher {
	return &dispatcher{
		ctx:         ctx,
		url:         url,
		secret:      secret,
		duplicate:   duplicate,
		client:      &http.Client{Timeout: 5 * time.Second},
		maxAttempts: 6,
		baseDelay:   500 * time.Millisecond,
		metrics:     m,
		log:         log,
	}
}

func (d *dispatcher) dispatch(event webhookEvent) {
	body, err := json.Marshal(event)
	if err != nil {
		d.log.Error("cannot encode webhook", "error", err)
		return
	}
	copies := 1
	if d.duplicate {
		copies = 2 // the same event id twice: receivers must be idempotent
	}
	for range copies {
		d.wg.Add(1)
		go func() {
			defer d.wg.Done()
			d.deliver(event.ID, body)
		}()
	}
}

func (d *dispatcher) deliver(eventID string, body []byte) {
	for attempt := 1; ; attempt++ {
		err := d.send(body)
		if err == nil {
			d.metrics.webhooksDelivered.Add(1)
			return
		}
		if attempt == d.maxAttempts {
			d.metrics.webhooksFailed.Add(1)
			d.log.Error("webhook delivery abandoned", "event_id", eventID, "attempts", attempt, "error", err)
			return
		}
		delay := d.baseDelay << (attempt - 1)
		delay += time.Duration(rand.Int64N(int64(delay)/2 + 1)) // jitter
		d.log.Warn("webhook delivery failed, retrying", "event_id", eventID, "attempt", attempt,
			"error", err, "retry_in", delay.String())
		select {
		case <-time.After(delay):
		case <-d.ctx.Done():
			return
		}
	}
}

func (d *dispatcher) send(body []byte) error {
	req, err := http.NewRequestWithContext(d.ctx, http.MethodPost, d.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(signatureHeader, sign(d.secret, time.Now().Unix(), body))
	resp, err := d.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("merchant answered %d", resp.StatusCode)
	}
	return nil
}

func (d *dispatcher) wait() {
	d.wg.Wait()
}
