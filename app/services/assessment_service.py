"""Role-based assessment: pre-seeded questions per role, graded live per answer.

Grading is GRADED (correct / partial / incorrect + covered/missing key points) and
SILENT — the verdict is recorded, never spoken to the candidate. The judge scores the
answer against the question's own expected_answer + key_points (self-contained; no RAG
retrieval needed for grading).
"""
from __future__ import annotations

import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AssessmentAnswer, AssessmentSession, RoleQuestion
from app.observability.tracing import groq_client, traceable

_GRADER_SYSTEM = (
    "You are a fair technical grader. Grade the candidate's spoken answer against the "
    "expected answer and key points. Be graded, not binary: award 'correct' if it covers "
    "most key points, 'partial' if some, 'incorrect' if few/none or it's wrong. Ignore "
    "phrasing and speech disfluencies; judge substance. Respond with STRICT JSON: "
    '{"verdict": "correct|partial|incorrect", "score": <0..1>, '
    '"covered": ["..."], "missing": ["..."], "rationale": "<one sentence>"}.'
)


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


@traceable(run_type="chain", name="assessment.grade")
def _grade(prompt: str, expected: str, key_points: list, answer: str) -> dict:
    kp = "\n".join(f"- {k}" for k in (key_points or []))
    user = (f"QUESTION: {prompt}\n\nEXPECTED ANSWER: {expected}\n\nKEY POINTS:\n{kp}\n\n"
            f"CANDIDATE ANSWER: {answer}\n\nGrade it.")
    # Silent grading runs on its own model (groq_grader_model) — a reasoning model
    # here is fine and desirable, since verdicts are recorded for the recruiter and
    # never spoken. reasoning_format="hidden" keeps the chain-of-thought out of the
    # JSON we parse. Decoupled from the conversational model so switching the voice
    # model (to a non-reasoning one) doesn't send Groq params it would reject.
    resp = groq_client().chat.completions.create(
        model=settings.groq_grader_model,
        messages=[{"role": "system", "content": _GRADER_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=1024,
        extra_body={"reasoning_format": "hidden", "reasoning_effort": "low"},
    )
    content = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            return {"verdict": d.get("verdict"), "score": float(d.get("score", 0)),
                    "covered": d.get("covered", []), "missing": d.get("missing", []),
                    "rationale": d.get("rationale", "")}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"verdict": None, "score": None, "covered": [], "missing": [],
            "rationale": content[:300]}


@traceable(run_type="chain", name="assessment.grade_and_advance")
def grade_answer(session: Session, *, session_id: int, transcript: str) -> dict:
    """Grade the current question's answer (silently) and return the next question,
    or a final summary when the set is exhausted."""
    s = session.get(AssessmentSession, session_id)
    if s is None:
        return {"ok": False, "message": "Unknown assessment session."}
    questions = get_questions(session, s.role)

    answered = session.scalar(
        select(func.count(AssessmentAnswer.id)).where(AssessmentAnswer.session_id == session_id)
    ) or 0
    if answered >= len(questions):
        return _summary(session, s, questions)

    q = questions[answered]
    grade = _grade(q.prompt, q.expected_answer, q.key_points, transcript)
    session.add(AssessmentAnswer(
        session_id=session_id, question_id=q.id, position=q.position, transcript=transcript,
        verdict=grade["verdict"], score=grade["score"],
        covered=grade["covered"], missing=grade["missing"], rationale=grade["rationale"],
    ))
    session.flush()

    next_idx = answered + 1
    if next_idx >= len(questions):
        return _summary(session, s, questions, just_graded=True)
    nq = questions[next_idx]
    # Verdict is intentionally NOT returned to the agent for speaking — silent grading.
    return {"ok": True, "done": False,
            "next_question": {"position": nq.position, "prompt": nq.prompt},
            "progress": f"{next_idx}/{len(questions)}"}


def _summary(session: Session, s: AssessmentSession, questions: list,
             just_graded: bool = False) -> dict:
    rows = session.scalars(
        select(AssessmentAnswer).where(AssessmentAnswer.session_id == s.id)
        .order_by(AssessmentAnswer.position)
    ).all()
    correct = sum(1 for r in rows if r.verdict == "correct")
    partial = sum(1 for r in rows if r.verdict == "partial")
    avg = round(sum((r.score or 0) for r in rows) / len(rows), 2) if rows else None
    return {
        "ok": True, "done": True, "role": s.role,
        "total": len(questions), "answered": len(rows),
        "correct": correct, "partial": partial,
        "incorrect": len(rows) - correct - partial, "average_score": avg,
        "breakdown": [{"position": r.position, "verdict": r.verdict, "score": r.score,
                       "missing": r.missing} for r in rows],
    }
