"""Seed a demo candidate + interview. Run: docker compose exec api python -m scripts.seed"""
from app.db.session import SessionLocal, init_db
from app.services import scenario_service


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        info = scenario_service.build(session, "no-answer")
        session.commit()
        print("Seeded scenario:", info)
    finally:
        session.close()


if __name__ == "__main__":
    main()
