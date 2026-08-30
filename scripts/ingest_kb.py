"""Ingest the KB + candidate corpora into Postgres/pgvector.

    docker compose exec api python -m scripts.ingest_kb            # incremental
    docker compose exec api python -m scripts.ingest_kb --reindex  # force re-embed
"""
import sys

from app.db.session import SessionLocal, init_db
from app.rag.ingest import ingest_dir


def main() -> None:
    reindex = "--reindex" in sys.argv
    root = "data"
    init_db()
    session = SessionLocal()
    try:
        results = ingest_dir(session, root, reindex=reindex)
        session.commit()
        ingested = [r for r in results if r.get("status") == "ingested"]
        unchanged = [r for r in results if r.get("status") == "unchanged"]
        print(f"Ingested {len(ingested)} file(s), {len(unchanged)} unchanged.")
        for r in results:
            print(" ", r)
    finally:
        session.close()


if __name__ == "__main__":
    main()
