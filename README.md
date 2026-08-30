# Inbound Interview / Assessment Voice Agent

A production-shaped, **open-source-first** voice AI agent that conducts spoken technical
screening assessments over **the phone** (Twilio) or a **browser** (WebRTC). A caller states
the role they're interviewing for; the agent asks that role's pre-seeded questions one at a
time, **grades each answer silently** against a rubric (correct / partial / incorrect + missing
key points), and records the result for a recruiter — never revealing the score to the
candidate. It can also answer factual questions about the company/role from a **RAG** knowledge
base, and every step is traced to **LangSmith**.

Runs at **$0** for inference (Groq free tier) with local TTS; deployed on **Oracle Cloud
free-tier** ARM with automatic HTTPS.

---

## What it does

```
Caller (phone or browser)
   │  audio
   ▼
Pipecat pipeline ── STT (Groq Whisper) ── LLM (gpt-oss-120b) ── TTS (Kokoro, local)
   │                         │
   │                         │ tool calls (HTTP, never DB directly)
   ▼                         ▼
Greet → ask role → ask role's questions → grade each answer silently → summary
                             │
                             ▼
                  FastAPI control plane
        (orchestrator · RAG retrieval · graded LLM-judge)
                             │
                             ▼
                    PostgreSQL + pgvector
                   (single source of truth)
```

- **Assessment flow (current):** pick a role → seeded question bank → per-answer graded,
  silent validation → recorded summary for the recruiter.
- **RAG:** grounded answers to candidate questions (company/role/comp/process) with citations;
  refuses / escalates instead of hallucinating.
- **Observability:** LangSmith traces for every step — tokens, latency, errors, nested by
  request.

---

## Architecture — the load-bearing rules

Three invariants define the system:

1. **PostgreSQL is the only source of truth.** The LLM and the Pipecat pipeline hold no durable
   business state. If a process restarts, everything is recoverable from the DB. Vectors live in
   `pgvector` in the same database — no separate vector store.

2. **The backend decides; the LLM converses.** The LLM never decides grades or business
   outcomes. It calls small tools (`start_assessment`, `submit_answer`, `kb_answer`) over HTTP;
   the FastAPI services do the deterministic work (question progression, rubric-grounded
   grading, retrieval) and hand back a per-turn `instruction` the LLM must follow. This
   **structured-flow** pattern (scoped prompt + tool-result instructions) keeps a small model
   on-rails and prevents it from wandering or ending early.

3. **Transport is swappable, logic is not.** The exact same backend + agent logic runs over
   **WebRTC** (browser) or **Twilio** (phone) — only the Pipecat transport changes. Providers
   (STT/LLM/TTS) sit behind adapters and are env-var swaps.

### Silent, graded validation

Each seeded question carries its own "correct answer" definition (`expected_answer` +
`key_points`). On every answer, an LLM-judge grades it **graded** (not binary):
`correct | partial | incorrect` + a 0–1 score + which key points were covered/missed. The
verdict is **stored for the recruiter and never returned to the agent to speak** — the agent
stays neutral and moves to the next question.

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Voice pipeline | **Pipecat 1.7** | STT → LLM+tools → TTS; VAD via `VADProcessor` |
| Transport | **WebRTC** (browser) / **Twilio** (phone) | `SmallWebRTC` or Twilio media streams (8kHz μ-law) |
| STT | **Groq Whisper** (`whisper-large-v3-turbo`) | free tier |
| LLM | **Groq `openai/gpt-oss-120b`** | reasoning hidden (`reasoning_format=hidden`) so it isn't spoken |
| TTS | **Kokoro** (local ONNX, CPU) | no GPU; hosted TTS is a drop-in swap |
| API / control plane | **FastAPI** | sync endpoints in a threadpool |
| Database | **PostgreSQL 16 + pgvector** | source of truth + vector store (HNSW) |
| Embeddings | **fastembed** `bge-small-en-v1.5` (ONNX, CPU) | local, zero-paid |
| Retrieval | dense (pgvector) + sparse (Postgres FTS) fused with **RRF** | hybrid |
| Observability | **LangSmith** | traces, tokens, latency, errors |
| Containers | **Docker Compose** | dev + prod stacks |
| Reverse proxy / TLS (prod) | **Caddy** | automatic Let's Encrypt HTTPS |
| Hosting (prod) | **Oracle Cloud** free-tier ARM (Ampere A1) | Ubuntu 22.04 |

---

## Repository layout

```
app/
  config.py                 all settings (env-driven); provider + observability config
  domain/states.py          interview/call state machines (pure, unit-tested)
  db/models.py              candidate, interview, call, event, RAG (kb_*), assessment_* tables
  db/session.py             engine + pgvector extension + HNSW/FTS index bootstrap
  services/
    interview_orchestrator  the single writer of interview state (row-locked transitions)
    assessment_service      role question banks + graded, silent LLM-judge
    evaluation_service      rubric-grounded post-hoc scoring
    candidate_resolver      deterministic phone/name/identifier lookup
    call_service            idempotent call intake
    scenario_service        seed any state for demos/tests
  rag/
    embeddings.py           fastembed adapter (provider boundary)
    retriever.py            hybrid dense+sparse retrieval, RRF, scope/visibility filters
    generate.py             grounded generation over retrieved context (Groq)
    ingest.py               markdown → chunks → embeddings → pgvector (idempotent)
  agent/
    pipeline.py             Pipecat bot: WebRTC/Twilio transport, tools, silent-grading flow
    prompts.py              tight, directive system prompt
    tools.py                HTTP clients to the control plane (agent never touches the DB)
  api/                      FastAPI routers (assessment, rag, observability, interviews, ...)
  observability/
    tracing.py              LangSmith bootstrap + traced Groq client (token capture)
    query.py                trace summaries via the current runs-query API
data/
  questions/*.json          per-role assessment banks (expected answer + key points)
  kb/**, candidates/**      RAG knowledge base + mock candidate docs
scripts/                    seed_questions, ingest_kb, seed, trace_summary
tests/                      unit (state machine) · integration (flow) · rag (retrieval/isolation)
deploy/Caddyfile            reverse proxy + auto-HTTPS for production
docker-compose.yml          dev stack (browser WebRTC)
docker-compose.prod.yml     prod stack (Twilio phone + Caddy) for Oracle Cloud
Dockerfile / Dockerfile.agent   core API image / heavier voice-agent image
```

---

## Run locally (browser / WebRTC)

Prereqs: Docker (Colima works: `colima start`). Create your local config + env from the
templates, then set at least `GROQ_API_KEY` in `.env`:

```bash
cp app/config.example.py app/config.py        # gitignored local settings module
cp .env.example .env                          # gitignored secrets (fill GROQ_API_KEY, ...)
docker compose up -d                          # postgres + api + agent
docker compose exec api python -m scripts.seed_questions   # seed assessment banks
docker compose exec api python -m scripts.ingest_kb        # seed RAG knowledge base
open http://localhost:7860                     # connect, allow mic, say a role
```

Inspect results:
```bash
curl -s localhost:8000/assessment/roles
curl -s localhost:8000/assessment/<session_id>/summary | python3 -m json.tool
```

### Tests
```bash
docker compose exec api pytest tests/unit          # pure state-machine rules
docker compose exec api pytest tests/integration   # full flow vs real Postgres
docker compose exec api pytest tests/rag           # retrieval + isolation
```

---

## Deploy to production (Twilio phone + Oracle Cloud)

The feature is transport-agnostic; production swaps the transport to Twilio and fronts the
agent with Caddy for HTTPS.

**Prereqs:** an OCI ARM instance (Ubuntu, Docker, ports 22/80/443 open), a domain pointing at it
(e.g. DuckDNS), and a Twilio number + credentials in `.env` (plus `PUBLIC_HOST=<your-domain>`).

```bash
# on the server
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api python -m scripts.seed_questions
docker compose -f docker-compose.prod.yml exec api python -m scripts.ingest_kb
```

Caddy obtains a Let's Encrypt cert automatically. Point the Twilio number's Voice webhook at
`https://<your-domain>/` (POST) — it returns TwiML that opens a media stream to the agent.
Then **call the number**.

Flow in production:
```
Caller → Twilio number → HTTPS webhook (Caddy :443) → agent TwiML
      → Twilio Media Stream (WSS) → Pipecat (Groq STT/LLM, Kokoro TTS) → assessment
```

---

## Configuration

**`app/config.py` is gitignored** — it is the local settings module and is intentionally
kept out of version control so secrets never get committed. After cloning, create it from the
tracked template:

```bash
cp app/config.example.py app/config.py
```

`config.py` ships with empty/placeholder defaults; the real secret values are supplied at
runtime from **`.env`** (also gitignored). You normally only edit `.env`.

### `.env`

Never commit `.env`. Key settings (see `.env.example`):

- `GROQ_API_KEY`, `GROQ_LLM_MODEL`, `GROQ_STT_MODEL` — inference (Groq free tier)
- `TTS_PROVIDER`, `TTS_VOICE` — local Kokoro
- `EMBEDDING_MODEL`, `RAG_*` — retrieval tuning
- `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` — observability
- `TWILIO_*`, `PUBLIC_HOST` — telephony + production host
- `TRANSPORT_PROVIDER`, `STT_PROVIDER`, `LLM_PROVIDER` — provider swaps

---

## Notes

- **Secrets:** API keys live only in `.env` (gitignored). Rotate any key that has been shared.
- **Migrations:** none yet — `init_db()` calls `create_all` on startup (+ creates the pgvector
  extension and HNSW/FTS indexes). Introduce Alembic before evolving the schema on real data.
- **Observability:** with a LangSmith key set, every server-side step (retrieval, grading,
  generation) is a nested run with token counts; `GET /observability/summary` aggregates them.
