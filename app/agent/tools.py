"""Agent tool implementations. These are thin HTTP clients to the FastAPI control
plane, so the orchestrator remains the single owner of state transitions. The LLM
calls these; it never touches the database."""
from __future__ import annotations

import os

import httpx

API_BASE = os.getenv("API_BASE", "http://api:8000")


async def resolve_caller(from_number: str | None = None, identifier: str | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post("/candidates/resolve",
                         json={"phone": from_number, "identifier": identifier})
        return r.json()


async def open_inbound(provider_call_id: str, from_number: str, transport: str = "webrtc") -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post("/calls/inbound",
                         json={"provider_call_id": provider_call_id,
                               "from_number": from_number, "transport": transport})
        return r.json()


async def identify_candidate(name: str, call_id: int | None = None) -> dict:
    """Resolve the caller by spoken name and attach them to the call."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post("/calls/identify", json={"name": name, "call_id": call_id})
        return r.json()


async def take_action(interview_id: int, intent: str, call_id: int | None = None) -> dict:
    """intent ∈ CONTINUE_INTERVIEW | START_INTERVIEW | RESCHEDULE | TALK_TO_HUMAN"""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post("/interviews/action",
                         json={"interview_id": interview_id, "intent": intent,
                               "call_id": call_id})
        return r.json()


async def record_answer(interview_id: int, transcript: str, call_id: int | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post(f"/interviews/{interview_id}/answer",
                         json={"transcript": transcript, "call_id": call_id})
        return r.json()


async def start_assessment(role: str, candidate_name: str | None = None,
                           call_id: int | None = None) -> dict:
    """Begin a role-based assessment; returns the first question + session id."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as c:
        r = await c.post("/assessment/start",
                         json={"role": role, "candidate_name": candidate_name, "call_id": call_id})
        return r.json()


async def submit_answer(session_id: int, transcript: str) -> dict:
    """Grade the current answer silently; returns the next question or the summary."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=20) as c:
        r = await c.post("/assessment/grade",
                         json={"session_id": session_id, "transcript": transcript})
        return r.json()


async def kb_answer(query: str, role: str | None = None, session_id: str | None = None) -> dict:
    """Grounded answer to a candidate's question from the company/role KB."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as c:
        r = await c.post("/rag/kb/answer",
                         json={"query": query, "role": role, "session_id": session_id})
        return r.json()


async def get_candidate_context(candidate_id: int, query: str = "background experience") -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as c:
        r = await c.post("/rag/candidate/context",
                         json={"candidate_id": candidate_id, "query": query})
        return r.json()


async def complete(interview_id: int) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post(f"/interviews/{interview_id}/complete")
        return r.json()


async def mark_interrupted(interview_id: int, call_id: int | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as c:
        r = await c.post(f"/interviews/{interview_id}/interrupt")
        return r.json()


# Tool schemas exposed to the LLM (OpenAI/Groq function-calling format).
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "take_action",
            "description": "Ask the backend to start, continue, reschedule, or escalate the "
                           "interview. The backend validates and returns a message to speak.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interview_id": {"type": "integer"},
                    "intent": {
                        "type": "string",
                        "enum": ["CONTINUE_INTERVIEW", "START_INTERVIEW",
                                 "RESCHEDULE", "TALK_TO_HUMAN"],
                    },
                },
                "required": ["interview_id", "intent"],
            },
        },
    },
]
