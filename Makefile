# Common workflows for the voice-assessment agent.
# Run `make help` to list targets.

COMPOSE      := docker compose
PROD_COMPOSE := docker compose -f docker-compose.prod.yml

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---- local development (browser / WebRTC) ----
.PHONY: init
init: ## First-time setup: create local config + env from templates
	@[ -f app/config.py ] || cp app/config.example.py app/config.py
	@[ -f .env ] || cp .env.example .env
	@echo "Created app/config.py and .env (if missing). Set GROQ_API_KEY in .env, then: make up seed"

.PHONY: up
up: ## Start the local stack (postgres + api + agent)
	$(COMPOSE) up -d

.PHONY: build
build: ## Rebuild images and start
	$(COMPOSE) up -d --build

.PHONY: down
down: ## Stop the local stack
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail api logs (debug 500s here)
	$(COMPOSE) logs -f api

.PHONY: agent-logs
agent-logs: ## Tail agent (voice) logs
	$(COMPOSE) logs -f agent

.PHONY: seed
seed: ## Seed assessment question banks + RAG knowledge base
	$(COMPOSE) exec api python -m scripts.seed_questions
	$(COMPOSE) exec api python -m scripts.ingest_kb

.PHONY: test
test: ## Run the full test suite in the api container
	$(COMPOSE) exec api pytest -q

.PHONY: shell
shell: ## Open a shell in the api container
	$(COMPOSE) exec api bash

# ---- observability ----
.PHONY: traces
traces: ## Print a LangSmith trace summary (tokens / errors / latency)
	$(COMPOSE) exec api python -m scripts.trace_summary

# ---- production (Twilio phone on the OCI box) ----
# Deployment is host-specific — see RUNBOOK.local.md and use the `deployer` agent.
.PHONY: prod-up
prod-up: ## Bring up the production stack (run on the OCI box)
	$(PROD_COMPOSE) up -d --build

.PHONY: prod-ps
prod-ps: ## Production container status (run on the OCI box)
	$(PROD_COMPOSE) ps

.PHONY: prod-logs
prod-logs: ## Tail production agent logs (run on the OCI box)
	$(PROD_COMPOSE) logs -f agent
