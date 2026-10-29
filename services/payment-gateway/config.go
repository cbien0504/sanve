package main

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

type config struct {
	Addr              string
	APIKey            string
	WebhookURL        string
	WebhookSecret     string
	PublicURL         string
	FailureRate       float64
	MinLatency        time.Duration
	MaxLatency        time.Duration
	DuplicateWebhooks bool
}

func loadConfig(getenv func(string) string) (config, error) {
	cfg := config{
		Addr:          withDefault(getenv("ADDR"), ":8090"),
		APIKey:        getenv("API_KEY"),
		WebhookURL:    getenv("WEBHOOK_URL"),
		WebhookSecret: getenv("WEBHOOK_SECRET"),
		PublicURL:     strings.TrimRight(withDefault(getenv("PUBLIC_URL"), "http://localhost:8090"), "/"),
	}
	if cfg.APIKey == "" || cfg.WebhookURL == "" || cfg.WebhookSecret == "" {
		return cfg, errors.New("API_KEY, WEBHOOK_URL and WEBHOOK_SECRET are required")
	}

	var err error
	cfg.FailureRate, err = strconv.ParseFloat(withDefault(getenv("FAILURE_RATE"), "0"), 64)
	if err != nil || cfg.FailureRate < 0 || cfg.FailureRate > 1 {
		return cfg, errors.New("FAILURE_RATE must be a number between 0 and 1")
	}
	minMs, err := strconv.Atoi(withDefault(getenv("MIN_LATENCY_MS"), "20"))
	if err != nil || minMs < 0 {
		return cfg, errors.New("MIN_LATENCY_MS must be a non-negative integer")
	}
	maxMs, err := strconv.Atoi(withDefault(getenv("MAX_LATENCY_MS"), "300"))
	if err != nil || maxMs < minMs {
		return cfg, fmt.Errorf("MAX_LATENCY_MS must be an integer >= MIN_LATENCY_MS (%d)", minMs)
	}
	cfg.MinLatency = time.Duration(minMs) * time.Millisecond
	cfg.MaxLatency = time.Duration(maxMs) * time.Millisecond
	cfg.DuplicateWebhooks, err = strconv.ParseBool(withDefault(getenv("DUPLICATE_WEBHOOKS"), "false"))
	if err != nil {
		return cfg, errors.New("DUPLICATE_WEBHOOKS must be true or false")
	}
	return cfg, nil
}

func withDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
