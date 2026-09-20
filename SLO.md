# Service Level Objectives (SLO)

This document defines target performance and reliability metrics for the voice agent. These are observability targets for operational monitoring and tuning — they are NOT automated alerts (no alerting infrastructure exists yet; see "Infrastructure" below).

All targets and their sources are listed here so operators can track against them manually via the exposed observability endpoints.

## Voice agent latency

**Greeting latency**: Time from WebSocket connect to first TTS audio output (agent says "hello")
- **Target**: < 3.0 seconds (source: `scripts/selftest.py::GREETING_DEADLINE_S`)
- **Why**: Caller hears agent within 3s of answering, reducing perception of delay/hang
- **Measured via**: `selftest.py` observer capturing `TTSStartedFrame` timestamp

**Reply latency (TTFB, per turn)**: Time from end-of-user-speech to first TTS token output
- **Target (p95)**: < 3.5 seconds (source: `scripts/selftest.py::TTFB_P95_MAX_S`)
- **Target (hard max)**: 8.0 seconds absolute max (source: `scripts/selftest.py::TTFB_HARD_MAX_S`)
- **Why**: Conversational feel breaks if the bot takes > 3.5s per turn in the typical case, or > 8s in the worst case
- **Measured via**: Pipecat `UserBotLatencyObserver` (built-in, enabled via `enable_metrics=True`), surfaced as `MetricsLogObserver` output

## Call and session duration

**Max call duration**: 900 seconds (15 minutes, source: `app/config.py::max_call_duration_seconds`)
- **Enforcement**: Hard stop; `_duration_watchdog` in `app/agent/pipeline.py` ends the call after 900s
- **Why**: Prevent resource exhaustion (concurrent call ceiling, token budget), avoid long-running calls from blocking other callers

**Max inactivity**: 120 seconds (source: `app/agent/pipeline.py` reads `MAX_INACTIVITY_SECONDS`, default 120)
- **Enforcement**: `_inactivity_watchdog` (added P4) force-ends a call if both bot and caller are silent for 120s
- **Why**: Free up concurrent-call slots for waiting callers; prevent zombie calls

## Grading job queue health

**Backlog**: No explicit SLO target set yet (proposed starting point: backlog should not exceed 50 jobs under normal load)
- **Measured via**: `GET /observability/grading` → `backlog` field
- **Why**: Backlog represents answers awaiting grading; excessive backlog means candidate results delayed
- **Current system capacity**: Worker runs in a single process, claims up to 5 jobs per batch, polls every 2s — capable of ~150 jobs/min on real Groq grading (2–4s per job)

**Oldest pending job age**: Proposed starting SLO: should not exceed ~300 seconds (5 minutes) under normal load
- **Measured via**: `GET /observability/grading` → `oldest_pending_age_seconds`
- **Why**: Stale pending jobs indicate worker stall, provider throttling, or load surge; triggers operator investigation

**Average job processing duration**: Informational (no hard SLO, baseline expectation ~2–4s per job via Groq)
- **Measured via**: `GET /observability/grading` → `avg_processing_seconds`
- **Why**: Helps operator tune `GRADING_LEASE_SECONDS` and max_attempts; identifies slow grader model performance

## Call concurrency

**Max concurrent calls**: 5 (source: `app/config.py::max_concurrent_calls`)
- **Enforcement**: PostgreSQL partial unique index on `call(interview_id) WHERE status='ACTIVE'` prevents second active call per interview; `at_concurrency_ceiling()` rejects new calls when active count ≥ 5
- **Why**: Reasonable ceiling for a zero-paid POC with constrained Groq/Deepgram quotas

**Per-number rate limit**: 3 calls per minute (source: `app/config.py::max_calls_per_number_per_minute`)
- **Enforcement**: Application-level counter in `call_service.ingest_call`
- **Why**: Prevent rapid-fire retry floods from a single malicious/confused caller

## Infrastructure and monitoring

**NO alerting infrastructure exists yet.** These targets are for manual observation only:

- No Grafana dashboards
- No PagerDuty, Slack, or email alerts
- No automated thresholds that page or notify operators

Operations currently relies on:
1. Manual checks of `GET /observability/grading` (backlog, job age, processing time)
2. Container logs (`docker compose logs api`, `docker compose logs worker`)
3. Database size/replication lag monitoring (operator's responsibility)

**Deferred (PR-511)**: Alerting for auth failures, provider errors, backlog age spike, DB saturation, disk space, backup age, and abnormal spend/session volume requires standing up infrastructure (even a free-tier service like UptimeRobot or a custom Lambda) and integrating it. This is out of scope for the zero-paid POC.

## Observability endpoints

All targets below are queryable over HTTP (ops-scoped, require `X-Ops-Key` header):

- `GET /observability/grading` — backlog, running, retry/failed/complete counts, oldest-pending-age-seconds, avg-processing-seconds
- `GET /observability/grading?call_id=<id>` (PR-509) — same breakdown scoped to one call's jobs
- `GET /observability/summary` — token usage, errors, latency by traced run type (LangSmith)
- `GET /observability/runs` — recent trace runs with id, name, type, tokens, error, latency

## Tuning and production calibration

These targets are based on:
- `scripts/selftest.py` live-run measurements (real Groq/Deepgram API calls) against the codebase at commit time
- Configured thresholds in `app/config.py` and pipeline.py (`MAX_INACTIVITY_SECONDS`, backoff caps, etc.)
- The current zero-paid POC has NO production traffic history

**Before production launch with real candidates**, operators should:
1. Run representative load tests and re-calibrate p95/p99 latency targets
2. Validate that grading backlog stays under control with expected question volume
3. Adjust `max_concurrent_calls`, `max_calls_per_number_per_minute`, and grading worker pool size based on actual traffic
4. Define incident thresholds (e.g. "page if oldest_pending_age_seconds > 600")
5. Set up automated dashboards and alerts via the infrastructure of choice

## Current status

- Voice latency targets (greeting, TTFB) are measured and validated by `selftest.py`
- Call concurrency and rate limits are enforced at the DB/application level
- Grading queue metrics are exposed but not alerted on
- No historical SLO compliance data (zero production traffic yet)
