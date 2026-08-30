# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Use the project subagents

This repo defines dedicated subagents in `.claude/agents/`. Prefer them for their domain:

- **python-dev** — implement features, bug fixes, and refactors here (endpoints, services, agent
  tools, RAG, tests). Enforces the architecture invariants + professional Python standards.
- **code-reviewer** — review and tune changed code (correctness, simplicity, types, best practice).
- **sec-audit** — security audit (leaked secrets, auth gaps, injection, exposed surfaces) before
  pushing or deploying.
- **deployer** — deploy to the OCI production box (Twilio phone) and verify.

Host-specific and sensitive operational details (box IP, SSH, key rotation, troubleshooting)
live in the gitignored **`RUNBOOK.local.md`**. Common commands are in the **`Makefile`**
(`make help`).

## What this is

Zero-paid POC of an inbound interview voice agent. A candidate who missed an outbound
interview call (no-answer / voicemail / disconnected) calls back; the agent identifies them,
recovers interview state from Postgres, and takes the correct action: START / RESUME /
RESCHEDULE / ESCALATE. Full design rationale is in
`inbound_interview_agent_zero_paid_oss_architecture.md` (kept in the user's Downloads).

## Commands

```bash
# stack (postgres + api). Colima provides the Docker daemon: `colima start` if `docker info` fails.
docker compose up -d
docker compose up -d --build            # after changing deps/Dockerfile
docker compose logs api                 # debug 500s here — tracebacks land in api logs

# tests (run inside the api container — deps live there, host Python is 3.14 with no wheels)
docker compose exec api pytest tests/unit          # pure state-machine, no DB
docker compose exec api pytest tests/integration   # full flow vs real Postgres
docker compose exec api pytest tests/unit/test_state_machine.py::test_completed_is_terminal

# manual flow
curl -sX POST localhost:8000/scenarios/interrupted -H 'content-type: application/json' -d '{}'
curl -sX POST localhost:8000/interviews/action -H 'content-type: application/json' \
     -d '{"interview_id":<ID>,"intent":"CONTINUE_INTERVIEW"}'
```

The `api` service mounts `./app`, `./tests` etc. as volumes and runs uvicorn with `--reload`,
so Python edits are live without a rebuild. Rebuild only when dependencies change.

## Architecture — the load-bearing rules

Three invariants define this system. Preserve them; most bugs come from violating one:

1. **PostgreSQL is the only source of truth.** The LLM and the Pipecat pipeline hold no
   durable business state. If a process restarts, everything is recoverable from the DB.

2. **The orchestrator is the single writer of interview state.** `app/services/interview_orchestrator.py`
   is the ONLY place `interview.status` transitions. It row-locks the interview
   (`SELECT ... FOR UPDATE` via `_lock_interview`) so concurrent callbacks can't both
   start/resume it. Never mutate `interview.status` elsewhere — read helpers live in
   `interview_service.py`, and they deliberately contain no transitions.

3. **Call state ≠ interview state.** A call can be `NO_ANSWER` while the interview is
   `NOT_STARTED`; `DISCONNECTED` while the interview is `IN_PROGRESS, current_question=4`.
   They are separate state machines in `app/domain/states.py`. This separation is what
   makes callback recovery correct — do not collapse them.

### Decision flow

The LLM never decides anything. It classifies the caller's request into an `Intent` and calls
one tool (`/interviews/action`). `states.resolve_action(status, intent)` maps to an `Action`,
then the orchestrator validates the transition against `_ALLOWED_TRANSITIONS` under the lock
before persisting. Illegal or ambiguous requests resolve to `REJECT`/`ESCALATE` — the system
escalates rather than guesses. `states.py` is pure (no DB/IO) and fully unit-tested; put new
business rules there, not in prompts or endpoints.

### Derived state, never trusted pointers

- **Resume position** = first question with no `interview_answer` row
  (`interview_service.next_unanswered_position`), NOT the `current_question` column alone.
- **Expiry** is derived from `expires_at` at read time (`interview_service.effective_status`),
  so a stale `status` can't let an expired interview start.

### Idempotency (no Redis — Postgres does it)

`call.provider_call_id` is `UNIQUE`; `call_service.ingest_call` uses
`INSERT ... ON CONFLICT DO NOTHING` so retried webhooks / double-taps collapse to one row.
When adding tables that reference `call`, remember cleanup order: `interview_event` rows may
reference a call by `call_id` alone (unresolved inbound calls), so delete events before calls
— this exact FK ordering bit `scenario_service._fresh_interview` once.

## Layout of intent

- `app/domain/states.py` — enums + transition tables + `resolve_action`. Pure. Start here for rules.
- `app/services/` — `interview_orchestrator` (transitions), `interview_service` (reads),
  `candidate_resolver` (deterministic phone/identifier lookup, E.164), `call_service`
  (idempotent intake), `scenario_service` (seeds any state for demos/tests).
- `app/api/` — FastAPI routers. Control plane only; **never** streams audio. Endpoints are
  sync `def` so FastAPI runs them in a threadpool with the sync SQLAlchemy session.
- `app/agent/` — Pipecat **1.7** voice bot. `pipeline.py` defines `bot(runner_args)` and runs
  via the official `pipecat.runner` (serves the SmallWebRTC UI on :7860). `tools.py` calls the
  orchestrator over HTTP (not the DB directly), keeping the orchestrator the sole state writer.
  Heavy deps (torch); built from `Dockerfile.agent`, not the core image — the Dockerfile splits
  a byte-stable heavy layer (pipecat/kokoro/torch) from a light layer (fastapi/prebuilt-UI/
  kokoro-onnx) so rebuilds don't re-pull torch. Pipecat import paths drift between releases:
  the pipeline shape (STT → LLM+tools → TTS over WebRTC) is stable, the class names are not —
  introspect the installed package before trusting old imports.

## Provider boundaries

Swapping providers is an env-var change (`STT_PROVIDER`, `LLM_PROVIDER`, `TTS_PROVIDER`,
`TRANSPORT_PROVIDER` in `config.py`), never an orchestrator change. Defaults: Groq free-tier
STT+LLM, local Kokoro TTS, WebRTC transport. Optional adapters: Ollama/faster-whisper (offline),
Twilio (PSTN, paid).

## Conventions

- Migrations: none yet — `init_db()` calls `create_all` on startup. Introduce Alembic before
  the schema needs to evolve on existing data.
- Every meaningful transition writes an `interview_event` (the audit trail that stands in for
  Kafka). Keep this up when adding new transitions.
- Debug 500s via `docker compose logs api`; the response body is a generic message but the
  traceback (often an `IntegrityError` with the exact FK/constraint) is in the logs.
