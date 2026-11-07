"""groq_client respects LangSmith tracing and content-capture flags."""

import sys
from types import ModuleType

from app.observability import tracing


class _WrappedClient:
    def __init__(self, original):
        self.original = original


def _install_fake_openai(monkeypatch):
    openai = ModuleType("openai")
    created = []

    class FakeCompletions:
        def create(self, **kwargs):
            return kwargs

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = FakeChat()
            created.append(self)

    openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", openai)
    return created


def _install_fake_wrap_openai(monkeypatch, *, accepts_callbacks):
    langsmith = ModuleType("langsmith")
    langsmith.__path__ = []
    wrappers = ModuleType("langsmith.wrappers")
    calls = []

    if accepts_callbacks:

        def wrap_openai(client, *, process_inputs=None, process_outputs=None):
            wrapped = _WrappedClient(client)
            calls.append({
                "client": client,
                "process_inputs": process_inputs,
                "process_outputs": process_outputs,
                "wrapped": wrapped,
            })
            return wrapped

    else:

        def wrap_openai(client):
            wrapped = _WrappedClient(client)
            calls.append({"client": client, "wrapped": wrapped})
            return wrapped

    wrappers.wrap_openai = wrap_openai
    langsmith.wrappers = wrappers
    monkeypatch.setitem(sys.modules, "langsmith", langsmith)
    monkeypatch.setitem(sys.modules, "langsmith.wrappers", wrappers)
    return calls


def _install_kwargs_only_capturing_wrap_openai(monkeypatch):
    langsmith = ModuleType("langsmith")
    langsmith.__path__ = []
    wrappers = ModuleType("langsmith.wrappers")
    calls = []
    captured_messages = []

    class CapturingCompletions:
        def __init__(self, original):
            self._original = original

        def create(self, **kwargs):
            captured_messages.append(kwargs.get("messages"))
            return self._original.create(**kwargs)

    class CapturingChat:
        def __init__(self, original):
            self.completions = CapturingCompletions(original.completions)

    class CapturingWrappedClient(_WrappedClient):
        def __init__(self, original):
            super().__init__(original)
            self.chat = CapturingChat(original.chat)

    def wrap_openai(client, **kwargs):
        wrapped = CapturingWrappedClient(client)
        calls.append({"client": client, "kwargs": kwargs, "wrapped": wrapped})
        return wrapped

    wrappers.wrap_openai = wrap_openai
    langsmith.wrappers = wrappers
    monkeypatch.setitem(sys.modules, "langsmith", langsmith)
    monkeypatch.setitem(sys.modules, "langsmith.wrappers", wrappers)
    return calls, captured_messages


def test_tracing_off_does_not_call_wrap_openai(monkeypatch):
    created = _install_fake_openai(monkeypatch)
    calls = _install_fake_wrap_openai(monkeypatch, accepts_callbacks=True)
    monkeypatch.setattr(tracing, "TRACING_ENABLED", False)
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    monkeypatch.setattr(tracing, "_groq_singleton", None)

    client = tracing.groq_client()

    assert client is created[0]
    assert calls == []


def test_tracing_on_capture_off_skips_kwargs_only_wrapper_that_ignores_callbacks(monkeypatch):
    created = _install_fake_openai(monkeypatch)
    calls, captured_messages = _install_kwargs_only_capturing_wrap_openai(monkeypatch)
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    monkeypatch.setattr(tracing, "_groq_singleton", None)

    client = tracing.groq_client()
    messages = [{"role": "user", "content": "raw candidate answer"}]
    client.chat.completions.create(messages=messages)

    assert client is created[0]
    assert calls == []
    assert captured_messages == []


def test_tracing_on_capture_on_wraps_without_redaction_callbacks(monkeypatch):
    created = _install_fake_openai(monkeypatch)
    calls = _install_fake_wrap_openai(monkeypatch, accepts_callbacks=True)
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", True)
    monkeypatch.setattr(tracing, "_groq_singleton", None)

    client = tracing.groq_client()

    assert len(calls) == 1
    assert calls[0]["client"] is created[0]
    assert calls[0]["process_inputs"] is None
    assert calls[0]["process_outputs"] is None
    assert client is calls[0]["wrapped"]


def test_tracing_on_capture_off_wraps_with_redaction_callbacks_when_supported(monkeypatch):
    created = _install_fake_openai(monkeypatch)
    calls = _install_fake_wrap_openai(monkeypatch, accepts_callbacks=True)
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    monkeypatch.setattr(tracing, "_groq_singleton", None)

    client = tracing.groq_client()

    assert len(calls) == 1
    assert calls[0]["client"] is created[0]
    assert calls[0]["process_inputs"] is tracing._redact_for_trace
    assert calls[0]["process_outputs"] is tracing._redact_for_trace
    assert client is calls[0]["wrapped"]


def test_tracing_on_capture_off_skips_wrapper_without_callback_support(monkeypatch):
    created = _install_fake_openai(monkeypatch)
    calls = _install_fake_wrap_openai(monkeypatch, accepts_callbacks=False)
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    monkeypatch.setattr(tracing, "_groq_singleton", None)

    client = tracing.groq_client()

    assert client is created[0]
    assert calls == []
