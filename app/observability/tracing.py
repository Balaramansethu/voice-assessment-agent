"""LangSmith tracing bootstrap.

Importing this module configures LangSmith from settings. Tracing is active only
when `langsmith_tracing` is true AND an API key is present — otherwise `traceable`
is a graceful no-op and the traced Groq client behaves like a plain client.

Every server-side step decorates with `@traceable(...)`; every LLM call goes
through `groq_client()` (an OpenAI SDK client pointed at Groq, wrapped by LangSmith)
so token usage, latency, model, and errors are captured automatically.
"""
from __future__ import annotations

import os

from app.config import settings

TRACING_ENABLED = bool(settings.langsmith_tracing and settings.langsmith_api_key)

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
traceable = _traceable
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
