"""Regression tests for app.agent.tools._post error handling.

Tests added:
- 200 response whose `.json()` raises ValueError should return {"ok": False, "error": "invalid_json"} (never raise).
- Transport exception such as `httpx.ReadError` should behave like other transport failures and return {"ok": False, "error": "provider_unreachable"} after retries.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from app.agent import tools


class BadJsonResponse:
    """Mock httpx response whose .json() raises ValueError."""
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def json(self):
        raise ValueError("invalid json")


def test_post_200_invalid_json_returns_invalid_json_error():
    """A 200 response whose .json() raises ValueError should not raise but return invalid_json."""
    response = BadJsonResponse(200)
    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        result = asyncio.run(tools._post("/test", json={"x": 1}, timeout=10))
    assert result == {"ok": False, "error": "invalid_json"}


def test_post_read_error_returns_provider_unreachable():
    """An httpx.ReadError should be treated like other transport errors and return provider_unreachable after retries."""
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        raise httpx.ReadError("read failed")

    with patch("app.agent.tools.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post
        result = asyncio.run(
            tools._post("/test", json={"x": 1}, timeout=10, retries=1)
        )
    assert result == {"ok": False, "error": "provider_unreachable"}
    assert call_count[0] == 2
