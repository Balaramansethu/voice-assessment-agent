"""Twilio webhook guard.

Twilio signs every request with an HMAC-SHA1 `X-Twilio-Signature` over the exact
public URL + sorted POST params, keyed by the account auth token. This endpoint
validates that signature and only forwards genuine Twilio calls to the agent —
closing the "anyone can drive the agent" hole on the public webhook. The agent's
media WebSocket (`/ws`) is separately protected by the runner's one-time tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
from fastapi import APIRouter, Request, Response

from app.config import settings

router = APIRouter(tags=["telephony"])

AGENT_URL = "http://agent:7860/"   # internal; returns the TwiML with the WSS stream


def _valid_signature(url: str, params: dict[str, str], signature: str, token: str) -> bool:
    """Twilio's algorithm: sign(url + concat(sorted key+value pairs)) with the auth token."""
    if not token or not signature:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


@router.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    """Guarded Voice webhook. Caddy routes the public POST here; on a valid Twilio
    signature it proxies to the agent and returns its TwiML, else 403."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    # Twilio signs the public URL it hit — the domain root, not the internal path.
    public_url = f"https://{settings.public_host}/"

    if not _valid_signature(public_url, params, signature, settings.twilio_auth_token):
        return Response(status_code=403, content="Forbidden")

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(AGENT_URL, data=params)
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "text/xml"),
                    status_code=r.status_code)
