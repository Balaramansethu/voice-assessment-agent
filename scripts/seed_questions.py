"""Seed per-role assessment question banks from data/questions/*.json.

    docker compose exec api python -m scripts.seed_questions
"""
import json
from pathlib import Path

from sqlalchemy import delete

from app.db.models import RoleQuestion
from app.db.session import SessionLocal, init_db


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        total = 0
        for path in sorted(Path("data/questions").glob("*.json")):
            data = json.loads(path.read_text())
            role = data["role"]
            # idempotent: replace this role's questions
            session.execute(delete(RoleQuestion).where(RoleQuestion.role == role))
            for pos, q in enumerate(data["questions"], start=1):
                session.add(RoleQuestion(
                    role=role, position=pos, prompt=q["prompt"],
                    expected_answer=q["expected_answer"], key_points=q.get("key_points", []),
                    difficulty=q.get("difficulty", "medium"),
                ))
                total += 1
            print(f"  {role}: {len(data['questions'])} questions")
        session.commit()
        print(f"Seeded {total} questions across roles.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
