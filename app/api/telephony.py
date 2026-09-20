"""Twilio webhook guard + one-use voice-session token minting.

Twilio signs every request with an HMAC-SHA1 X-Twilio-Signature over the exact
public URL + sorted POST params, keyed by the account auth token. This endpoint
validates that signature, applies the PR-021 rate/concurrency gates, and — on
success — mints a one-use, short-TTL voice-session token and builds the
<Connect><Stream> TwiML directly (no more proxying to the agent for TwiML).
The agent's media WebSocket (/ws) redeems that token via
POST /telephony/session/consume before it ever builds a pipeline — see
app/agent/pipeline.py.

We keep the HMAC check hand-rolled rather than adding the `twilio` PyPI
package: it implements exactly Twilio's own algorithm (HMAC-SHA1 over
url + sorted concatenated key+value pairs, base64, constant-time compare),
and this repo's production-readiness TODO explicitly allows "match it with
exhaustive proxy/URL tests" as the alternative to the maintained library —
see tests/integration/test_telephony.py for that suite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.schemas import ConsumeSessionRequest
from app.config import settings
from app.db.session import get_session, session_scope
from app.domain.states import CallDirection, CallStatus
from app.services import call_service as cs
from app.services import interview_service as iv
from app.services import voice_session_service as vs

router = APIRouter(tags=["telephony"])

_PHONE_FIELD_MAX_LEN = 32  # matches Call.from_number/to_number and
                           # VoiceSessionToken.from_number/to_number column width


def _fit_phone_field(value: str | None) -> str | None:
    """Truncate to the DB column width. Real Twilio E.164 numbers never hit
    this; SIP-trunk URIs (sip:+1234567890@sip.trunking.twilio.com) can exceed
    it — Postgres raises StringDataRightTruncation rather than truncating
    itself, so we do it here instead of crashing the webhook. No schema
    change (no migration tooling yet). Applied once, here, at the FIRST point
    these values are ever written (mint time)."""
    if value is None:
        return None
    return value[:_PHONE_FIELD_MAX_LEN]


def _valid_signature(url: str, params: dict[str, str], signature: str, token: str) -> bool:
    """Twilio's algorithm: sign(url + concat(sorted key+value pairs)) with the auth token."""
    if not token or not signature:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _reject_twiml() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>',
        media_type="text/xml",
    )


def _stream_twiml(token: str) -> Response:
    wss_url = escape(f"wss://{settings.public_host}/ws?token={token}")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
          f'<Response><Connect><Stream url="{wss_url}"/></Connect></Response>')
    return Response(content=xml, media_type="text/xml")


def _handle_twilio_voice(params: dict[str, str], signature: str) -> Response:
    """Runs in a threadpool (see twilio_voice below): all Postgres work here is
    sync, off the event loop, per this repo's sync-endpoint convention. Uses
    session_scope() directly rather than FastAPI's Depends(get_session) because
    this function is invoked from a plain run_in_threadpool call, outside
    FastAPI's DI."""
    public_url = f"https://{settings.public_host}/"
    if not _valid_signature(public_url, params, signature, settings.twilio_auth_token):
        return Response(status_code=403, content="Forbidden")

    call_sid = params.get("CallSid")
    from_number = _fit_phone_field(params.get("From"))
    to_number = _fit_phone_field(params.get("To"))
    if not call_sid or not from_number:
        return Response(status_code=400, content="Bad Request")

    try:
        with session_scope() as session:
            # Serializes the ceiling-check + rate-limit-check + mint sequence across ALL
            # concurrent webhook requests. Without this, two things race: (1) a burst of
            # concurrent calls can all see the same stale ACTIVE count and all mint,
            # bypassing max_concurrent_calls entirely (Call rows for Twilio don't exist
            # until /telephony/session/consume runs, well after minting — so the ceiling
            # has nothing to see during the mint-time check for concurrent bursts); (2) two
            # requests for the same CallSid (a real Twilio webhook retry) can both pass
            # mint_token's invalidate-then-insert sequence before either commits, hitting
            # the partial unique index and raising an uncaught IntegrityError -> 500. A
            # fixed-key advisory lock, scoped to this transaction (auto-released on
            # commit/rollback), makes this whole section atomic relative to every other
            # concurrent call to this function — cheap, since it's held only for these few
            # DB statements, not the full webhook round-trip.
            # Key is arbitrary but memorable: bytes 56 4F 49 43 45 5F 53 spell
            # "VOICE_S" in ASCII — just this module's fixed lock id, not decoded
            # anywhere at runtime.
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x564F4943_45_5F53})
            if cs.at_concurrency_ceiling(session):
                iv.record_event(session, event_type="VOICE_SESSION_THROTTLED",
                                payload={"reason": "concurrency_ceiling", "from_number": from_number})
                return _reject_twiml()
            if vs.rate_limited(session, from_number=from_number):
                iv.record_event(session, event_type="VOICE_SESSION_THROTTLED",
                                payload={"reason": "rate_limit", "from_number": from_number})
                return _reject_twiml()
            token = vs.mint_token(session, provider_call_id=call_sid,
                                  from_number=from_number, to_number=to_number)
    except IntegrityError:
        return _reject_twiml()
    return _stream_twiml(token)


@router.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    """Guarded Voice webhook. `await request.form()` is the one part Starlette
    forces async; everything else (signature check, PR-021 gates, minting) is
    sync Postgres work and runs in a threadpool via run_in_threadpool, rather
    than blocking the event loop — see _handle_twilio_voice."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    return await run_in_threadpool(_handle_twilio_voice, params, signature)


@router.post("/telephony/session/consume")
def consume_session(body: ConsumeSessionRequest, session: Session = Depends(get_session)) -> dict:
    """Atomically redeem a one-use voice-session token at WebSocket
    establishment (PR-019). Called by app.agent.pipeline.bot() — via
    app.agent.tools.consume_voice_session() — as the very first thing it does
    for a Twilio connection, before create_transport() builds anything."""
    claims = vs.consume_token(session, body.token)
    if claims is None:
        iv.record_event(session, event_type="VOICE_SESSION_TOKEN_REJECTED")
        return JSONResponse({"ok": False}, status_code=403)

    call = cs.ingest_call(
        session,
        provider_call_id=claims.provider_call_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.ACTIVE,
        transport="twilio",
        from_number=claims.from_number,
        to_number=claims.to_number,
    )
    try:
        cs.set_status(session, call, CallStatus.ACTIVE)
    except IntegrityError:
        iv.record_event(session, event_type="VOICE_SESSION_REJECTED",
                        payload={"reason": "interview_already_owned_by_active_call"})
        return JSONResponse({"ok": False}, status_code=403)
    vs.bind_call(session, claims.token_id, call.id)
    iv.record_event(session, event_type="VOICE_SESSION_CONSUMED", call_id=call.id,
                    payload={"provider_call_id": claims.provider_call_id})
    return {"ok": True, "call_id": call.id, "provider_call_id": claims.provider_call_id,
            "from_number": claims.from_number, "to_number": claims.to_number}
