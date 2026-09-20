"""PII/content redaction for LangSmith trace payloads (PR-013). No importlib.reload —
CONTENT_CAPTURE_ENABLED and _redact_value are read as bare module globals at call
time inside _redact_for_trace, so monkeypatch.setattr(tracing, ...) is visible even to
an ALREADY-decorated real function from a different module (proven below against the
real assessment_service._grade, not a toy function)."""
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langsmith")

from app.observability import tracing
from app.services import assessment_service
from app.services.candidate_resolver import resolve_by_phone


@dataclasses.dataclass
class _FakeChunk:
    chunk_id: int
    content: str
    title: str


def _client_with_content(content):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


# ---- _redact_value: pure unit coverage ----

def test_redact_value_passes_through_when_capture_enabled(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", True)
    payload = {"transcript": "candidate said something private", "score": 7}
    assert tracing._redact_for_trace(payload) == payload


def test_redact_value_redacts_named_categories(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {
        "answer": "x", "candidate_answer": "x", "expected_answer": "x",
        "rubric": "x", "rubric_text": "x", "reason": "x", "rationale": "x",
        "phone": "x", "email": "x", "candidate_name": "x", "resume": "x",
        "grading_data": "x", "content": "x", "unrelated_field": "keep me",
    }
    redacted = tracing._redact_for_trace(payload)
    for key in payload:
        if key == "unrelated_field":
            assert redacted[key] == "keep me"
        else:
            assert redacted[key] == "[REDACTED]"


def test_redact_value_redacts_rubric_grader_breakdown_fields(monkeypatch):
    """assessment_service._grade's/_summary's breakdown fields carry the internal
    answer key and paraphrased candidate-answer fragments, not just a bare score —
    these must be redacted like any other content field."""
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {
        "key_points": ["candidate mentioned X"],
        "required_covered": ["point A"],
        "important_covered": ["point B"],
        "important_missed": ["point C, paraphrased from the answer"],
        "optional_missed": ["point D"],
        "technical_errors": ["said Y instead of Z"],
        "score": 7.5,
    }
    redacted = tracing._redact_for_trace(payload)
    for key in payload:
        if key == "score":
            assert redacted[key] == 7.5
        else:
            assert redacted[key] == "[REDACTED]"


def test_redact_value_key_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"Transcript": "x", "EMAIL": "x", "Candidate_Name": "x"}
    redacted = tracing._redact_for_trace(payload)
    assert all(v == "[REDACTED]" for v in redacted.values())


def test_redact_value_scrubs_phone_and_email_in_unmatched_free_text(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"note": "call me at 555-123-4567 or a@b.com"}
    redacted = tracing._redact_for_trace(payload)
    assert "555-123-4567" not in redacted["note"]
    assert "a@b.com" not in redacted["note"]


def test_redact_value_recurses_two_levels_deep(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"outer": {"middle": {"transcript": "secret", "position": 3}}}
    redacted = tracing._redact_for_trace(payload)
    assert redacted["outer"]["middle"]["transcript"] == "[REDACTED]"
    assert redacted["outer"]["middle"]["position"] == 3


def test_redact_value_wholesale_replaces_a_dict_under_a_redact_key(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"transcript": {"nested": "still sensitive", "position": 1}}
    redacted = tracing._redact_for_trace(payload)
    assert redacted["transcript"] == "[REDACTED]"


@pytest.mark.parametrize("value", [None, 42, True, 3.14])
def test_redact_value_redacts_non_string_values_under_a_redact_key(monkeypatch, value):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"transcript": value}
    assert tracing._redact_for_trace(payload) == {"transcript": "[REDACTED]"}


def test_redact_value_handles_empty_dict(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    assert tracing._redact_for_trace({}) == {}


def test_redact_value_recurses_into_lists(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    payload = {"items": [{"transcript": "nested candidate text"}, "plain string"]}
    redacted = tracing._redact_for_trace(payload)
    assert redacted["items"][0]["transcript"] == "[REDACTED]"
    assert redacted["items"][1] == "plain string"


def test_redact_value_handles_dataclasses_without_mutating_the_original(monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    chunk = _FakeChunk(chunk_id=1, content="sensitive resume text", title="Resume")
    redacted = tracing._redact_for_trace(chunk)
    assert redacted.content == "[REDACTED]"
    assert redacted.title == "Resume"
    assert chunk.content == "sensitive resume text"
    assert redacted is not chunk


# ---- opt-in gate ----

def test_capture_enabled_outside_production_by_default():
    assert tracing.CONTENT_CAPTURE_ENABLED is True


# ---- proves the wiring actually reaches a REAL, already-decorated call site ----

def test_traceable_redacts_real_grader_call_site_without_reload(monkeypatch):
    spy_calls = []

    def spy_redact_value(value):
        spy_calls.append(value)
        return {"redacted": True}

    monkeypatch.setattr(tracing, "CONTENT_CAPTURE_ENABLED", False)
    monkeypatch.setattr(tracing, "_redact_value", spy_redact_value)

    content = json.dumps({
        "score": 7.0, "rating": "Good",
        "required_covered": [], "important_covered": [], "important_missed": [],
        "optional_missed": [], "technical_errors": [],
        "reason": "a real reason string", "pass": True,
    })
    monkeypatch.setattr(assessment_service, "groq_client",
                        lambda: _client_with_content(content))

    grade = assessment_service._grade("q", "expected", [], "candidate real answer text")

    assert len(spy_calls) >= 1
    assert any(isinstance(c, dict) and c.get("reason") == "a real reason string" for c in spy_calls)
    assert grade["score"] == 7.0
    assert grade["reason"] == "a real reason string"


# ---- resolve_by_phone's explicit override: honest, direct coverage ----

def test_resolve_by_phone_output_override_reduces_candidate_to_found_and_id():
    override = resolve_by_phone.__traceable_config__["process_outputs"]

    assert override(None) == {"found": False, "candidate_id": None}

    fake_candidate = MagicMock(id=42)
    assert override(fake_candidate) == {"found": True, "candidate_id": 42}


def test_resolve_by_phone_process_inputs_defaults_to_the_generic_redactor():
    config = resolve_by_phone.__traceable_config__
    assert config["process_inputs"] is tracing._redact_for_trace
    assert config["process_outputs"] is not tracing._redact_for_trace
