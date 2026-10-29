package main

import (
	"fmt"
	"net/http"
	"sync/atomic"
)

// metrics exposes a handful of counters in the Prometheus text format, without extra dependencies.
type metrics struct {
	intentsCreated     atomic.Int64
	injectedFailures   atomic.Int64
	checkoutsCompleted atomic.Int64
	webhooksDelivered  atomic.Int64
	webhooksFailed     atomic.Int64
}

func (m *metrics) serveHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	counters := []struct {
		name, help string
		value      int64
	}{
		{"paymock_intents_created_total", "Payment intents created", m.intentsCreated.Load()},
		{"paymock_injected_failures_total", "Requests answered 503 on purpose", m.injectedFailures.Load()},
		{"paymock_checkouts_completed_total", "Checkouts paid or failed", m.checkoutsCompleted.Load()},
		{"paymock_webhooks_delivered_total", "Webhook deliveries acknowledged with 2xx", m.webhooksDelivered.Load()},
		{"paymock_webhooks_failed_total", "Webhooks abandoned after all retries", m.webhooksFailed.Load()},
	}
	for _, c := range counters {
		fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s counter\n%s %d\n", c.name, c.help, c.name, c.name, c.value)
	}
}
