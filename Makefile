.DEFAULT_GOAL := help
COMPOSE := docker compose
BUYERS ?= 500
SEATS ?= 100

.PHONY: help up down clean logs ps test test-api test-go lint demo flash-sale k8s-render k8s-validate

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.env:
	cp .env.example .env

up: .env ## Build and start the whole stack
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps the database volume)
	$(COMPOSE) down

clean: ## Stop the stack and delete its data
	$(COMPOSE) down -v

logs: ## Follow application logs
	$(COMPOSE) logs -f api worker scheduler payment-gateway

ps: ## Show container status
	$(COMPOSE) ps

test: test-api test-go ## Run every test suite

test-api: .env ## Python unit + integration tests (real PostgreSQL and Redis)
	$(COMPOSE) --profile test run --build --rm tests pytest -q --cov=app --cov-report=term-missing:skip-covered

test-go: ## Go tests of the payment gateway (race detector on)
	docker run --rm -v $(CURDIR)/services/payment-gateway:/src -w /src golang:1.25 go test -race ./...

lint: .env ## ruff + gofmt + go vet
	$(COMPOSE) --profile test run --build --rm tests sh -c "ruff check . && ruff format --check ."
	docker run --rm -v $(CURDIR)/services/payment-gateway:/src -w /src golang:1.25-alpine sh -c 'test -z "$$(gofmt -l .)" && go vet ./...'

demo: ## Walk through the whole purchase flow against the running stack
	uv run scripts/demo.py

flash-sale: ## Load test: BUYERS concurrent buyers race for SEATS seats
	$(COMPOSE) exec -T api python -m app.cli seed-buyers --count $(BUYERS) > .flash-sale-tokens.jsonl
	uv run scripts/flash_sale.py --tokens .flash-sale-tokens.jsonl --seats $(SEATS)

k8s-render: ## Render the Kubernetes manifests of the local overlay
	kubectl kustomize deploy/k8s/overlays/local

k8s-validate: ## Validate rendered manifests against the Kubernetes schemas
	kubectl kustomize deploy/k8s/overlays/local | docker run --rm -i ghcr.io/yannh/kubeconform:v0.7.0 -strict -summary -
