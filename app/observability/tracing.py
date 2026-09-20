"""LangSmith tracing bootstrap.

Importing this module configures LangSmith from settings. Tracing is active only
when `langsmith_tracing` is true AND an API key is present — otherwise `traceable`
is a graceful no-op and the traced Groq client behaves like a plain client.

Every server-side step decorates with `@traceable(...)`; every LLM call goes
through `groq_client()` (an OpenAI SDK client pointed at Groq, wrapped by LangSmith)
so token usage, latency, model, and errors are captured automatically.
"""
from __future__ import annotations

import dataclasses
import os
import re

from app.config import settings

TRACING_ENABLED = bool(settings.langsmith_tracing and settings.langsmith_api_key)

# Opt-in, production-only content redaction. Outside production, full capture is
# unchanged (existing dev-debugging behavior). In production, capture defaults OFF
# (settings.langsmith_capture_content=False); when off, PII/content-shaped fields are
# replaced before being serialized to LangSmith, never sent raw.
#
# IMPORTANT: this is a plain module global, read by BARE NAME inside _redact_for_trace
# below — never captured as a default-argument value or closure variable. langsmith
# captures process_inputs/process_outputs as function-object REFERENCES at each
# @traceable(...) call site's decoration time — but the referenced function's BODY
# still resolves bare-name globals fresh on every call, against this module's live
# __dict__. That's what makes a monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED",
# ...) in a test work on an ALREADY-decorated real function with no reload needed.
CONTENT_CAPTURE_ENABLED = settings.app_env.strip().lower() != "production" or settings.langsmith_capture_content

_PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{7,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Parameter/field names carrying candidate PII, transcripts, resumes, or internal
# rubric content across every @traceable-decorated function in this codebase.
# Matched case-insensitively against dict keys and dataclass field names, at any
# nesting depth.
_REDACT_KEYS = {
    "transcript", "answer", "candidate_answer", "expected", "expected_answer",
    "rubric", "rubric_text", "reason", "rationale", "content",
    "raw_phone", "phone", "email", "candidate_name", "resume", "grading_data",
    # Rubric-grader breakdown fields (assessment_service._grade/_summary) — these
    # carry the internal answer key and paraphrased fragments of the candidate's
    # spoken answer, not just a bare score.
    "key_points", "required_covered", "important_covered", "important_missed",
    "optional_missed", "technical_errors",
}


def _redact_value(value):
    """Recursively redact strings/dicts/lists/dataclasses. Never mutates in place —
    dataclasses.replace() returns a NEW instance, since process_outputs receives the
    real return value and a caller downstream may still use the original object after
    this hook runs."""
    if isinstance(value, str):
        value = _PHONE_RE.sub("[REDACTED_PHONE]", value)
        value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        return value
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _REDACT_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.replace(value, **{
            f.name: (
                "[REDACTED]" if f.name.lower() in _REDACT_KEYS
                else _redact_value(getattr(value, f.name))
            )
            for f in dataclasses.fields(value)
        })
    return value


def _redact_for_trace(payload):
    """process_inputs/process_outputs hook, injected as a default on every traceable()
    call below. CONTENT_CAPTURE_ENABLED and _redact_value are read by bare name here."""
    if CONTENT_CAPTURE_ENABLED:
        return payload
    return _redact_value(payload)

if TRACING_ENABLED:
    # LangSmith SDK reads these env vars. Set them before anything traced runs.
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    # legacy aliases some versions still read
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith_project)
else:
    os.environ["LANGSMITH_TRACING"] = "false"


try:
    from langsmith import traceable as _traceable
    from langsmith import trace as _trace  # context manager
except Exception:  # langsmith not installed → no-op shims
    def _traceable(*d_args, **d_kwargs):
        def deco(fn):
            return fn
        # support both @traceable and @traceable(...)
        if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
            return d_args[0]
        return deco

    class _trace:  # type: ignore
        def __init__(self, *a, **k):
            self._meta = k.get("metadata") or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False


# Public re-exports
def traceable(*d_args, **d_kwargs):
    """Wraps langsmith.traceable (or its no-op shim) to redact PII/content fields by
    default. Every @traceable(...) call site in this codebase already passes kwargs
    (run_type=, name=), never the bare @traceable form, so that no-op-shim branch is
    unaffected by always adding these two kwargs."""
    d_kwargs.setdefault("process_inputs", _redact_for_trace)
    d_kwargs.setdefault("process_outputs", _redact_for_trace)
    return _traceable(*d_args, **d_kwargs)

trace = _trace


_groq_singleton = None


def groq_client():
    """OpenAI SDK client pointed at Groq, wrapped by LangSmith when tracing is on.
    LLM calls made through it appear in LangSmith as `llm` runs with token usage."""
    global _groq_singleton
    if _groq_singleton is not None:
        return _groq_singleton
    from openai import OpenAI
    client = OpenAI(api_key=settings.groq_api_key,
                    base_url="https://api.groq.com/openai/v1")
    if TRACING_ENABLED:
        try:
            import warnings
            with warnings.catch_warnings():
                # Importing langsmith.wrappers triggers an unrelated deprecation
                # warning from its _openai_agents submodule (which we don't use).
                warnings.simplefilter("ignore", DeprecationWarning)
                from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
        except Exception:
            pass
    _groq_singleton = client
    return client
