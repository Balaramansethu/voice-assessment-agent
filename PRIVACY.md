# Privacy and Data Protection

This document describes PII collection, processing, retention, and candidate rights implementation in the voice agent. Every claim is grounded in the actual implemented code and reflects the system's current state — see "Known gaps" for what is NOT yet implemented.

## PII classes collected

The system collects and processes the following personally identifiable information:

1. **Candidate identity**: `Candidate.name` (String, required), `Candidate.phone` (String, indexed, unique per candidate, optional in schema but required for identity verification in practice)
2. **Spoken transcripts**: `AssessmentAnswer.transcript` (full text of every answer the candidate speaks), `InterviewAnswer.transcript` (legacy answers), implicit in raw audio transmitted to STT provider
3. **Interview-specific fields**: `Interview.invitation_code` (12-char unique code per interview, unguessable, one-time use per candidate)
4. **Call metadata**: `Call.from_number` (caller's phone number, optional, required only for Twilio), `Call.to_number` (destination, only on Twilio), connection IP/port implicit in Twilio or WebRTC connection metadata
5. **Application context**: `Candidate.email` (optional), any resume/application text uploaded to `CandidateDocument` table

## Third-party processors (subprocessors)

These services receive PII in the course of normal operation:

1. **Groq (LLM inference, grading)**
   - Receives: full `transcript` text of the answer being graded, question prompt text, expected answer key points, candidate-to-be-graded data
   - Form: HTTP JSON POST to Groq API (`https://api.groq.com/openai/v1/...`)
   - Purpose: conversational LLM responses during the interview, and grading answers against the rubric
   - Retention: per Groq's API Terms of Service (API logs retained for audit; no guarantee of immediate deletion)

2. **Deepgram (STT, TTS)**
   - Receives: raw audio frames (16 kHz PCM), transcribed text (for TTS input)
   - Form: WebSocket streaming for STT (audio in, transcripts out), HTTP POST for TTS
   - Purpose: speech-to-text (candidate answers), text-to-speech (agent responses)
   - Retention: per Deepgram ToS (typically 30 days for API logs)

3. **Twilio (optional PSTN transport)**
   - Receives: from/to phone numbers, raw audio, call metadata (CallSid, duration, direction)
   - Form: HTTPS webhooks (call lifecycle events), TwiML WebSocket stream
   - Purpose: inbound/outbound PSTN call routing and audio transport
   - Retention: per Twilio ToS (API logs, recordings if enabled; user configurable)

4. **LangSmith (observability, tracing) — OPTIONAL, OFF BY DEFAULT**
   - Receives: trace payloads containing step names, latencies, status codes; **content capture is OFF by default**
   - Content capture behavior (when `langsmith_capture_content=true` in `app/config.py`):
     - ON in development: full payloads including transcripts, candidate names, answers, rubrics
     - ON in production only if explicitly enabled: same full payloads sent to LangSmith cloud
     - OFF in production by default: PII/content fields are redacted; only step names, latencies, status codes logged
   - Redacted fields (always stripped in production, unless `langsmith_capture_content=true`): `transcript`, `candidate_name`, `phone`, `email`, `answer`, `reason`, `rationale`, and resume/rubric text (see `app/observability/tracing.py::_REDACT_KEYS`)
   - Retention: per LangSmith ToS (typically 30 days; user can configure)

5. **Self-hosted PostgreSQL**
   - All of the above, plus interview state, grading verdicts, session/call metadata
   - Form: in-database, encrypted in transit (over local Docker network in dev; over TLS in production, operator's responsibility)
   - Purpose: single source of truth for interview state, durable grading jobs, audit trail
   - Retention: controlled by operator (no automatic TTL/deletion job implemented, see "Known gaps")

## Data retention and expiry

The following automatic TTLs and expiry mechanisms are implemented:

- **Voice session tokens** (`VoiceSessionToken.expires_at`): 30 seconds (set via `voice_session_token_ttl_seconds` in `config.py`, default 30). Consumed tokens are marked with `consumed_at` timestamp and cannot be reused.
- **Interview lifecycle** (`Interview.expires_at`): derived from `interview_expiry_hours` (default 72 hours). An expired interview cannot be resumed (checked at read time via `effective_status` in `interview_service.py`).
- **Assessment session** (`AssessmentSession.created_at`): no automatic expiry; recruiter-facing records remain indefinitely.
- **Call records** (`Call`): no automatic TTL; `Call` rows persist indefinitely for audit trail.
- **Grading job backlog** (`GradingJob`): no automatic archival/deletion; jobs persist after `status=COMPLETE` or `status=FAILED` for audit.

**Known gap**: No background job or cron schedule exists to delete, archive, or anonymize candidate records, transcripts, or call history after a retention period. The operator must manually clean up old data or implement a deletion policy — this must be done before production launch with real candidate data.

## Export and deletion

- **Candidate-facing data export**: NO endpoint exists (`GET /candidates/{id}/export` or similar). Not implemented.
- **Candidate-facing data deletion**: NO cascading delete endpoint exists (`DELETE /candidates/{id}` with cascade). Not implemented.

If these capabilities are added in the future, the following FK cleanup order MUST be respected (reflecting the actual database constraints):

1. Delete `interview_event` rows (references `call` or `interview` by id, only path out of the audit trail)
2. Delete `assessment_answer` and `assessment_session` rows (reference `call`, `candidate`, `interview`)
3. Delete `answer_evaluation` and `interview_answer` rows (reference `interview`, candidate data)
4. Delete `interview_question` and `role_question` rows (reference `interview`)
5. Delete `call` rows (owns `interview_event` rows via FK)
6. Delete `interview` rows (owns questions, events, answers)
7. Delete `candidate` rows (owns all interviews, calls, documents)

## Consent and notice

- **Consent capture**: NO consent-capture flow is implemented. No candidate-facing consent text, no "agree to terms" dialog, no evidence of consent collection.
- **Notice to candidates**: The agent's greeting (`app/agent/prompts.py`) does NOT include privacy notices, data retention statements, or processor disclosures.

**Required before production**: A real deployment handling actual candidate data must implement:
1. Consent capture (e.g., a pre-call IVR that reads a privacy notice and requires explicit opt-in)
2. Privacy notice linked from job application landing page
3. Subprocessor and retention disclosure to candidates
4. Mechanism for candidates to request data export/deletion (even if not yet automated)

## Security controls

The following controls reduce PII exposure risk:

- **Transcripts in logs/traces**: suppressed by default in production (`langsmith_capture_content=false`), redacted via `_REDACT_KEYS` in `tracing.py`
- **Passwords/secrets**: never logged; `settings` and credentials are not printed or captured in traces
- **Phone numbers**: indexed and logged only for identity verification (call ingestion), never in response bodies to the agent
- **One-time voice tokens**: 30-second TTL, cryptographically random, single-use, consumed at WebSocket auth time

## Scope of this document

This describes the *current* POC implementation, not a promise of production-ready privacy compliance. Before handling real candidate data, the operator must:

1. Engage legal counsel to define data processing obligations (GDPR, CCPA, jurisdiction-specific rules)
2. Finalize subprocessor agreements with Groq, Deepgram, Twilio, LangSmith (if used)
3. Implement automated data retention/deletion or manual governance processes
4. Add consent capture and privacy notices
5. Define and implement subject access request (SAR) and deletion workflows
6. Configure encrypted backups, access controls, and breach response procedures

This document will be updated as capabilities are added.
