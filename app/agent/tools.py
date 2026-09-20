"""Agent tool implementations. These are thin HTTP clients to the FastAPI control
plane, so the orchestrator remains the single owner of state transitions. The LLM
calls these; it never touches the database."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

API_BASE = os.getenv("API_BASE", "http://api:8000")
AGENT_SHARED_KEY = os.getenv("AGENT_SHARED_KEY", "dev-agent-key-change-me")
_AGENT_HEADERS = {"X-Agent-Key": AGENT_SHARED_KEY}

_RETRYABLE_STATUS = {502, 503, 504}


async def _post(path: str, json: dict, timeout: float, retries: int = 1) -> dict:
    """POST to the api with one retry on a transient failure (connection error, timeout,
    or 502/503/504) and a short backoff — and NEVER raises. A tool handler that raises
    leaves the LLM's function call hanging with no result_callback; every endpoint this
    module calls is idempotent at the DB level (grade_answer's transcript-dedup guard,
    the orchestrator's transition validation under a row lock, ON CONFLICT DO NOTHING
    call intake), so a retried POST cannot duplicate an effect — see CLAUDE.md's
    architecture invariants."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=timeout) as c:
                r = await c.post(path, json=json, headers=_AGENT_HEADERS)
            if r.status_code in _RETRYABLE_STATUS and attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                logger.warning("tool call %s -> HTTP %s", path, r.status_code)
                return {"ok": False, "error": f"http_{r.status_code}"}
            return r.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
    logger.warning("tool call %s failed after retries: %s", path, last_exc)
    return {"ok": False, "error": "provider_unreachable"}


async def open_inbound(provider_call_id: str, from_number: str, transport: str = "webrtc") -> dict:
    return await _post("/calls/inbound",
                       json={"provider_call_id": provider_call_id,
                             "from_number": from_number, "transport": transport},
                       timeout=10)


async def take_action(interview_id: int, intent: str, call_id: int) -> dict:
    """intent ∈ CONTINUE_INTERVIEW | START_INTERVIEW | RESCHEDULE | TALK_TO_HUMAN"""
    return await _post("/interviews/action",
                       json={"interview_id": interview_id, "intent": intent,
                             "call_id": call_id},
                       timeout=10)


async def record_answer(interview_id: int, transcript: str, call_id: int) -> dict:
    return await _post(f"/interviews/{interview_id}/answer",
                       json={"transcript": transcript, "call_id": call_id},
                       timeout=10)


async def start_assessment(role: str, candidate_name: str | None = None,
                           call_id: int | None = None) -> dict:
    """Begin a role-based assessment; returns the first question + session id."""
    return await _post("/assessment/start",
                       json={"role": role, "candidate_name": candidate_name, "call_id": call_id},
                       timeout=15)


async def submit_answer(session_id: int, call_id: int, transcript: str) -> dict:
    """Grade the current answer silently; returns the next question or the summary."""
    return await _post("/assessment/grade",
                       json={"session_id": session_id, "call_id": call_id, "transcript": transcript},
                       timeout=20)


async def kb_answer(query: str, role: str | None = None, session_id: str | None = None) -> dict:
    """Grounded answer to a candidate's question from the company/role KB."""
    return await _post("/rag/kb/answer",
                       json={"query": query, "role": role, "session_id": session_id},
                       timeout=15)


async def get_candidate_context(call_id: int, query: str = "background experience") -> dict:
    return await _post("/rag/candidate/context",
                       json={"call_id": call_id, "query": query},
                       timeout=15)


async def verify_invitation_code(call_id: int, code: str) -> dict:
    return await _post("/calls/verify_invitation",
                       json={"call_id": call_id, "code": code},
                       timeout=10)


async def complete(interview_id: int, call_id: int) -> dict:
    return await _post(f"/interviews/{interview_id}/complete",
                       json={"call_id": call_id},
                       timeout=10)


async def mark_interrupted(interview_id: int, call_id: int) -> dict:
    return await _post(f"/interviews/{interview_id}/interrupt",
                       json={"call_id": call_id},
                       timeout=10)


async def consume_voice_session(token: str) -> dict:
    """Redeem a one-use Twilio voice-session token. Returns {"ok": False} for
    any invalid/expired/replayed token or non-200 response — never raises for
    that case — so bot() can reject cleanly with no provider usage."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post("/telephony/session/consume", json={"token": token},
                         headers=_AGENT_HEADERS)
        if r.status_code != 200:
            return {"ok": False}
        return r.json()


async def close_call(call_id: int, status: str = "DISCONNECTED") -> dict:
    return await _post(f"/calls/{call_id}/end",
                       json={"status": status},
                       timeout=10)
