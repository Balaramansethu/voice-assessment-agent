"""Request-size validation for assessment transcripts and RAG queries."""

import pytest
from pydantic import ValidationError

from app.api.assessment import GradeRequest, StartRequest
from app.api.rag import CandidateContextRequest, KBAnswerRequest, KBSearchRequest


@pytest.mark.parametrize("transcript", ["", "x" * 12_001, 123])
def test_grade_request_rejects_invalid_transcript(transcript):
    with pytest.raises(ValidationError):
        GradeRequest(session_id=1, call_id=1, transcript=transcript)


def test_grade_request_accepts_bounded_transcript():
    body = GradeRequest(session_id=1, call_id=1, transcript="x" * 12_000)
    assert len(body.transcript) == 12_000


def test_grade_request_accepts_whitespace_only_transcript():
    """Whitespace-only is a legitimate STT-silence artifact for this field only —
    assessment_service.grade_answer's blank-transcript branch handles it, not 422."""
    body = GradeRequest(session_id=1, call_id=1, transcript="   ")
    assert body.transcript == "   "


@pytest.mark.parametrize("request_type", [
    KBSearchRequest,
    KBAnswerRequest,
    lambda **kwargs: CandidateContextRequest(call_id=1, **kwargs),
])
@pytest.mark.parametrize("query", ["", "x" * 2_001, 123])
def test_rag_requests_reject_invalid_query(request_type, query):
    with pytest.raises(ValidationError):
        request_type(query=query)


@pytest.mark.parametrize("request_type", [
    KBSearchRequest,
    KBAnswerRequest,
    lambda **kwargs: CandidateContextRequest(call_id=1, **kwargs),
])
def test_rag_requests_accept_bounded_query(request_type):
    body = request_type(query="x" * 2_000)
    assert len(body.query) == 2_000


@pytest.mark.parametrize("role", ["", "   ", "x" * 201])
def test_start_request_rejects_invalid_role(role):
    with pytest.raises(ValidationError):
        StartRequest(role=role, call_id=1)


def test_start_request_accepts_bounded_role_and_strips_candidate_name():
    assert len(StartRequest(role="x" * 200, call_id=1).role) == 200
    body = StartRequest(role="Backend Engineer", candidate_name="  Grace Hopper  ", call_id=1)
    assert body.candidate_name == "Grace Hopper"


def test_start_request_rejects_invalid_candidate_name():
    with pytest.raises(ValidationError):
        StartRequest(role="Backend Engineer", candidate_name="x" * 201, call_id=1)


def test_start_request_treats_blank_candidate_name_as_none():
    assert StartRequest(role="Backend Engineer", candidate_name="", call_id=1).candidate_name is None
    assert StartRequest(role="Backend Engineer", candidate_name="   ", call_id=1).candidate_name is None


@pytest.mark.parametrize("request_type", [KBSearchRequest, KBAnswerRequest])
@pytest.mark.parametrize("kwargs", [
    {"role": "x" * 201},
    {"session_id": "x" * 129},
    {"role": "   "},
])
def test_kb_requests_reject_invalid_role_and_session_id(request_type, kwargs):
    with pytest.raises(ValidationError):
        request_type(query="valid query", **kwargs)


@pytest.mark.parametrize("role", [123, True, None])
def test_start_request_rejects_wrong_type_role(role):
    with pytest.raises(ValidationError):
        StartRequest(role=role, call_id=1)


@pytest.mark.parametrize("request_type", [KBSearchRequest, KBAnswerRequest])
@pytest.mark.parametrize("kwargs", [{"role": 123}, {"session_id": 123}])
def test_kb_requests_reject_wrong_type_role_and_session_id(request_type, kwargs):
    with pytest.raises(ValidationError):
        request_type(query="valid query", **kwargs)


def test_start_request_accepts_unicode_candidate_name_at_max_length():
    name = "中" * 200
    body = StartRequest(role="Backend Engineer", candidate_name=name, call_id=1)
    assert len(body.candidate_name) == 200


def test_start_request_rejects_unicode_candidate_name_over_max_length():
    with pytest.raises(ValidationError):
        StartRequest(role="Backend Engineer", candidate_name="中" * 201, call_id=1)


def test_start_request_preserves_internal_whitespace_in_role():
    """strip_whitespace trims only leading/trailing whitespace — internal spacing must
    survive unchanged."""
    body = StartRequest(role="  Senior   Engineer  ", call_id=1)
    assert body.role == "Senior   Engineer"


def test_start_request_candidate_name_omitted_or_null_is_none():
    assert StartRequest(role="Backend Engineer", call_id=1).candidate_name is None
    assert StartRequest(role="Backend Engineer", candidate_name=None, call_id=1).candidate_name is None
