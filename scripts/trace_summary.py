"""Print a LangSmith trace summary using the current (non-deprecated) SDK API.

    docker compose exec api python -m scripts.trace_summary
"""
import asyncio
import json

from app.observability import query


async def main() -> None:
    summary = await query.summarize_runs(limit=200)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
