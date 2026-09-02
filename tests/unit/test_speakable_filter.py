"""Unit tests for the agent's speakable-text helpers (pure functions).

These guard the FIX-5 narrowing of SpeakableTextFilter: qwen streams sentence
punctuation as standalone tokens (".", "?", ...), and those marks give the TTS its
prosody, so the filter must PRESERVE them and only drop ellipsis/dot-run spam and
pure whitespace.

`app.agent.pipeline` imports pipecat (present only in the agent image), so these
skip cleanly where pipecat isn't installed (e.g. the api container) rather than
failing collection.
"""
import pytest

pytest.importorskip("pipecat")

from app.agent.pipeline import _is_unspeakable  # noqa: E402


@pytest.mark.parametrize("text", ["", "   ", "\n", "\t "])
def test_pure_whitespace_is_unspeakable(text: str) -> None:
    assert _is_unspeakable(text) is True


@pytest.mark.parametrize("text", ["..", "...", "…", "……", ". . .", " ... "])
def test_dot_and_ellipsis_runs_are_unspeakable(text: str) -> None:
    # 2+ dot/ellipsis characters with nothing else — the old reasoning-model residue.
    assert _is_unspeakable(text) is True


@pytest.mark.parametrize("text", [".", ",", "?", "!", ";", ":", "Hi", "Hi there.", "It's 7."])
def test_real_punctuation_and_words_pass_through(text: str) -> None:
    # Single sentence marks carry prosody; anything with a word is obviously speakable.
    assert _is_unspeakable(text) is False
