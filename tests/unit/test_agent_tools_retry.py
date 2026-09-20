"""Unit tests for app.agent.tools retry helper and never-raise contract."""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agent import tools


class MockResponse:
    """Mock httpx response for testing."""
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_post_success_200():
    """A clean 200 response returns the JSON with no retry."""
    response = MockResponse(200, {"result": "ok"})
    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10))
    assert result == {"result": "ok"}
    mock_client.return_value.__aenter__.return_value.post.assert_called_once()


def test_post_connection_error_then_success():
    """A ConnectError followed by success retries and returns the success response."""
    response = MockResponse(200, {"result": "success"})
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.ConnectError("connection failed")
        return response

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"result": "success"}
    assert call_count[0] == 2  # called twice (once failed, once succeeded)


def test_post_timeout_error_then_success():
    """A TimeoutException followed by success retries and returns the success response."""
    response = MockResponse(200, {"result": "success"})
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.TimeoutException("timeout")
        return response

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"result": "success"}
    assert call_count[0] == 2


def test_post_connection_error_both_attempts():
    """A ConnectError on both attempts returns provider_unreachable, never raises."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        raise httpx.ConnectError("connection failed")

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"ok": False, "error": "provider_unreachable"}
    assert call_count[0] == 2  # both attempts made
    # Critically: no exception was raised


def test_post_503_then_200():
    """A 503 on first attempt followed by 200 on retry succeeds."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return MockResponse(503, {})
        return MockResponse(200, {"result": "success"})

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"result": "success"}
    assert call_count[0] == 2


def test_post_404_no_retry():
    """A 404 does not retry (not in _RETRYABLE_STATUS) and returns http_404."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        return MockResponse(404, {})

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"ok": False, "error": "http_404"}
    assert call_count[0] == 1  # called once only, no retry


def test_post_502_then_success():
    """A 502 on first attempt retries and succeeds."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return MockResponse(502, {})
        return MockResponse(200, {"result": "success"})

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"result": "success"}
    assert call_count[0] == 2


def test_post_504_retry():
    """A 504 (Gateway Timeout) retries as a transient failure."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return MockResponse(504, {})
        return MockResponse(200, {"result": "success"})

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
    assert result == {"result": "success"}
    assert call_count[0] == 2


def test_post_backoff_timing():
    """Backoff increases with each attempt: 0.5s on attempt 1, 1.0s on attempt 2."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.TimeoutException("timeout")
        return MockResponse(200, {"result": "success"})

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        import time
        start_time = time.time()
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10, retries=1))
        elapsed = time.time() - start_time

    assert result == {"result": "success"}
    assert call_count[0] == 2
    # Backoff should be at least 0.5s but allow some jitter/timing variance
    assert elapsed >= 0.4  # generous lower bound to avoid flaky tests


def test_open_inbound_uses_post():
    """open_inbound now uses _post and has never-raise contract."""
    with patch("app.agent.tools._post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"call_id": 123}
        result = asyncio.run(
            tools.open_inbound(
                provider_call_id="test-call", from_number="+1234567890"
            )
        )
    assert result == {"call_id": 123}
    mock_post.assert_called_once_with(
        "/calls/inbound",
        json={
            "provider_call_id": "test-call",
            "from_number": "+1234567890",
            "transport": "webrtc",
        },
        timeout=10,
    )


def test_submit_answer_uses_post():
    """submit_answer now uses _post with timeout=20."""
    with patch("app.agent.tools._post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"done": False, "next_question": {"prompt": "test"}}
        result = asyncio.run(
            tools.submit_answer(session_id=1, call_id=2, transcript="my answer")
        )
    assert result == {"done": False, "next_question": {"prompt": "test"}}
    mock_post.assert_called_once_with(
        "/assessment/grade",
        json={"session_id": 1, "call_id": 2, "transcript": "my answer"},
        timeout=20,
    )


def test_close_call_uses_post():
    """close_call now uses _post."""
    with patch("app.agent.tools._post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"ok": True}
        result = asyncio.run(tools.close_call(call_id=42, status="DISCONNECTED"))
    assert result == {"ok": True}
    mock_post.assert_called_once_with(
        "/calls/42/end",
        json={"status": "DISCONNECTED"},
        timeout=10,
    )
