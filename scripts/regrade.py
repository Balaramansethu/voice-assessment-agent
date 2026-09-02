"""Re-grade a stored assessment session under the NEW weighted 0..10 rubric.

Loads a session's persisted AssessmentAnswer transcripts + their RoleQuestions, re-runs
the new grader on each answer, and prints an old-vs-new table (old 0..1 score/verdict vs
new 0..10 score/rating/pass) plus the new overall. Read-only: it does NOT persist the
re-graded values — it only reports what the new rubric would produce.

    docker compose exec api python -m scripts.regrade <session_id>
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.db.models import AssessmentAnswer, AssessmentSession, RoleQuestion
from app.db.session import session_scope
from app.services import assessment_service as asv


def _fmt(value: object) -> str:
    return "-" if value is None else str(value)


def regrade(session_id: int) -> int:
    with session_scope() as session:
        s = session.get(AssessmentSession, session_id)
        if s is None:
            print(f"Unknown assessment session {session_id}.")
            return 1

        rows = session.scalars(
            select(AssessmentAnswer).where(AssessmentAnswer.session_id == session_id)
            .order_by(AssessmentAnswer.position)
        ).all()
        if not rows:
            print(f"Session {session_id} ({s.role}) has no answers to re-grade.")
            return 1

        # Map position -> question so we can supply expected_answer + key_points.
        questions = {q.position: q for q in asv.get_questions(session, s.role)}

        print(f"Session {session_id}  role={s.role}  answers={len(rows)}\n")
        header = f"{'pos':>3}  {'OLD score/verdict':<22}  {'NEW score/rating (pass)':<28}"
        print(header)
        print("-" * len(header))

        new_scores: list[float] = []
        for r in rows:
            q = questions.get(r.position)
            if q is None:                       # question set drifted since grading
                print(f"{r.position:>3}  (no matching question — skipped)")
                continue
            g = asv._grade(q.prompt, q.expected_answer, q.key_points, r.transcript)
            if g["score"] is not None:
                new_scores.append(g["score"])

            old = f"{_fmt(r.score):<6} {_fmt(r.verdict)}"
            new = f"{_fmt(g['score']):<5} {_fmt(g['rating'])} ({_fmt(g['passed'])})"
            print(f"{r.position:>3}  {old:<22}  {new:<28}")

        overall = round(sum(new_scores) / len(new_scores), 1) if new_scores else None
        rating = asv._rating_band(overall)
        passed_count = sum(1 for x in new_scores if x >= asv.PASS_THRESHOLD)
        overall_pass = overall is not None and overall >= asv.PASS_THRESHOLD
        print("-" * len(header))
        print(f"NEW overall: {_fmt(overall)}/10  rating={_fmt(rating)}  "
              f"passed={passed_count}/{len(new_scores)}  overall_pass={overall_pass}")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.regrade <session_id>")
        raise SystemExit(2)
    try:
        session_id = int(sys.argv[1])
    except ValueError:
        print("session_id must be an integer")
        raise SystemExit(2)
    raise SystemExit(regrade(session_id))


if __name__ == "__main__":
    main()
