"""Rubric-grounded answer evaluation. Retrieves the INTERNAL rubric for a question
and uses an LLM-as-judge to score the candidate's answer 0-5 with rationale.

Internal only — scores/rationale are never spoken to the candidate. This writes
`answer_evaluation` audit rows; it never transitions interview state (that stays
the orchestrator's job).
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.observability.tracing import groq_client, traceable
from app.db.models import (
    AnswerEvaluation,
    InterviewAnswer,
    InterviewQuestion,
    KBChunk,
    KBSource,
)
from app.rag import retriever

_JUDGE_SYSTEM = (
    "You are a strict but fair technical interviewer. Score the candidate's answer "
    "from 0 to 5 using ONLY the rubric's score anchors and signals. Reward reasoning "
    "and trade-offs; do not reward buzzwords. Respond with STRICT JSON: "
    '{"score": <number 0-5>, "rationale": "<one or two sentences>"}.'
)


@traceable(run_type="chain", name="rag.judge_answer")
def _judge(question: str, answer: str, rubric_text: str) -> dict:
    user = (f"RUBRIC:\n{rubric_text}\n\nQUESTION: {question}\n\n"
            f"CANDIDATE ANSWER: {answer}\n\nScore it.")
    resp = groq_client().chat.completions.create(
        model=settings.groq_llm_model,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        # gpt-oss is a reasoning model; give it room for reasoning + the JSON,
        # and keep reasoning light so latency/tokens stay bounded.
        max_tokens=1024,
        extra_body={"reasoning_effort": "low"},
    )
    content = resp.choices[0].message.content or ""
    # Lenient parse: extract the first {...} JSON object from the reply.
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return {"score": float(data.get("score")), "rationale": data.get("rationale", "")}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"score": None, "rationale": content[:300]}


@traceable(run_type="chain", name="evaluation.evaluate_interview")
def evaluate_interview(session: Session, interview_id: int, *, force: bool = False) -> dict:
    """Score every answered question in the interview (idempotent unless force)."""
    rows = session.execute(
        select(InterviewAnswer, InterviewQuestion)
        .join(InterviewQuestion, InterviewQuestion.id == InterviewAnswer.question_id)
        .where(InterviewAnswer.interview_id == interview_id)
        .order_by(InterviewQuestion.position)
    ).all()

    results = []
    for answer, question in rows:
        existing = session.scalar(
            select(AnswerEvaluation).where(
                AnswerEvaluation.interview_id == interview_id,
                AnswerEvaluation.question_id == question.id,
            )
        )
        if existing and not force:
            results.append({"position": question.position, "score": existing.score,
                            "rationale": existing.rationale, "cached": True})
            continue

        rubric_chunks = retriever.get_rubric_for_question(session, question.prompt_text)
        rubric_text = "\n\n".join(c.content for c in rubric_chunks)
        rubric_source_id = _rubric_source_id(session, rubric_chunks)

        verdict = _judge(question.prompt_text, answer.transcript, rubric_text)

        if existing:
            existing.score = verdict["score"]
            existing.rationale = verdict["rationale"]
            existing.rubric_source_id = rubric_source_id
            existing.model = settings.groq_llm_model
            existing.retrieved_context = {"chunk_ids": [c.chunk_id for c in rubric_chunks]}
        else:
            session.add(AnswerEvaluation(
                interview_id=interview_id, question_id=question.id, answer_id=answer.id,
                rubric_source_id=rubric_source_id, score=verdict["score"],
                rationale=verdict["rationale"], model=settings.groq_llm_model,
                retrieved_context={"chunk_ids": [c.chunk_id for c in rubric_chunks]},
            ))
        session.flush()
        results.append({"position": question.position, "question": question.prompt_text,
                        "score": verdict["score"], "rationale": verdict["rationale"],
                        "cached": False})

    scored = [r["score"] for r in results if r.get("score") is not None]
    return {
        "interview_id": interview_id,
        "evaluations": results,
        "average_score": round(sum(scored) / len(scored), 2) if scored else None,
        "scored_count": len(scored),
    }


def _rubric_source_id(session: Session, chunks) -> int | None:
    if not chunks:
        return None
    return session.scalar(
        select(KBSource.id).join(KBChunk, KBChunk.source_id == KBSource.id)
        .where(KBChunk.id == chunks[0].chunk_id)
    )
