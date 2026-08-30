# RAG Integration Plan — Interview Voice Agent (Pipecat + LiveKit)

Status: **Phases 0–4 IMPLEMENTED & VERIFIED** (pgvector + ingestion + hybrid retrieval +
Pipecat tools + rubric scoring). Phases 5 (LiveKit) and 6 (observability/eval harness) pending.
Target: a production-shaped Retrieval-Augmented
Generation layer, bound to both the existing Pipecat agent and a new LiveKit agent path,
seeded with realistic mock data. Zero-paid / open-source-first, consistent with the current
architecture (`PostgreSQL = source of truth`, `orchestrator = single writer`, provider
boundaries swappable).

---

## 1. Why RAG here — three concrete jobs it does

RAG is only worth adding if it has a real job. This plan gives it three, each mapped to a
current gap:

| # | Use case | Today | With RAG |
|---|---|---|---|
| A | **Answer candidate questions** ("what's the tech stack / comp band / remote policy / interview process?") | Bot hallucinates or says "I'm not sure" | Grounded answer from a **Company & Role KB**, with citations, or a safe escalation |
| B | **Evaluate answers** (score the candidate's spoken answer) | Answers stored as raw transcript, no grading | Retrieve the **role rubric + model answer** for that question → LLM-judge scores against it → persisted |
| C | **Personalize** (reference the candidate's background, ask relevant follow-ups) | One fixed question list for everyone | Retrieve the **candidate's resume/application** → tailored follow-ups and context |

Non-goal: RAG never decides interview *state*. It supplies **knowledge** (read-only). The
orchestrator remains the only writer of `interview.status`. This is the load-bearing boundary.

---

## 2. Principles (invariants preserved)

1. **Postgres stays the single source of truth** — vectors live in `pgvector`, not a new DB.
2. **Orchestrator owns state** — RAG is a read path; scoring writes go through the orchestrator.
3. **Provider-neutral** — embeddings/reranker/generator are swappable behind interfaces.
4. **Transport-agnostic RAG** — one retrieval service, consumed identically by Pipecat and
   LiveKit agents (via MCP + REST). No agent-framework lock-in.
5. **Least privilege on candidate data** — a caller can only retrieve *their own* documents;
   internal rubrics are never spoken to the candidate.

---

## 3. Target architecture

```text
                         ┌──────────────────────────────┐
     Candidate ── audio ─▶│  Agent runtime (either)      │
                         │  • Pipecat / SmallWebRTC      │  ← existing
                         │  • Pipecat / LiveKit transport │  ← new (§10)
                         │  • LiveKit-native agent        │  ← new (§10)
                         └───────────────┬───────────────┘
                          tool calls (MCP + REST), never DB directly
                                         ▼
                         ┌──────────────────────────────┐
                         │   RAG Service (FastAPI)        │
                         │   • /kb/search  /kb/answer     │
                         │   • /candidate/context         │
                         │   • /evaluate                  │
                         │   • MCP server (same tools)    │
                         └───────┬───────────────┬────────┘
                     retrieve    │               │  score (LLM judge)
                                 ▼               ▼
              ┌───────────────────────┐   ┌──────────────────┐
              │  Retrieval core        │   │  Interview        │
              │  • query embed (local) │   │  Orchestrator     │  ← writes scores
              │  • dense (pgvector)    │   │  (single writer)  │
              │  • sparse (tsvector)   │   └────────┬─────────┘
              │  • RRF fusion + rerank │            │
              └───────────┬───────────┘            ▼
                          ▼                   ┌──────────────┐
                 ┌──────────────────┐         │ PostgreSQL   │
                 │ PostgreSQL +     │◀────────│ (truth)      │
                 │ pgvector (HNSW)  │         └──────────────┘
                 └──────────────────┘
```

Key idea: **RAG is a standalone service**, exposed as **MCP tools** (for agent tool-calling)
*and* REST (for the orchestrator/scoring path). Both Pipecat and LiveKit agents call the
exact same tools — the binding layer is MCP, not framework-specific glue.

---

## 4. Technology choices (zero-paid, production-shaped)

| Concern | Choice | Rationale | Swap-out |
|---|---|---|---|
| Vector store | **pgvector** (extension on existing Postgres 16) + **HNSW** index | Keeps single source of truth; no new service; transactional with business data | Qdrant / Weaviate if scale demands |
| Embeddings | **fastembed** (ONNX, CPU) — `BAAI/bge-small-en-v1.5` (384-dim) | Local, no GPU, same ONNX pattern as Kokoro; fast; free | `bge-base`, `nomic-embed-text` via Ollama, or hosted |
| Sparse/keyword | **Postgres FTS** (`tsvector` + `ts_rank_cd`) | Hybrid recall with zero extra infra | ParadeDB / `pg_search` (BM25) |
| Fusion | **Reciprocal Rank Fusion (RRF)** of dense+sparse | Robust hybrid without tuning weights | Weighted linear |
| Reranker (optional) | **`bge-reranker-base`** cross-encoder (ONNX/CPU) | Precision boost on top-k; toggle by latency budget | Cohere/rerank hosted |
| Generator | **Groq LLM** (`openai/gpt-oss-20b`) — existing | Grounds answer over retrieved context; already wired | any OpenAI-compatible |
| Tool binding | **MCP** (Model Context Protocol) server + REST | One implementation, consumed by Pipecat (`mcp_service`) and LiveKit agents | — |
| Eval judge | Groq LLM as **LLM-as-judge** (RAGAS-style) | Free-tier scoring for faithfulness/relevance | hosted judge |

Everything on the required path stays local/free; Groq (free tier) is the only hosted piece,
already in use.

---

## 5. Data model additions

New tables (all in the same Postgres):

```text
kb_source           id · type(company|role|faq|rubric) · title · uri · version · checksum
                    · visibility(candidate|internal) · role · created_at
kb_chunk            id · source_id → kb_source · ordinal · content · token_count
                    · metadata(jsonb) · tsv(tsvector, generated)         -- sparse index
kb_chunk_embedding  chunk_id → kb_chunk · model · dim · embedding vector(384)  -- HNSW index

candidate_document  id · candidate_id → candidate · doc_type(resume|application|prior_feedback)
                    · content · metadata(jsonb)
candidate_doc_embedding  document_id → candidate_document · embedding vector(384)

answer_evaluation   id · interview_id · question_id · answer_id · rubric_source_id
                    · score(0-5) · rationale · retrieved_context(jsonb) · model · created_at
rag_query_log       id · session_id · scope · query · retrieved_ids(jsonb) · reranked bool
                    · latency_ms · grounded bool · created_at        -- observability
```

Notes:
- `visibility` enforces the guardrail: `internal` sources (rubrics, model answers) are **never**
  returned to the candidate-facing `kb_answer` tool — only to the `/evaluate` scoring path.
- `candidate_document` retrieval is **row-filtered by the resolved `candidate_id`** (bound
  per connection in the bot, exactly like `interview_id` today) → a caller can never retrieve
  another candidate's data.
- Indexes: HNSW on the two embedding tables (cosine); GIN on `tsv`.

---

## 6. Mock data (production-realistic corpora)

Seeded via an ingestion script (`scripts/ingest_kb.py`) so demos are repeatable. Proposed
`data/kb/` corpus:

```text
data/kb/company/
  handbook.md            benefits, PTO, remote policy, working hours, values
  compensation.md        salary bands per level (candidate-visible ranges only)
  tech_stack.md          languages, infra, data stores, deployment
  interview_process.md   rounds, timeline, what to expect, reschedule policy
data/kb/roles/
  backend_engineer.md    responsibilities, required skills, leveling (L3–L6)
data/kb/rubrics/         (visibility = internal)
  be_system_design.md    competency, signals, model answer, 0–5 scale anchors
  be_databases.md        "
  be_behavioral.md       "
data/kb/faq/
  logistics.md           timezone, rescheduling, accessibility, contact
data/candidates/
  rahul_resume.md        realistic resume (roles, projects, stack)
  rahul_application.md    application answers, notice period, expected comp
  rahul_prior_feedback.md screening notes (visibility = internal)
```

The existing `scenario_service` seeds interviews; a parallel `kb_seed` seeds + ingests the
corpus (chunk → embed → index) so a single command yields a fully populated RAG. Mock data is
written to read like real HR/eng docs (not lorem ipsum) so retrieval quality is demonstrable.

---

## 7. Ingestion pipeline (offline)

```text
source files ─▶ loader ─▶ normalize ─▶ chunk ─▶ embed ─▶ upsert (idempotent)
                                          │              │
                          semantic/heading-aware      pgvector + tsv
                          chunks (~512 tok, overlap 64)  (dedupe by checksum)
```

- **Idempotent**: `kb_source.checksum` skips unchanged docs; re-runs are safe.
- **Versioned**: bump `version` on change; keep old chunks until reindex swap (no read gap).
- **Metadata carried**: role, section heading, visibility, source uri → used for filtering
  and citations.
- CLI: `python -m scripts.ingest_kb --path data/kb --reindex`.

---

## 8. Retrieval pipeline (online)

```text
query ─▶ embed(query) ─┬─▶ dense: pgvector cosine top-k (HNSW), scope+visibility filter
                       └─▶ sparse: tsvector ts_rank_cd top-k
                       ▼
                  RRF fusion (top-k union)
                       ▼
             optional cross-encoder rerank → top-n
                       ▼
        assemble context (token budget, dedupe, with [cite:source#ordinal])
                       ▼
        grounded generation (Groq) OR raw chunks back to the agent tool
```

- **Scope filter** applied *in SQL* (WHERE visibility / candidate_id / role) — security is at
  the query, not post-hoc.
- **Citations** returned with every chunk so the spoken answer can be traced and logged.
- **Confidence gate**: if top score < threshold → return `insufficient_context=true`; the agent
  then **escalates** (existing `TALK_TO_HUMAN` path) instead of hallucinating.

---

## 9. Binding to Pipecat (existing agent)

Expose RAG as agent tools (added to the current `FunctionSchema` set), backed by the RAG
service over HTTP/MCP — same pattern as `take_action`/`record_answer` today:

```text
kb_answer(question)              → grounded answer from Company/Role KB (candidate-visible only)
get_candidate_context()          → summary of THIS caller's resume/application (bound candidate_id)
evaluate_current_answer(answer)  → (internal) retrieve rubric, LLM-judge score, persist via orchestrator
```

- `interview_id` / `candidate_id` remain **server-bound per connection** (as now) — the LLM
  never passes identity into RAG; the tool injects it. Preserves least-privilege.
- The candidate-facing `kb_answer` uses `visibility=candidate` only.
- `evaluate_current_answer` is called after `record_answer`; it uses `visibility=internal`
  rubrics and writes the score through the orchestrator (single-writer invariant).
- Pipecat consumes the RAG **MCP server** via its built-in `mcp_service`, so tools are declared
  once and reused.

Latency handling (voice): kick retrieval in parallel with a short filler ("Let me check that for
you…") and stream the grounded answer; target < 300 ms added (see §13).

---

## 10. Binding to LiveKit agents (new)

Two complementary additions; both reuse the *same* RAG MCP server (no duplication):

**(a) Pipecat on LiveKit transport** — swap `SmallWebRTCTransport` → Pipecat's
`LiveKitTransport`. Same pipeline, same STT/LLM/TTS, same RAG tools. Unlocks LiveKit rooms,
SIP/PSTN via LiveKit, recording, and scale-out. Selected by `TRANSPORT_PROVIDER=livekit`.

**(b) LiveKit-native "Recruiter Co-pilot" agent** — a second agent (LiveKit Agents SDK) that
joins the room during a **human handoff** and, using the same RAG MCP tools, surfaces
rubric-grounded suggestions and candidate context to the recruiter (private side-channel).

```text
                     LiveKit Room
   Candidate ──▶ ┌───────────────────────────┐
                 │  Interview Agent (Pipecat) │──┐
   Recruiter ──▶ │  Recruiter Co-pilot (LK)   │  ├─▶ RAG MCP server (shared)
                 └───────────────────────────┘──┘
```

This is exactly the multi-party scenario LiveKit was originally kept for — now with a concrete
job, and RAG serving both agents through one interface. Escalation (`TALK_TO_HUMAN`) becomes a
real recruiter join, not a placeholder.

---

## 11. Answer-evaluation RAG flow (use case B, end to end)

```text
candidate speaks answer
   ▼ record_answer (existing) → transcript persisted
   ▼ evaluate_current_answer(transcript)
        ▼ retrieve rubric for (role, current_question) [visibility=internal]
        ▼ LLM-judge: score 0–5 + rationale, grounded in rubric anchors
        ▼ orchestrator persists answer_evaluation (single writer)
   ▼ agent continues to next question (no behavioral change to candidate)
post-interview: aggregate scores → recruiter summary (spoken by co-pilot or exported)
```

Guardrail: the score/rationale are **internal** — never spoken to the candidate.

---

## 12. Guardrails & security

- **Tenant/candidate isolation**: candidate-doc retrieval filtered by bound `candidate_id` in
  SQL; verified by a test that candidate A can never retrieve candidate B's docs.
- **Visibility split**: `internal` sources (rubrics, prior feedback, model answers) are
  physically excluded from candidate-facing tools.
- **Groundedness / anti-hallucination**: answers must cite retrieved chunks; low-confidence →
  escalate. Optional post-gen faithfulness check (LLM-judge) before speaking.
- **PII hygiene**: candidate docs are access-controlled; logs store chunk *ids*, not content.
- **Prompt-injection defense**: retrieved content is treated as **data, not instructions**
  (wrapped/delimited; system prompt states retrieved text cannot change behavior).

---

## 13. Latency budget (voice-critical)

Added hops must fit the realtime loop. Targets (CPU, local):

```text
query embed (bge-small, ONNX)      ~15–30 ms
dense HNSW + sparse + RRF           ~5–20 ms
rerank (optional, bge-reranker)     ~50–150 ms   ← toggle off if over budget
context assemble                    ~5 ms
grounded generation (Groq)          reuse existing LLM turn (no extra hop)
────────────────────────────────────────────────
added latency (no rerank)           ~25–55 ms
added latency (with rerank)         ~75–200 ms
```

Techniques: parallel "filler" TTS during retrieval; cache query embeddings; precompute doc
embeddings; keep rerank optional and behind a flag. Budget goal: **< 300 ms** perceived.

---

## 14. Evaluation & observability

- **Retrieval eval**: golden query→relevant-chunk set; report `recall@k`, `MRR`, `nDCG`.
- **Generation eval**: RAGAS-style faithfulness, answer-relevance, context-precision via
  Groq LLM-judge. CI gate on regressions.
- **Ops metrics** (`rag_query_log`): latency histograms, retrieval hit-rate, escalation rate,
  per-source usage, groundedness pass-rate.
- **Golden set** lives in `tests/rag/` and runs in the existing pytest flow.

---

## 15. Phased roadmap

| Phase | Scope | Deliverable |
|---|---|---|
| **0. Foundations** | Enable `pgvector`; schema (§5); embeddings adapter (fastembed); config flags | `docker compose` builds with pgvector; embed a string end-to-end |
| **1. Mock data + ingestion** | Author corpora (§6); `ingest_kb` CLI (idempotent, versioned) | One command populates KB + candidate docs, indexed |
| **2. Retrieval service** | Hybrid retrieve + RRF (+optional rerank); REST + MCP; scope/visibility filters | `/kb/search`, `/kb/answer`, `/candidate/context` return grounded, cited results |
| **3. Pipecat binding** | `kb_answer`, `get_candidate_context` tools; filler/latency handling | Voice agent answers company/role questions correctly with citations |
| **4. Evaluation RAG** | Rubric retrieval + LLM-judge scoring; persist via orchestrator | `answer_evaluation` rows; recruiter summary export |
| **5. LiveKit binding** | Pipecat LiveKit transport; LiveKit-native Recruiter Co-pilot on shared MCP | Multi-party room; recruiter gets RAG-assisted context on handoff |
| **6. Hardening** | Guardrails, eval harness, observability, load/latency tuning | CI eval gate; dashboards; injection/isolation tests green |

Each phase is independently demoable and preserves the existing (non-RAG) voice flow.

---

## 16. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Retrieval latency hurts voice UX | Optional rerank; parallel filler; caching; strict budget (§13) |
| Hallucinated answers | Citations + confidence gate → escalate; faithfulness check |
| Candidate-data leakage | SQL-level scope filter + visibility split + isolation tests |
| Prompt injection via docs | Retrieved text delimited as data; system-prompt hardening |
| KB drift / staleness | Versioned, checksum-idempotent reindex; freshness metrics |
| Local embedding quality ceiling | Adapter allows swap to larger/hosted embeddings without code change |
| Scope creep (RAG deciding state) | Hard boundary: RAG read-only; orchestrator single writer |

---

## 17. New/changed components summary

```text
NEW   data/kb/**, data/candidates/**                mock corpora
NEW   scripts/ingest_kb.py                          ingestion CLI
NEW   app/rag/embeddings.py                          fastembed adapter (provider boundary)
NEW   app/rag/retriever.py                           hybrid retrieve + RRF + rerank
NEW   app/rag/service.py (FastAPI) + app/rag/mcp.py  REST + MCP surfaces
NEW   app/services/evaluation_service.py             rubric-grounded scoring (writes via orchestrator)
NEW   app/db/models.py (+kb_*, candidate_document, answer_evaluation, rag_query_log)
CHG   app/agent/pipeline.py                          + kb_answer / get_candidate_context / evaluate tools
NEW   app/agent/livekit_pipeline.py                  Pipecat-on-LiveKit transport variant
NEW   app/agent/recruiter_copilot.py                 LiveKit-native agent (shared MCP)
NEW   tests/rag/**                                   retrieval + generation eval, isolation tests
CHG   docker-compose.yml                             pgvector image; optional livekit service
```

---

## 18. Recommendation

Start with **Phases 0–3** (pgvector + ingestion + hybrid retrieval + Pipecat `kb_answer`).
That alone closes the most visible gap — the agent answering candidate questions accurately —
and is fully testable without LiveKit. Add **Phase 4** (scoring) for recruiter value, then
**Phase 5** (LiveKit + co-pilot) once the single-agent RAG loop is solid. The RAG service is
built transport-agnostic from day one, so the LiveKit binding is additive, not a rewrite.
