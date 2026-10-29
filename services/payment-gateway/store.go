package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"sync"
	"time"
)

const (
	statusRequiresPayment = "requires_payment"
	statusSucceeded       = "succeeded"
	statusFailed          = "failed"
)

var (
	errNotFound     = errors.New("payment intent not found")
	errAlreadyFinal = errors.New("payment intent is already completed")
)

type intent struct {
	ID          string    `json:"id"`
	Status      string    `json:"status"`
	Amount      int64     `json:"amount"`
	Currency    string    `json:"currency"`
	MerchantRef string    `json:"merchant_ref"`
	Description string    `json:"description"`
	CheckoutURL string    `json:"checkout_url"`
	CreatedAt   time.Time `json:"created_at"`
}

// store keeps intents in memory: good enough for a mock, gone on restart.
type store struct {
	mu        sync.Mutex
	intents   map[string]*intent
	byIdemKey map[string]string
}

func newStore() *store {
	return &store{intents: map[string]*intent{}, byIdemKey: map[string]string{}}
}

// create stores a new intent, or returns the one already created with the same idempotency key
// (created == false). Check-and-insert happens under one lock, so concurrent retries cannot race.
func (s *store) create(idempotencyKey string, build func(id string) intent) (in intent, created bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id, ok := s.byIdemKey[idempotencyKey]; ok {
		return *s.intents[id], false
	}
	id := "pi_" + randomHex(12)
	fresh := build(id)
	s.intents[id] = &fresh
	s.byIdemKey[idempotencyKey] = id
	return fresh, true
}

func (s *store) get(id string) (intent, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in, ok := s.intents[id]
	if !ok {
		return intent{}, false
	}
	return *in, true
}

func (s *store) complete(id string, succeeded bool) (intent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in, ok := s.intents[id]
	if !ok {
		return intent{}, errNotFound
	}
	if in.Status != statusRequiresPayment {
		return *in, errAlreadyFinal
	}
	in.Status = statusFailed
	if succeeded {
		in.Status = statusSucceeded
	}
	return *in, nil
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b) // crypto/rand never fails on supported platforms
	return hex.EncodeToString(b)
}
