# Current Production-Readiness Audit

Audit date: 2026-09-19  
Repository: `voice_agent`  
Audit mode: read-only verification of the current working tree

## Executive verdict

**Status: NOT production-ready. External launch remains blocked.**

The repository has implemented a meaningful P0 hardening slice, especially around public
Twilio ingress, one-use PostgreSQL-backed media tokens, input limits, strict grading output,
production scenario lockout, and trace redaction. It has not implemented the full production
plan. The repository's own checklist reports **24 of 111 checkboxes complete (21.6%)**.

Current readiness score: **4.8/10**. This is an improvement over the original audited POC,
but it remains a controlled-demo/internal-pilot system rather than a production hiring system.

Important delivery fact: the production-hardening changes are present as **uncommitted working-
tree changes**. The latest commit is still `d432e32`, and there is no evidence in this audit that
the current working tree has been built by CI or deployed to the production host.

## Verification performed

- `docker compose exec api pytest -q`: **184 passed, 1 skipped**.
- `docker compose -f docker-compose.prod.yml config --quiet`: passed, with warnings because
  `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` were not defined in the audit shell.
- `git diff --check`: passed.
- Existing `graphify-out/graph.json`: queried to trace live module relationships.
- Production host, Twilio console, provider consoles, and real PSTN canary were not inspected.

The skipped test means the normal suite still does not prove the complete Pipecat/audio runtime.
The current CI installs the core project and runs pytest; it does not build or exercise the agent
image or a real Twilio call.

## Production gate status

| Gate | Status | Current evidence | Missing evidence/control |
|---|---|---|---|
| G1: Authenticated public voice admission | **Partial** | Signed webhook, short-lived token digest, atomic single-use redemption, real CallSid, rate/concurrency/duration controls | Query token is present in Caddy access logs; webhook replay mints a new token; no real PSTN/cross-process deployment evidence; route matcher remains `/ws*` |
| G2: Verified candidate identity and authorization | **Not implemented** | Phone resolver exists | Caller still supplies name and role; no invitation code/OTP; no candidate-bound principal; no agent/recruiter/worker scopes |
| G3: Durable restart/reconnect and turn recovery | **Not implemented** | Twilio disconnect closes the call; interview backend has derived resume helpers | Live assessment still creates a fresh assessment, uses in-memory session binding, and lacks `turn_id`, expected question, ownership fencing, and replayable responses |
| G4: Durable grading | **Not implemented** | Grader output is validated and malformed grades remain ungraded | Accepted answers still rely on FastAPI `BackgroundTasks`; no PostgreSQL job, lease, retry, reconciliation, dead-letter state, or worker |
| G5: Safe, reproducible grading | **Partial** | Strict JSON Schema, finite/range validation, prompt-data delimiting, application-derived rating/pass | No persisted prompt/rubric/model/question-bank versions, calibration benchmark, fairness/repeatability evidence, or human-review workflow |
| G6: Migrations, backups, readiness, rollback | **Not implemented** | PostgreSQL remains the source of truth | No Alembic; startup still runs `create_all` and ad-hoc DDL; no `/ready`; no backup/restore drill; no rollback evidence |
| G7: Voice/operations SLOs | **Not implemented** | Basic LangSmith tracing and duration limits | No EOU/TTFT/TTS-TTFB p95/p99 SLO, grading backlog metrics, alert delivery, graceful drain evidence, or provider-outage drill |
| G8: Privacy lifecycle | **Partial** | Production trace payload redaction and secret/build-context hardening | No retention policy, consent event, export/delete workflow, processor-deletion tracking, Deepgram opt-out flag, or verified provider data controls |
| G9: Reproducible CI/release | **Partial** | Core pytest suite is green | CI does not build/test both images, run migrations, test the Pipecat stack, publish commit-SHA images, or verify deployment digests |

## What is genuinely implemented

### 1. Public Twilio ingress is materially stronger

- `VoiceSessionToken` stores only a SHA-256 token digest, provider CallSid, phone metadata,
  expiry, consumption time, and bound call.
- Token redemption is an atomic conditional `UPDATE ... RETURNING`, so concurrent reuse has
  one winner.
- The Pipecat entry point redeems the token before transport/provider construction and rejects
  invalid connections with close code 4003.
- The parsed Twilio CallSid is compared with the CallSid bound to the token.
- Calls are stored using the real CallSid and marked active/terminal.
- Per-number rate limits, concurrent-call limits, stale-call cleanup, and a maximum-duration
  watchdog were added.
- Twilio signature, token replay, concurrency, duration, and input edge cases have substantial
  integration coverage.

Relevant implementation:

- `app/services/voice_session_service.py`
- `app/api/telephony.py`
- `app/agent/pipeline.py`
- `app/services/call_service.py`
- `tests/integration/test_telephony.py`

### 2. Grading input/output safety improved

- Assessment and RAG request strings have explicit strict bounds.
- Silent graders request strict JSON Schema responses.
- Pydantic rejects malformed, extra, non-finite, boolean, negative, and out-of-range scores.
- Rating and pass/fail are derived in backend code.
- Candidate transcripts and rubric material are JSON encoded inside explicit untrusted-data
  boundaries.
- Invalid grading results remain `score = NULL` and cannot stamp completion.

### 3. P0 operational safety improved

- Production startup validates placeholder/default configuration.
- Scenario endpoints return 404 in production mode.
- `.dockerignore` excludes secret-bearing and irrelevant build context.
- Production LangSmith input/output processing redacts candidate-content-shaped fields by default.
- The API and agent Dockerfiles can materialize a tracked, secret-free config template.

## Production blockers still present

### Blocker 1 — Candidate identity is still self-asserted

The active voice flow still asks for the caller's name and desired role, then passes those values
to `start_assessment`. `AssessmentSession` has no verified candidate/invitation relationship.
Someone can still complete an assessment under another person's name.

Required before launch:

- Invitation/application code or equivalent verified binding.
- Candidate and interview IDs derived from server-bound session claims.
- Candidate, agent, recruiter, worker, and operations authorization scopes.
- Cross-candidate negative tests at the API boundary.

### Blocker 2 — The live assessment bypasses the durable orchestrator

The Pipecat runtime registers `start_assessment`, `submit_answer`, and `kb_answer`. It does not
route the active assessment through the interview orchestrator. Its session ID is held in a
per-connection dictionary. A reconnect does not reconstruct the active assessment from the
interview aggregate.

### Blocker 3 — Answer submission has no transactional turn protocol

The request contains only `session_id` and transcript. Position is derived by counting answer
rows, and duplicate detection is a normalized-transcript/time-window heuristic. The existing
unique position constraint can turn concurrency into an error but cannot safely replay a lost
response or recognize an STT-altered retry.

Required protocol:

```text
submit(call_id, session_id, turn_id, expected_question_id, transcript)
  -> lock canonical session
  -> validate active-call ownership
  -> replay the stored result for an existing turn_id
  -> validate expected question
  -> atomically write answer + turn result + event + grading job
```

### Blocker 4 — Grading remains lossy

`POST /assessment/grade` still schedules `grade_pending_answer` through in-process FastAPI
`BackgroundTasks`. A restart or provider failure after answer commit can leave the answer
permanently ungraded. There is no durable PostgreSQL queue or reconciliation worker.

### Blocker 5 — API authorization is absent

Candidate, assessment, interview, RAG, observability, transcript, and score endpoints do not have
application-level authentication. Current Caddy routing hides most endpoints from the public
internet, but network topology is not an authorization model.

`POST /rag/candidate/context` still accepts an arbitrary numeric `candidate_id`; its SQL filter is
correct, but the caller's right to use that ID is not established.

### Blocker 6 — Schema lifecycle remains unsafe

`init_db()` still performs `Base.metadata.create_all()` and lightweight `ALTER`/index operations
at API startup. There is no schema revision, one-shot migration service, downgrade/compatibility
plan, or restricted runtime database role.

### Blocker 7 — Operations and recovery are incomplete

- `/health` is shallow and exposes provider/key-presence metadata.
- No dependency-aware `/ready` endpoint exists.
- API/agent/Caddy have no meaningful Compose health checks.
- No automated encrypted database backup or tested restore exists.
- No CPU, memory, PID, log-size, non-root, capability-drop, or read-only filesystem policy is
  defined in production Compose.
- Base/service images and most Python dependencies remain floating/broadly ranged.
- CI does not build/test the voice image or deploy an immutable tested digest.

### Blocker 8 — Privacy is incomplete

Trace redaction is useful, but raw candidate data still has no implemented retention, consent,
export, deletion, or third-party deletion workflow. Deepgram STT/TTS is not configured with the
researched data-retention opt-out flag. Local `.env` permissions are still `0644`, not `0600`.

## P0 implementation caveats

1. **Token leakage into access logs:** the raw voice-session token is carried in the WebSocket
   query string while Caddy JSON access logging is enabled. Standard request logging normally
   includes the URI/query. The token is short-lived and single-use, which reduces exploitability,
   but logging bearer material is still incorrect.
2. **Webhook replay is bounded, not eliminated:** replaying a valid signed voice webhook for the
   same CallSid invalidates the old unused token and mints a new one. Rate limiting constrains the
   blast radius, but this is not a stable idempotent admission result.
3. **Application time controls security expiry:** token expiry and rate windows use application
   clocks rather than PostgreSQL time, leaving avoidable clock-skew sensitivity.
4. **Broad WebSocket matcher:** Caddy uses `path /ws*`, matching more paths than exact `/ws` and
   `/ws/*`.
5. **Environment mode is a free string:** a typo such as `prod` can disable several production
   safeguards because the comparisons only recognize the exact word `production`.
6. **Agent receives excessive secrets:** the agent consumes the complete `.env`, including values
   not required by the voice process.

## Updated module scores

| Module | Score | Current status |
|---|---:|---|
| Public ingress / Twilio | **6/10** | Significant P0 implementation; still needs log hygiene, stronger replay semantics, and real deployment proof |
| Caller identity / authorization | **1/10** | Not implemented |
| Voice pipeline and lifecycle | **5/10** | Real CallSid, disconnect closure, duration watchdog; durable resume and fully proven barge-in absent |
| FastAPI control plane | **3/10** | Better validation and production lockout; no scopes/authentication |
| State machine/orchestrator | **6/10 design, 3/10 live use** | Good backend remains bypassed by active assessment path |
| Assessment/grading | **5/10** | Safer output and prompts; durability and versioning absent |
| RAG | **5/10** | Good SQL visibility and bounds; ownership authorization and quality benchmark absent |
| Database/schema | **4/10** | Useful constraints and token state; no Alembic or least-privilege roles |
| Observability/privacy | **4/10** | Trace redaction added; SLOs and data lifecycle absent |
| Deployment/operations | **3/10** | Compose/Caddy work, but no readiness, backups, limits, immutable delivery, or rollback proof |
| Testing | **6/10** | 184 green tests and improved security cases; no standard CI voice-image/PSTN/failure-drill coverage |

## Current launch decision

- Local/demo use: **acceptable**.
- Controlled internal testing with non-sensitive data: **acceptable with caution**.
- Real candidates or consequential hiring decisions: **not approved**.
- Public production launch: **blocked**.

The next required engineering milestone is not more prompt tuning. It is **Alembic baseline plus
verified identity/authorization**, followed by the canonical session/turn protocol and durable
PostgreSQL grading worker.

