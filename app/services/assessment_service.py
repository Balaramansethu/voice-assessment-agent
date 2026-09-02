"""Role-based assessment: pre-seeded questions per role, graded live per answer.

Grading uses a weighted 0..10 rubric (client spec) and is SILENT — the score/rating are
recorded for the recruiter, never spoken to the candidate. The judge scores the answer
against the question's own expected_answer + key_points (self-contained; no RAG retrieval
needed for grading). The overall result (score/rating/pass) is aggregated from the persisted
per-answer scores and stamped onto the session at completion.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AssessmentAnswer, AssessmentSession, RoleQuestion
from app.db.session import session_scope
from app.observability.tracing import groq_client, traceable

# Client-provided evaluator spec (verbatim). {{question}} and {{candidate_answer}} are
# filled in _grade(); the question block also carries the expected answer + key points as
# grading context so the judge has a self-contained "correct answer" definition.
_GRADER_TEMPLATE = """You are a technical interview answer evaluator.

Your job is to evaluate the candidate's spoken answer against the interview question and determine how well they actually understand the concept.

IMPORTANT:
* Evaluate MEANING and technical understanding, not exact keywords.
* The candidate is answering verbally, so tolerate filler words, imperfect grammar, speech-to-text errors, abbreviations, and different terminology.
* Do not require the candidate to mention every possible detail.
* Do not penalize missing OPTIONAL concepts.
* Do not treat specific libraries/tools as mandatory unless the question explicitly asks for them.
* Penalize technically incorrect statements more heavily than missing optional details.
* A concise answer can receive a high score if it correctly covers the core concepts.
* Do not reward keyword stuffing when the surrounding explanation is technically incorrect or meaningless.

QUESTION:
{{question}}

CANDIDATE ANSWER:
{{candidate_answer}}

Evaluate the answer using these categories:
1. CORE CONCEPTS — 50%
2. TECHNICAL CORRECTNESS — 25%
3. IMPORTANT DETAILS — 15%
4. PRACTICAL APPLICATION — 10%

Scoring:
9.0–10.0 = Excellent
8.0–8.9 = Strong
7.0–7.9 = Good
5.0–6.9 = Partial
3.0–4.9 = Weak
0–2.9 = Incorrect

IMPORTANT GRADING RULE:
Separate concepts into REQUIRED / IMPORTANT / OPTIONAL. Missing OPTIONAL concepts must NOT significantly reduce the score.

Return ONLY valid JSON in this exact structure:
{"score": 0, "rating": "Excellent | Strong | Good | Partial | Weak | Incorrect", "required_covered": [], "important_covered": [], "important_missed": [], "optional_missed": [], "technical_errors": [], "reason": "One concise explanation of why this score was given.", "pass": true}

PASS RULE: "pass": true when score >= 7.0, else false."""

# Band thresholds on the 0..10 scale (client spec), highest first.
_BANDS = [(9.0, "Excellent"), (8.0, "Strong"), (7.0, "Good"),
          (5.0, "Partial"), (3.0, "Weak"), (0.0, "Incorrect")]
PASS_THRESHOLD = 7.0


def _rating_band(score: float | None) -> str | None:
    """Map a 0..10 score to its rating band name."""
    if score is None:
        return None
    for threshold, name in _BANDS:
        if score >= threshold:
            return name
    return "Incorrect"


def list_roles(session: Session) -> list[str]:
    return list(session.scalars(select(RoleQuestion.role).distinct().order_by(RoleQuestion.role)))


def resolve_role(session: Session, role_text: str) -> str | None:
    """Map a spoken role ('backend', 'the frontend role') to a seeded canonical role."""
    text = (role_text or "").strip().lower()
    if not text:
        return None
    roles = list_roles(session)
    for r in roles:                                   # exact
        if r.lower() == text:
            return r
    for r in roles:                                   # contains either way
        if text in r.lower() or r.lower().split()[0] in text:
            return r
    return None


def get_questions(session: Session, role: str) -> list[RoleQuestion]:
    canonical = resolve_role(session, role) or role
    return list(session.scalars(
        select(RoleQuestion).where(func.lower(RoleQuestion.role) == canonical.lower())
        .order_by(RoleQuestion.position)
    ))


def start_session(session: Session, role: str, *, candidate_name: str | None = None,
                  call_id: int | None = None) -> dict:
    questions = get_questions(session, role)
    if not questions:
        return {"ok": False, "message": f"No question set seeded for role '{role}'."}
    canonical_role = questions[0].role
    s = AssessmentSession(role=canonical_role, candidate_name=candidate_name, call_id=call_id)
    session.add(s)
    session.flush()
    return {"ok": True, "session_id": s.id, "role": canonical_role,
            "total_questions": len(questions),
            "first_question": {"position": questions[0].position, "prompt": questions[0].prompt}}


# A retried tool cycle fires within seconds; a genuinely similar-but-later answer arrives
# after the candidate has heard and answered the next question. 30s comfortably separates
# the two while still catching the storm-driven retry.
_DUP_WINDOW_SECONDS = 30.0


def _normalize(text: str) -> str:
    """Case-insensitive, whitespace-collapsed form used for duplicate detection."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _within_dup_window(last: AssessmentAnswer) -> bool:
    """True if `last` was inserted recently enough to treat a matching resend as a retry.
    created_at may be naive (server default) or tz-aware depending on the driver — coerce
    to UTC before subtracting so the comparison never raises."""
    ts = last.created_at
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= _DUP_WINDOW_SECONDS


def _pending(session: Session, s: AssessmentSession, questions: list) -> dict:
    """Idempotent 'where are we now' answer: the next unanswered question, or the
    summary if the set is exhausted. Never inserts or advances — used by the
    duplicate-submit and empty-transcript guards so a retry re-hands the correct
    next step instead of corrupting positions. Shape mirrors grade_answer's success
    returns so the agent can't tell a no-op retry from the original call."""
    answered = session.scalar(
        select(func.count(AssessmentAnswer.id)).where(AssessmentAnswer.session_id == s.id)
    ) or 0
    if answered >= len(questions):
        return _summary(session, s, questions)          # read-only: not just_graded
    nq = questions[answered]
    return {"ok": True, "done": False,
            "next_question": {"position": nq.position, "prompt": nq.prompt},
            "progress": f"{answered}/{len(questions)}"}


def _empty_grade(reason: str = "") -> dict:
    """Fallback grade when the model returns no parseable JSON."""
    return {"score": None, "rating": None, "passed": None, "reason": reason,
            "required_covered": [], "important_covered": [], "important_missed": [],
            "optional_missed": [], "technical_errors": []}


@traceable(run_type="chain", name="assessment.grade")
def _grade(prompt: str, expected: str, key_points: list, answer: str) -> dict:
    kp = "\n".join(f"- {k}" for k in (key_points or []))
    # The question block carries the expected answer + key points as grading context so
    # the evaluator has a self-contained definition of the "correct answer".
    question = (f"{prompt}\n\nGRADING CONTEXT — expected answer:\n{expected}\n\n"
                f"GRADING CONTEXT — key points:\n{kp}")
    content_msg = (_GRADER_TEMPLATE
                   .replace("{{question}}", question)
                   .replace("{{candidate_answer}}", answer))
    # Silent grading runs on its own model (groq_grader_model) — a reasoning model here
    # is fine and desirable, since scores are recorded for the recruiter and never
    # spoken. reasoning_format="hidden" keeps the chain-of-thought out of the JSON we
    # parse. Decoupled from the conversational model so switching the voice model (to a
    # non-reasoning one) doesn't send Groq params it would reject.
    resp = groq_client().chat.completions.create(
        model=settings.groq_grader_model,
        messages=[{"role": "user", "content": content_msg}],
        temperature=0.0,
        max_tokens=1024,
        extra_body={"reasoning_format": "hidden", "reasoning_effort": "low"},
    )
    content = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            score = float(d.get("score", 0))
            # Trust the model's score for the number, but re-derive rating/pass from our
            # own thresholds so bands stay consistent with aggregation (the backend
            # decides, never the LLM's free-text band).
            return {
                "score": score,
                "rating": _rating_band(score),
                "passed": score >= PASS_THRESHOLD,
                "reason": d.get("reason", ""),
                "required_covered": d.get("required_covered", []),
                "important_covered": d.get("important_covered", []),
                "important_missed": d.get("important_missed", []),
                "optional_missed": d.get("optional_missed", []),
                "technical_errors": d.get("technical_errors", []),
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return _empty_grade(content[:300])


@traceable(run_type="chain", name="assessment.submit_answer")
def grade_answer(session: Session, *, session_id: int, transcript: str) -> dict:
    """Fast-submit path (silent grading is OFF the conversation critical path).

    Persist the answer's transcript IMMEDIATELY with score/rating/etc. left NULL
    (ungraded) and return the next question — or the final "done" response — right away.
    The actual Groq grading (0.9–4s, reasoning model) runs in the BACKGROUND afterwards
    via `grade_pending_answer`, which the endpoint schedules through FastAPI
    BackgroundTasks. Grading is SILENT (recruiter-only), so blocking the candidate on it
    bought nothing; moving it off-path cuts ~1–4s per question and softens the impact of
    Groq daily-token throttling on the live flow.

    On a real insert the returned dict carries `_answer_id` (the row to grade) so the
    endpoint can enqueue the background task; the idempotent no-op paths omit it. The
    caller (endpoint) strips `_answer_id` before it reaches the agent — it's not part of
    the tool contract, which is still {next_question} / {done}.
    """
    s = session.get(AssessmentSession, session_id)
    if s is None:
        return {"ok": False, "message": "Unknown assessment session."}
    questions = get_questions(session, s.role)

    # Idempotency guard (defense-in-depth). A cancelled/retried LLM tool cycle can call
    # submit_answer twice with the SAME transcript; because position is derived by counting
    # rows, a naive second insert would land on the NEXT question and shift every later
    # answer (prod session 28). This is Postgres-as-idempotency, same spirit as
    # call.provider_call_id UNIQUE + ON CONFLICT DO NOTHING. The tool only sends {answer},
    # so there's no client token to dedupe on — we compare the normalized transcript against
    # the most-recently-inserted answer. Predicate: identical (case-insensitive, whitespace-
    # collapsed) to the last stored transcript AND inserted within DUP_WINDOW → treat as a
    # retry. The time window keeps a legitimately-similar answer to a LATER question (given
    # much later) from being swallowed; exact consecutive duplicates are the observed break.
    # The guard still holds under background grading: the transcript is stored on submit
    # (before grading), so a retry compares against a row that already exists.
    last = session.scalars(
        select(AssessmentAnswer).where(AssessmentAnswer.session_id == session_id)
        .order_by(AssessmentAnswer.position.desc(), AssessmentAnswer.created_at.desc())
        .limit(1)
    ).first()
    incoming = _normalize(transcript)
    # Blank/spurious turn: never insert or advance, just re-hand the pending question.
    if not incoming:
        return _pending(session, s, questions)
    if last is not None and _normalize(last.transcript) == incoming and _within_dup_window(last):
        return _pending(session, s, questions)

    answered = session.scalar(
        select(func.count(AssessmentAnswer.id)).where(AssessmentAnswer.session_id == session_id)
    ) or 0
    if answered >= len(questions):
        return _summary(session, s, questions)

    q = questions[answered]
    # Insert UNGRADED: transcript + position now, verdict fields NULL. The background
    # grader fills score/rating/reason/covered/missed/errors and (once all answers are
    # graded) stamps the session aggregate. Completion is NOT stamped here — it can only
    # be correct once every answer has a score.
    answer = AssessmentAnswer(
        session_id=session_id, question_id=q.id, position=q.position, transcript=transcript,
    )
    session.add(answer)
    # Commit the ungraded row NOW so the background grader (its own session_scope, possibly
    # a different thread) can read it. FastAPI runs BackgroundTasks before the get_session
    # dependency's own commit fires, so an uncommitted flush would be invisible to the task
    # and the answer would never get graded. Committing here also keeps the transcript
    # durable the instant we hand back the next question.
    session.commit()

    next_idx = answered + 1
    if next_idx >= len(questions):
        # Final answer: the candidate-facing "that was the last question, thanks" response
        # returns immediately. The recruiter's /summary shows the full aggregate a moment
        # later, once background grading stamps completion — partial/NULL before that.
        return {"ok": True, "done": True, "role": s.role,
                "candidate_name": s.candidate_name,
                "total": len(questions), "answered": next_idx,
                "_answer_id": answer.id}
    nq = questions[next_idx]
    # Score/rating are intentionally NOT returned to the agent for speaking — silent grading.
    return {"ok": True, "done": False,
            "next_question": {"position": nq.position, "prompt": nq.prompt},
            "progress": f"{next_idx}/{len(questions)}",
            "_answer_id": answer.id}


@traceable(run_type="chain", name="assessment.grade_pending_answer")
def grade_pending_answer(answer_id: int) -> None:
    """Background grader: grade one persisted-but-ungraded answer, then (if this was the
    last one) stamp the session aggregate. Opens its OWN DB session via `session_scope()`
    because the request session that inserted the row is already closed by the time
    FastAPI runs the background task (the response has been sent).

    Idempotent: re-running on an already-graded row (score not NULL) is a no-op, and
    completion stamping is guarded by `completed_at IS NULL` under a row lock so the last
    two grades finishing concurrently stamp exactly once.

    CAVEAT (POC, acceptable): if the process dies after the row is committed but before
    this task grades it, that answer stays ungraded (score NULL) and the session never
    stamps — BackgroundTasks are in-process, not durable. A tiny reconciliation sweep
    (grade any NULL-score answers on startup, then stamp any complete-but-unstamped
    session) would close this; not built here to avoid over-engineering the POC.
    """
    with session_scope() as session:
        answer = session.get(AssessmentAnswer, answer_id)
        if answer is None or answer.score is not None:
            return                                     # gone or already graded
        s = session.get(AssessmentSession, answer.session_id)
        if s is None:
            return
        q = session.get(RoleQuestion, answer.question_id)
        if q is None:
            return

        grade = _grade(q.prompt, q.expected_answer, q.key_points, answer.transcript)
        answer.score = grade["score"]
        answer.rating = grade["rating"]
        answer.passed = grade["passed"]
        answer.reason = grade["reason"]
        answer.required_covered = grade["required_covered"]
        answer.important_covered = grade["important_covered"]
        answer.important_missed = grade["important_missed"]
        answer.optional_missed = grade["optional_missed"]
        answer.technical_errors = grade["technical_errors"]
        session.flush()

        _maybe_stamp_completion(session, s)


def _maybe_stamp_completion(session: Session, s: AssessmentSession) -> None:
    """Stamp the session aggregate exactly once, when every answer is graded.

    Called from the background grader after each answer is scored. Row-locks the session
    (`SELECT ... FOR UPDATE`) and re-checks `completed_at IS NULL` so that when the last
    two grades finish concurrently, only one transaction stamps — the other blocks on the
    lock, then sees `completed_at` already set and returns. Uses `_summary(..., just_graded=
    True)`, the single scoring path, so the SAME 0..10 aggregate is written as before.
    """
    locked = session.scalars(
        select(AssessmentSession).where(AssessmentSession.id == s.id).with_for_update()
    ).first()
    if locked is None or locked.completed_at is not None:
        return
    questions = get_questions(session, locked.role)
    # Any ungraded answer left? Then it isn't complete yet.
    ungraded = session.scalar(
        select(func.count(AssessmentAnswer.id)).where(
            AssessmentAnswer.session_id == locked.id, AssessmentAnswer.score.is_(None))
    ) or 0
    answered = session.scalar(
        select(func.count(AssessmentAnswer.id)).where(AssessmentAnswer.session_id == locked.id)
    ) or 0
    if ungraded > 0 or answered < len(questions):
        return
    _summary(session, locked, questions, just_graded=True)


def _summary(session: Session, s: AssessmentSession, questions: list,
             just_graded: bool = False) -> dict:
    """Aggregate the persisted per-answer 0..10 scores into the overall result.

    This is the single scoring path: the numbers returned here are exactly what we stamp
    onto the session on completion. `just_graded=True` marks the last question as just
    graded, so we persist the aggregate onto the session row here (the clean completion
    spot). Read-only callers (the summary endpoint) leave the row untouched but see the
    same numbers, since they derive from assessment_answer.
    """
    rows = session.scalars(
        select(AssessmentAnswer).where(AssessmentAnswer.session_id == s.id)
        .order_by(AssessmentAnswer.position)
    ).all()
    scores = [r.score for r in rows if r.score is not None]
    overall_score = round(sum(scores) / len(scores), 1) if scores else None
    overall_rating = _rating_band(overall_score)
    passed_count = sum(1 for r in rows if (r.score or 0) >= PASS_THRESHOLD)
    overall_pass = overall_score is not None and overall_score >= PASS_THRESHOLD

    # Persist the aggregate exactly once, when the final answer has just been graded.
    # Grading is SILENT: these values are for the recruiter and never returned to the
    # agent to speak. Idempotent — re-stamping the same derived numbers is harmless.
    if just_graded and s.completed_at is None:
        s.total_questions = len(questions)
        s.answered = len(rows)
        s.passed_count = passed_count
        s.overall_score = overall_score
        s.rating = overall_rating
        s.completed_at = datetime.now(timezone.utc)
        session.flush()

    return {
        "ok": True, "done": True, "role": s.role,
        "candidate_name": s.candidate_name,
        "total": len(questions), "answered": len(rows),
        "overall_score": overall_score, "overall_rating": overall_rating,
        "passed_count": passed_count, "overall_pass": overall_pass,
        "breakdown": [{"position": r.position, "score": r.score, "rating": r.rating,
                       "pass": r.passed, "reason": r.reason,
                       "important_missed": r.important_missed,
                       "technical_errors": r.technical_errors} for r in rows],
    }
