"""Deterministic candidate resolution. Never LLM-guessed."""
from __future__ import annotations

import re

import phonenumbers
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Candidate, Interview
from app.observability.tracing import traceable


def normalize_phone(raw: str, default_region: str = "IN") -> str:
    """Return E.164, or the stripped input if it can't be parsed."""
    try:
        parsed = phonenumbers.parse(raw, default_region)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return raw.strip()


@traceable(
    run_type="tool", name="candidate.resolve_by_phone",
    process_outputs=lambda c: {"found": c is not None, "candidate_id": c.id if c else None},
)
def resolve_by_phone(session: Session, raw_phone: str) -> Candidate | None:
    phone = normalize_phone(raw_phone)
    return session.scalar(select(Candidate).where(Candidate.phone == phone))


def resolve_by_identifier(session: Session, identifier: str) -> Candidate | None:
    """Recruiter-tool fallback: email only. A free-text 'identifier' must never
    double as a raw ID-based lookup."""
    ident = (identifier or "").strip().lower()
    return session.scalar(select(Candidate).where(Candidate.email == ident)) if ident else None


def resolve_by_invitation_code(session: Session, code: str) -> Interview | None:
    """Resolves the exact Interview a code was issued for (codes are per-interview,
    not per-candidate — this also settles multi-interview ambiguity)."""
    normalized = re.sub(r"[\s-]", "", (code or "")).upper()
    if not normalized:
        return None
    return session.scalar(select(Interview).where(Interview.invitation_code == normalized))


def resolve_by_name(session: Session, name: str) -> Candidate | None:
    """Resolve a candidate by spoken name. Exact (case-insensitive) first, then a
    contains-match on the first name. POC-grade — production would verify identity
    with a second factor (application id, DOB) before revealing interview details."""
    name = (name or "").strip()
    if not name:
        return None
    exact = session.scalar(
        select(Candidate).where(func.lower(Candidate.name) == name.lower())
    )
    if exact:
        return exact
    first = name.split()[0]
    return session.scalar(
        select(Candidate).where(Candidate.name.ilike(f"{first}%")).order_by(Candidate.id)
    )
