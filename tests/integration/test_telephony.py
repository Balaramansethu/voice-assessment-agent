"""Signed-webhook + voice-session-token negative/positive tests (PR-015..022),
over a real Postgres. Run: docker compose exec api pytest tests/integration/test_telephony.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

import app.main as main_module
from app.config import settings
from app.db.models import Call, VoiceSessionToken
from app.db.session import SessionLocal, init_db
from app.domain.states import CallDirection, CallStatus
from app.services import call_service as cs
from app.services import voice_session_service as vs

client = TestClient(main_module.app)

_TWILIO_PARAMS = {
    "CallSid": "CADEFAULT0000000000000000000000",
    "From": "+15550000000",
    "To": "+15551111111",
    "CallStatus": "ringing",
}

# Per-test-session uniqueness to prevent Call.provider_call_id collisions and
# rate-limit accumulation across reruns. Each pytest invocation gets a unique suffix.
_RUN_ID = os.urandom(4).hex()  # 8-char hex from 4 random bytes


def _session():
    init_db()
    return SessionLocal()


@pytest.fixture(autouse=True)
def _clean_concurrency_state():
    """`at_concurrency_ceiling()` counts ALL genuinely-ACTIVE Call rows and live
    VoiceSessionToken rows in the whole (never-truncated) test database, not just this
    test's own. Several tests here monkeypatch `max_concurrent_calls` down to a small
    number (even 1) to exercise the ceiling/reaper logic in isolation — any ACTIVE row
    left over by an unrelated test (this file's own, or another file's, or a real
    selftest/worker run against this same dev Postgres) permanently breaks them. Reset
    to a clean baseline before every test in this file so each one's ceiling assertions
    reflect only the rows it creates itself.

    A bulk UPDATE like this can itself hang forever if some OTHER test anywhere left a
    raw session open on one of these rows without committing/closing (a known, recurring
    pattern in this test suite — a bare `SessionLocal()` that never calls `.close()`).
    Bounded with `lock_timeout`, same defensive reasoning as
    `app/db/session.py::_apply_lightweight_migrations`: best-effort cleanup that gives up
    quickly rather than turning one stray leaked session into a permanent suite hang."""
    s = _session()
    try:
        s.execute(text("SET LOCAL lock_timeout = '2s'"))
        s.execute(update(Call).where(Call.status == "ACTIVE").values(status="COMPLETED"))
        s.execute(update(VoiceSessionToken).where(VoiceSessionToken.consumed_at.is_(None))
                  .values(consumed_at=datetime.now(timezone.utc)))
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()
    yield


def _sign(url: str, params: dict, token: str) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "public_host", "example.com")
    monkeypatch.setattr(settings, "twilio_auth_token", "testtoken")


def _extract_token(twiml: str) -> str:
    root = ET.fromstring(twiml)
    stream_url = root.find("./Connect/Stream").attrib["url"]
    return parse_qs(urlsplit(stream_url).query)["token"][0]


_AGENT_HEADERS = {"X-Agent-Key": settings.agent_shared_key}
# NOTE: /twilio/voice is intentionally NOT gated by this — its auth is the Twilio
# HMAC signature check (a stronger, per-request signal than a shared secret), per
# the P1 endpoint-auth table. Only /telephony/session/consume and /calls/{id}/end
# require the agent scope header.


def _expire_token_row(session, row: VoiceSessionToken) -> None:
    """Mark an already-fetched, deliberately-unconsumed test token as consumed and
    commit. Otherwise it counts as a live reserved slot in at_concurrency_ceiling
    and pollutes other tests/reruns within the token TTL window."""
    row.consumed_at = datetime.now(timezone.utc)
    session.commit()


def _expire_tokens_for_callsid(session, provider_call_id_pattern: str) -> None:
    """Bulk form of _expire_token_row: expires every token minted under a CallSid
    (plain value or a LIKE pattern) that a test deliberately left unconsumed."""
    session.execute(
        update(VoiceSessionToken)
        .where(VoiceSessionToken.provider_call_id.like(provider_call_id_pattern))
        .values(consumed_at=datetime.now(timezone.utc))
    )
    session.commit()


def test_webhook_missing_signature_rejected(monkeypatch):
    _configure(monkeypatch)
    resp = client.post("/twilio/voice", data=_TWILIO_PARAMS)
    assert resp.status_code == 403


def test_webhook_wrong_signature_rejected(monkeypatch):
    _configure(monkeypatch)
    resp = client.post("/twilio/voice", data=_TWILIO_PARAMS,
                       headers={"X-Twilio-Signature": "bogus"})
    assert resp.status_code == 403


def test_webhook_tampered_param_rejected(monkeypatch):
    _configure(monkeypatch)
    sig = _sign("https://example.com/", _TWILIO_PARAMS, "testtoken")
    tampered = {**_TWILIO_PARAMS, "From": "+19999999999"}
    resp = client.post("/twilio/voice", data=tampered,
                       headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 403


def test_webhook_signed_for_wrong_host_rejected(monkeypatch):
    _configure(monkeypatch)
    sig = _sign("https://attacker.example/", _TWILIO_PARAMS, "testtoken")
    resp = client.post("/twilio/voice", data=_TWILIO_PARAMS,
                       headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 403


def test_webhook_missing_callsid_rejected_as_bad_request(monkeypatch):
    _configure(monkeypatch)
    params = {"From": "+15550000000", "To": "+15551111111"}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params,
                       headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 400


def test_webhook_valid_signature_mints_usable_token(monkeypatch):
    _configure(monkeypatch)
    params = {**_TWILIO_PARAMS, "CallSid": f"CAVALID001{_RUN_ID}", "From": f"+1555123{_RUN_ID[:4]}"}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params,
                       headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200
    token = _extract_token(resp.text)

    s = _session()
    claims = vs.consume_token(s, token)
    s.commit()
    assert claims is not None
    assert claims.provider_call_id == f"CAVALID001{_RUN_ID}"


def test_minted_token_round_trips_through_stream_twiml_url(monkeypatch):
    _configure(monkeypatch)
    params = {**_TWILIO_PARAMS, "CallSid": f"CAROUNDTRIP1{_RUN_ID}", "From": f"+1555456{_RUN_ID[:4]}"}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    token_from_url = _extract_token(resp.text)

    s = _session()
    claims = vs.consume_token(s, token_from_url)
    s.commit()
    assert claims is not None
    assert claims.provider_call_id == f"CAROUNDTRIP1{_RUN_ID}"


def test_webhook_rate_limited_returns_reject_not_403(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "max_calls_per_number_per_minute", 0)
    params = {**_TWILIO_PARAMS, "CallSid": f"CATHROTTLE1{_RUN_ID}", "From": "+15559998888"}
    sig = _sign("https://example.com/", params, "testtoken")

    s = _session()
    before = s.scalar(select(func.count(VoiceSessionToken.id))
                      .where(VoiceSessionToken.from_number == params["From"]))

    resp = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200
    assert "<Reject" in resp.text

    after = s.scalar(select(func.count(VoiceSessionToken.id))
                     .where(VoiceSessionToken.from_number == params["From"]))
    assert after == before


def test_webhook_concurrency_ceiling_returns_reject_not_403(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "max_concurrent_calls", 0)
    monkeypatch.setattr(settings, "max_calls_per_number_per_minute", 1000)
    params = {**_TWILIO_PARAMS, "CallSid": f"CACEIL1{_RUN_ID}", "From": "+15557776666"}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200
    assert "<Reject" in resp.text


def test_at_concurrency_ceiling_reaps_stale_active_calls_as_failed(monkeypatch):
    monkeypatch.setattr(settings, "max_call_duration_seconds", 60)
    monkeypatch.setattr(settings, "max_concurrent_calls", 1)
    s = _session()
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=200)
    stale_call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CASTALE1{_RUN_ID}",
                      transport="twilio", created_at=stale_cutoff)
    s.add(stale_call); s.commit()

    assert cs.at_concurrency_ceiling(s) is False
    s.commit()  # end_call()/at_concurrency_ceiling() don't commit (caller does) —
                # a separate session below needs the reap to actually be visible.

    s2 = _session()
    refreshed = s2.get(Call, stale_call.id)
    assert refreshed.status == CallStatus.FAILED.value
    assert refreshed.ended_at is not None


def test_at_concurrency_ceiling_does_not_reap_a_genuinely_recent_active_call(monkeypatch):
    monkeypatch.setattr(settings, "max_call_duration_seconds", 900)
    monkeypatch.setattr(settings, "max_concurrent_calls", 1)
    s = _session()
    fresh_call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CAFRESH1{_RUN_ID}",
                      transport="twilio")
    s.add(fresh_call); s.commit()

    assert cs.at_concurrency_ceiling(s) is True

    s2 = _session()
    refreshed = s2.get(Call, fresh_call.id)
    assert refreshed.status == CallStatus.ACTIVE.value
    assert refreshed.ended_at is None

    # Cleanup: this row is deliberately left ACTIVE for the assertions above, but
    # a lingering ACTIVE row would otherwise pollute later runs' concurrency-ceiling
    # checks against the real (unpatched) max_concurrent_calls default.
    cs.end_call(s2, refreshed, CallStatus.COMPLETED)
    s2.commit()


def test_multiple_tokens_same_callsid_converge_on_one_call_row():
    s = _session()
    raw_a = vs.mint_token(s, provider_call_id=f"CADUP1{_RUN_ID}", from_number="+1", to_number="+2")
    raw_b = vs.mint_token(s, provider_call_id=f"CADUP1{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()

    assert vs.consume_token(s, raw_a) is None
    claims = vs.consume_token(s, raw_b)
    s.commit()
    assert claims is not None
    assert claims.provider_call_id == f"CADUP1{_RUN_ID}"

    call = cs.ingest_call(s, provider_call_id=claims.provider_call_id,
                          direction=CallDirection.INBOUND, status=CallStatus.ACTIVE,
                          transport="twilio", from_number=claims.from_number,
                          to_number=claims.to_number)
    s.commit()

    count = s.scalar(select(func.count(Call.id)).where(Call.provider_call_id == f"CADUP1{_RUN_ID}"))
    assert count == 1
    assert call.status == CallStatus.ACTIVE.value

    cs.end_call(s, call, CallStatus.COMPLETED)  # avoid polluting later runs' ceiling
    s.commit()


def test_webhook_truncates_oversized_from_number(monkeypatch):
    _configure(monkeypatch)
    oversized = f"sip:+123{_RUN_ID}@sip.trunking.twilio.com"  # unique per run, gets truncated
    params = {**_TWILIO_PARAMS, "CallSid": f"CASIP1{_RUN_ID}", "From": oversized}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200

    s = _session()
    row = s.scalar(select(VoiceSessionToken).where(VoiceSessionToken.provider_call_id == f"CASIP1{_RUN_ID}"))
    assert row is not None
    assert len(row.from_number) <= 32
    assert row.from_number == oversized[:32]

    # This token is deliberately never consumed by this test — but a live
    # (unconsumed, unexpired) token now correctly counts toward
    # at_concurrency_ceiling's reserved-slot check, so leaving it live would
    # pollute the ceiling for any test run within the next TTL window.
    _expire_token_row(s, row)


def test_rate_limited_window_boundary_via_injected_clock(monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(vs, "_now", lambda: base)
    monkeypatch.setattr(settings, "max_calls_per_number_per_minute", 1)
    s = _session()
    vs.mint_token(s, provider_call_id=f"CAWIN1{_RUN_ID}", from_number="+15550001111", to_number="+2")
    s.commit()

    monkeypatch.setattr(vs, "_now", lambda: base + timedelta(seconds=59))
    assert vs.rate_limited(s, from_number="+15550001111") is True

    monkeypatch.setattr(vs, "_now", lambda: base + timedelta(seconds=61))
    assert vs.rate_limited(s, from_number="+15550001111") is False


def test_consume_token_expiry_boundary_via_injected_clock(monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(vs, "_now", lambda: base)
    monkeypatch.setattr(settings, "voice_session_token_ttl_seconds", 30)
    s = _session()
    raw = vs.mint_token(s, provider_call_id=f"CAEXPB1{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()

    monkeypatch.setattr(vs, "_now", lambda: base + timedelta(seconds=29))
    assert vs.consume_token(s, raw) is not None
    s.commit()  # consume_token's UPDATE must be committed, or its row stays locked


def test_consume_token_rejects_past_expiry_via_injected_clock(monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(vs, "_now", lambda: base)
    monkeypatch.setattr(settings, "voice_session_token_ttl_seconds", 30)
    s = _session()
    raw = vs.mint_token(s, provider_call_id=f"CAEXPB2{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()

    monkeypatch.setattr(vs, "_now", lambda: base + timedelta(seconds=31))
    assert vs.consume_token(s, raw) is None
    s.commit()


def test_mint_token_never_persists_the_raw_value():
    s = _session()
    raw = vs.mint_token(s, provider_call_id=f"CARAW1{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()
    row = s.scalar(select(VoiceSessionToken).where(VoiceSessionToken.provider_call_id == f"CARAW1{_RUN_ID}"))
    assert row.token_digest != raw
    assert len(row.token_digest) == 64

    # Never consumed by this test — expire it so it doesn't count as a live
    # reserved slot toward at_concurrency_ceiling for later runs.
    _expire_token_row(s, row)


def test_consume_token_rejects_unknown_token():
    s = _session()
    assert vs.consume_token(s, "not-a-real-token") is None
    s.commit()


def test_consume_token_rejects_replay():
    s = _session()
    raw = vs.mint_token(s, provider_call_id=f"CAREPLAY1{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()
    first = vs.consume_token(s, raw)
    s.commit()
    assert first is not None
    second = vs.consume_token(s, raw)
    s.commit()
    assert second is None


def test_consume_token_concurrent_double_consume_race():
    s = _session()
    raw = vs.mint_token(s, provider_call_id=f"CARACE1{_RUN_ID}", from_number="+1", to_number="+2")
    s.commit()

    def _try_consume():
        session = SessionLocal()
        try:
            claims = vs.consume_token(session, raw)
            session.commit()
            return claims
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [f.result() for f in [ex.submit(_try_consume) for _ in range(2)]]

    assert sum(1 for r in results if r is not None) == 1


def test_full_flow_creates_exactly_one_call_bound_to_twilio_identity(monkeypatch):
    _configure(monkeypatch)
    params = {**_TWILIO_PARAMS, "CallSid": f"CAFULLFLOW1{_RUN_ID}", "From": f"+1555999{_RUN_ID[:4]}"}
    sig = _sign("https://example.com/", params, "testtoken")
    resp = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    token = _extract_token(resp.text)

    consume = client.post("/telephony/session/consume", json={"token": token}, headers=_AGENT_HEADERS)
    assert consume.status_code == 200
    body = consume.json()
    assert body["ok"] is True
    assert body["provider_call_id"] == f"CAFULLFLOW1{_RUN_ID}"
    assert body["from_number"] == f"+1555999{_RUN_ID[:4]}"

    replay = client.post("/telephony/session/consume", json={"token": token}, headers=_AGENT_HEADERS)
    assert replay.status_code == 403
    assert replay.json() == {"ok": False}

    s = _session()
    call = s.scalar(select(Call).where(Call.provider_call_id == f"CAFULLFLOW1{_RUN_ID}"))
    assert call is not None
    assert call.status == CallStatus.ACTIVE.value
    assert call.transport == "twilio"

    cs.end_call(s, call, CallStatus.COMPLETED)  # avoid polluting later runs' ceiling
    s.commit()


def test_consume_rejects_missing_and_malformed_token_body():
    resp = client.post("/telephony/session/consume", json={"token": ""}, headers=_AGENT_HEADERS)
    assert resp.status_code == 422
    resp2 = client.post("/telephony/session/consume", json={"token": "garbage-not-minted"}, headers=_AGENT_HEADERS)
    assert resp2.status_code == 403
    assert resp2.json() == {"ok": False}


def test_concurrency_ceiling_holds_under_a_true_concurrent_burst(monkeypatch):
    """Reproduces the TOCTOU gap: before the advisory-lock fix, a burst of concurrent
    webhook requests could all mint tokens because Call rows for Twilio don't exist
    until /telephony/session/consume runs, well after minting."""
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "max_concurrent_calls", 2)
    monkeypatch.setattr(settings, "max_calls_per_number_per_minute", 1000)

    def _call(i):
        params = {**_TWILIO_PARAMS, "CallSid": f"CABURST{i}{_RUN_ID}", "From": f"+1555000{i:04d}"}
        sig = _sign("https://example.com/", params, "testtoken")
        return client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = [f.result() for f in [ex.submit(_call, i) for i in range(6)]]

    minted = sum(1 for r in results if "<Stream" in r.text)
    rejected = sum(1 for r in results if "<Reject" in r.text)
    assert minted <= 2
    assert minted + rejected == 6

    # These tokens are deliberately never consumed — expire them now so they
    # don't count toward at_concurrency_ceiling's reserved-slot check for any
    # test run within the next TTL window.
    _expire_tokens_for_callsid(_session(), f"CABURST%{_RUN_ID}")


def test_same_callsid_concurrent_retry_never_500s(monkeypatch):
    """Reproduces the Twilio-webhook-retry race: two concurrent requests for the SAME
    CallSid must never raise an uncaught IntegrityError -> 500; each must get a clean
    200 (either a real <Stream> or a <Reject>)."""
    _configure(monkeypatch)
    same_sid = f"CARETRY{_RUN_ID}"
    params = {**_TWILIO_PARAMS, "CallSid": same_sid, "From": "+15554440000"}
    sig = _sign("https://example.com/", params, "testtoken")

    def _call():
        return client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [f.result() for f in [ex.submit(_call) for _ in range(2)]]

    assert all(r.status_code == 200 for r in results)
    assert all(("<Stream" in r.text) or ("<Reject" in r.text) for r in results)

    # Whatever token(s) got minted here are deliberately never consumed —
    # expire them now so they don't pollute later runs' concurrency ceiling.
    _expire_tokens_for_callsid(_session(), same_sid)


def test_end_call_stamps_terminal_status_and_timestamp():
    s = _session()
    call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CAEND1{_RUN_ID}", transport="twilio")
    s.add(call); s.commit()
    resp = client.post(f"/calls/{call.id}/end", json={"status": "COMPLETED"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["ended_at"] is not None


def test_end_call_is_idempotent_sequential():
    s = _session()
    call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CAEND2{_RUN_ID}", transport="twilio")
    s.add(call); s.commit()
    r1 = client.post(f"/calls/{call.id}/end", json={"status": "DISCONNECTED"}, headers=_AGENT_HEADERS)
    first_ended_at = r1.json()["ended_at"]
    r2 = client.post(f"/calls/{call.id}/end", json={"status": "FAILED"}, headers=_AGENT_HEADERS)
    assert r2.json()["ended_at"] == first_ended_at
    assert r2.json()["status"] == "DISCONNECTED"


def test_end_call_concurrent_race_exactly_one_consistent_terminal_state():
    s = _session()
    call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CARACEEND1{_RUN_ID}",
               transport="twilio")
    s.add(call); s.commit()
    call_id = call.id

    barrier = threading.Barrier(2)

    def _end(status):
        session = SessionLocal()
        c = session.get(Call, call_id)
        barrier.wait()
        cs.end_call(session, c, CallStatus[status])
        session.commit()
        session.close()

    t1 = threading.Thread(target=_end, args=("DISCONNECTED",))
    t2 = threading.Thread(target=_end, args=("FAILED",))
    t1.start(); t2.start(); t1.join(); t2.join()

    s2 = _session()
    final = s2.get(Call, call_id)
    assert final.status in ("DISCONNECTED", "FAILED")
    assert final.ended_at is not None


def test_end_call_rejects_invalid_status():
    s = _session()
    call = Call(direction="INBOUND", status="ACTIVE", provider_call_id=f"CAEND3{_RUN_ID}", transport="twilio")
    s.add(call); s.commit()
    resp = client.post(f"/calls/{call.id}/end", json={"status": "NOT_A_STATUS"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 422

    # The invalid request never reached end_call, so this row is still ACTIVE —
    # clean it up so it doesn't pollute later runs' concurrency-ceiling checks.
    cs.end_call(s, call, CallStatus.COMPLETED)
    s.commit()


def test_end_call_404_for_missing_call():
    resp = client.post("/calls/999999999/end", json={"status": "COMPLETED"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 404


def test_end_call_rejects_absurdly_large_id():
    resp = client.post("/calls/99999999999999/end", json={"status": "COMPLETED"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 422


def test_end_call_404_for_zero_id():
    resp = client.post("/calls/0/end", json={"status": "COMPLETED"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 404


def test_end_call_404_for_negative_id():
    resp = client.post("/calls/-1/end", json={"status": "COMPLETED"}, headers=_AGENT_HEADERS)
    assert resp.status_code == 404
