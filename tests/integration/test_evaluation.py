"""evaluation_service.evaluate_interview over a real Postgres.

Run inside the api container:
    docker compose exec api pytest tests/integration/test_evaluation.py
"""
import json
from types import SimpleNamespace

from app.db.models import Candidate, Interview, InterviewAnswer, InterviewQuestion
from app.db.session import SessionLocal, init_db
from app.services import evaluation_service as evs


def test_evaluate_interview_persists_null_score_on_provider_error_and_continues(monkeypatch):
    """The judge's except openai.APIError guard must compose with evaluate_interview's
    per-question loop: a REAL provider exception raised from the underlying client on one
    question must not raise/500, must persist score=None with a safe reason for that
    question, and must NOT abort grading of the remaining questions."""
    import httpx
    import openai

    init_db()
    with SessionLocal() as s:
        cand = Candidate(name="Eval Ex")
        s.add(cand)
        s.flush()
        interview = Interview(candidate_id=cand.id, role="Backend Engineer", status="COMPLETED")
        s.add(interview)
        s.flush()
        q1 = InterviewQuestion(interview_id=interview.id, position=1, prompt_text="q1")
        q2 = InterviewQuestion(interview_id=interview.id, position=2, prompt_text="q2")
        s.add_all([q1, q2])
        s.flush()
        a1 = InterviewAnswer(interview_id=interview.id, question_id=q1.id, transcript="answer 1")
        a2 = InterviewAnswer(interview_id=interview.id, question_id=q2.id, transcript="answer 2")
        s.add_all([a1, a2])
        s.commit()
        interview_id = interview.id

    call_count = {"n": 0}

    def _raise_once_then_succeed(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            response = httpx.Response(
                400, request=request,
                json={"error": {"message": "boom", "code": "json_validate_failed"}},
            )
            raise openai.BadRequestError(
                "boom", response=response, body={"error": {"message": "boom"}}
            )
        content = json.dumps({"score": 3.0, "rationale": "ok"})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    class _Completions:
        def create(self, **kwargs):
            return _raise_once_then_succeed(**kwargs)

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(evs, "groq_client", lambda: _Client())
    monkeypatch.setattr(evs.retriever, "get_rubric_for_question", lambda session, question: [])

    with SessionLocal() as s2:
        result = evs.evaluate_interview(s2, interview_id)  # must not raise
        s2.commit()

    scores = [e["score"] for e in result["evaluations"]]
    assert scores.count(None) == 1
    assert 3.0 in scores
    assert result["scored_count"] == 1
    assert result["average_score"] == 3.0
