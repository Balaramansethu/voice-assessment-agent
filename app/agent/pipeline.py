"""Pipecat voice bot (Pipecat 1.8 API): WebRTC ⇄ Deepgram Nova-3 (STT) ⇄ Groq LLM
⇄ Deepgram Aura (TTS).

End-to-end interview flow:
  on connect → resolve caller (DEMO_CALLER_PHONE) → recover interview state →
  greet with real context → CONTINUE_INTERVIEW (backend decides START/RESUME/REJECT)
  → ask the backend-provided question → record_answer → next question → … → complete.

The bot stays a thin transport+conversation layer. Every state change goes through
the orchestrator via HTTP (app.agent.tools → FastAPI api). The interview id is bound
server-side per connection, so the LLM can neither see nor guess it.

Browser demo has no real caller id, so the caller is taken from DEMO_CALLER_PHONE
(defaults to the seeded candidate). Re-seed a scenario and the same phone resolves to
the new interview — that's the whole demo loop.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame, LLMRunFrame, TextFrame, UserStoppedSpeakingFrame, TTSStartedFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.agent import prompts
from app.agent import tools


START_ASSESSMENT_SCHEMA = FunctionSchema(
    name="start_assessment",
    description="Begin the screening assessment for the role the caller named. Call this "
                "once they tell you which role they're interviewing for.",
    properties={
        "role": {"type": "string", "description": "The role, e.g. 'Backend Engineer'."},
        "candidate_name": {"type": "string", "description": "The caller's name. Always pass it "
                           "if they gave one; omit only if they truly declined to say."},
    },
    required=["role"],
)

SUBMIT_ANSWER_SCHEMA = FunctionSchema(
    name="submit_answer",
    description="Submit the caller's spoken answer to the current question. Returns the next "
                "question, or tells you the assessment is complete. It never returns a score — "
                "grading is silent.",
    properties={"answer": {"type": "string", "description": "The caller's spoken answer."}},
    required=["answer"],
)

KB_ANSWER_SCHEMA = FunctionSchema(
    name="kb_answer",
    description="Answer a caller's question about the company, role, compensation, tech "
                "stack, or process using the knowledge base. Use whenever they ask something "
                "factual about us or the job.",
    properties={"query": {"type": "string", "description": "The caller's question."}},
    required=["query"],
)

# Matches a fragment made up solely of dots/ellipses (e.g. "..", "...", "…", ". . .").
# A single "." is intentionally NOT caught here — see _is_unspeakable.
_DOTS_ONLY_RE = re.compile(r"^[.…]+$")

# secrets.token_urlsafe(32) always produces exactly 43 chars from this fixed
# charset (base64url, no padding, 32 random bytes -> ceil(256/6)=43 chars).
# Anything else is obviously garbage — reject with ZERO network/DB round-trip,
# before ever calling the api service. This doesn't stop a connection flood by
# itself (raw .accept() still happens at the TCP/WS-upgrade level, which is
# why a Caddy access-log + fail2ban layer exists — see deploy/Caddyfile and
# RUNBOOK.local.md) — it makes each rejected garbage connection nearly free
# instead of costing a Postgres write via consume_token.
_TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _is_unspeakable(text: str) -> bool:
    """True only for fragments TTS should never receive: pure whitespace, or a run of
    dots/ellipses that is spam rather than a real sentence mark.

    Dot-run spam is a relic of the old gpt-oss reasoning model. We drop it, but we KEEP
    a lone "." (and every other single mark ",", "?", "!", ";", ":") because qwen streams
    sentence punctuation as standalone tokens and those marks give the TTS its prosody.
    So a dots-only fragment is spam iff it has 2+ characters OR contains an ellipsis "…"
    (U+2026 already reads as three dots). Pure — easily unit-tested.
    Examples: ""/"  " → True; ".."/"..."/"…" → True; "."/"?"/"Hi" → False.
    """
    stripped = "".join(text.split())  # collapse all whitespace, including between dots
    if not stripped:
        return True
    if _DOTS_ONLY_RE.match(stripped):
        return len(stripped) >= 2 or "…" in stripped
    return False


class SpeakableTextFilter(FrameProcessor):
    """Drops LLM text fragments that are ellipsis/dot-run spam or pure whitespace.

    qwen streams clean spoken content, but it also streams sentence punctuation as
    STANDALONE tokens (".", ",", "?", "!"). Those marks are exactly what gives the TTS
    its prosody — pauses and intonation — and let it chunk sentences promptly, so they
    MUST pass through. We therefore suppress only the narrow garbage case: a run of two
    or more dots/ellipses ("..", "...", "…"), a leftover from the old gpt-oss reasoning
    model. Every other frame (real punctuation, words, LLMFullResponseStart/End, control
    frames) passes untouched so the TTS service's own sentence aggregation is unaffected.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # LLMTextFrame subclasses TextFrame; matching TextFrame covers both.
        if isinstance(frame, TextFrame) and _is_unspeakable(frame.text):
            return  # dot-spam / whitespace only — drop it before it reaches TTS
        await self.push_frame(frame, direction)


# --- numeronym / abbreviation → spoken-word normalization ----------------------
# Deepgram Aura reads raw text and mangles numeronyms ("a11y" → "a eleven y"), and
# Aura's Settings expose no SSML/normalization, so Deepgram's own guidance is to fix
# this at the TEXT layer. We map the known offenders to their spoken form and rewrite
# them on the TextFrame BEFORE TTS. Genuine acronyms that already read correctly
# (API, CSS, HTML, DOM, ARIA, CDN, SPA) are deliberately absent and left untouched.
_SPOKEN_FORMS: dict[str, str] = {
    "a11y": "accessibility",
    "i18n": "internationalization",
    "l10n": "localization",
    "k8s": "kubernetes",
    "e2e": "end to end",
    "p11y": "performance",
    "o11y": "observability",
    "s12n": "serialization",
}
_NUMERONYM_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_SPOKEN_FORMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _speakify(text: str) -> str:
    """Replace known numeronyms/abbreviations with their spoken-word form.

    Whole-word and case-insensitive; unknown tokens and normal acronyms are left
    exactly as-is. Pure — safe to unit-test in isolation.
    """
    return _NUMERONYM_RE.sub(lambda m: _SPOKEN_FORMS[m.group(0).lower()], text)


class SpokenFormNormalizer(FrameProcessor):
    """Rewrites numeronyms/abbreviations to spoken words on their way to TTS.

    Sits in the llm→tts slot and mutates the text of every TextFrame in place
    (LLMTextFrame subclasses TextFrame, so streamed LLM output is covered too).
    Only the known map is touched; everything else passes through unchanged.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            new_text = _speakify(frame.text)
            if new_text != frame.text:
                frame.text = new_text
        await self.push_frame(frame, direction)


class ActivityObserver(BaseObserver):
    """PR-407: updates last_activity['t'] on any user-speech-stop or bot-speech-start
    frame, so an inactivity watchdog can detect a caller who goes silent without hanging
    up — which would otherwise hold a max_concurrent_calls slot for the full
    max_call_duration_seconds. Passive observer (task.add_observer), not a pipeline
    FrameProcessor — does not touch the existing turn-detection chain."""

    def __init__(self, last_activity: dict) -> None:
        super().__init__()
        self._last_activity = last_activity

    async def on_push_frame(self, data: FramePushed) -> None:
        if isinstance(data.frame, (UserStoppedSpeakingFrame, TTSStartedFrame)):
            self._last_activity["t"] = time.monotonic()


def _transport_params() -> dict:
    return {
        # Browser: plain WebRTC in/out. Deepgram handles quiet mic audio natively,
        # so no pre-amplification is needed ahead of VAD.
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        # Phone: Twilio needs FastAPIWebsocketParams (the runner sets add_wav_header
        # and the Twilio serializer on it — the base TransportParams lacks those
        # fields, which crashes the telephony bot). 8kHz mu-law at telephone level,
        # so no gain boost.
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }


async def build_interview_task(
    head: FrameProcessor,
    tail: FrameProcessor,
    *,
    handle_sigint: bool = False,
):
    """Assemble the REAL interview pipeline between a supplied `head` and `tail`.

    Everything that defines the agent's behaviour — Deepgram Nova-3 STT, the Silero
    VADProcessor, Smart Turn v3 end-of-turn, the qwen LLM + the three tools, Deepgram
    Aura TTS, the SpokenFormNormalizer / SpeakableTextFilter, and the min-words
    barge-in strategy — is built here exactly once. `bot()` (WebRTC/Twilio) passes
    `transport.input()` / `transport.output()`; the offline self-test harness passes a
    scripted audio source / capturing sink. Neither forks the pipeline logic.

    Returns `(task, greet, last_activity, state)`: the assembled `PipelineTask`, an async
    `greet()` that seeds the greeting kickoff turn (what `on_client_connected` fires for a
    live call), a dict used by the inactivity watchdog to track activity timestamps, and
    the per-connection identity/session state dict (candidate_id/interview_id/call_id) —
    `bot()`'s `on_client_disconnected` handler needs this to know whether an interview is
    in progress when the call drops, and it lives in a different function's scope than the
    `state` closed over by the tool handlers defined here. The caller owns the run loop
    and, for the transport case, the event wiring.
    """
    groq_key = os.environ["GROQ_API_KEY"]
    deepgram_key = os.environ["DEEPGRAM_API_KEY"]
    # Deepgram Nova-3 streaming STT. ONE turn-taking authority only: the Silero
    # VADProcessor below owns turn boundaries and interruptions. We therefore
    # DISABLE Deepgram-side endpointing (endpointing=False, utterance_end_ms unset)
    # so Deepgram never also cuts a turn — running two detectors desynchronises them
    # and splits a single spoken answer across two turns (Deepgram's voice-agent guide
    # is explicit about this). In this pipecat (1.8.1) the Deepgram STT does not emit
    # UserStarted/StoppedSpeaking anyway — it consumes the VAD's frames to fire a
    # finalize — so the VAD must drive turns and Deepgram just transcribes.
    # smart_format tidies numbers/punctuation; keyterm boosts domain vocab that was
    # previously misheard (nova-3 keyterm prompting).
    stt = DeepgramSTTService(
        api_key=deepgram_key,
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_STT_MODEL", "nova-3"),
            endpointing=False,
            smart_format=True,
            keyterm=[
                "debouncing", "memoization", "virtual DOM", "hydration",
                "idempotency", "Postgres", "WebSocket", "Balaraman",
            ],
        ),
    )
    llm = GroqLLMService(
        api_key=groq_key,
        # qwen3.8-27b: a conversational model on this Groq tier that streams CLEAN
        # spoken content by default and calls our tools reliably. We deliberately do
        # NOT use gpt-oss here: gpt-oss is a reasoning model whose only clean path is
        # reasoning_format=hidden, and that param must ride in `extra_body` — but this
        # Pipecat (1.8.1) drops the Settings `extra`/`extra_body` on the streaming
        # create() call, so the suppression never reaches Groq and gpt-oss speaks its
        # chain-of-thought ("GreatOopsWeSorryWe…"). Verified directly against Groq:
        # gpt-oss+hidden is clean, but only when the param actually lands; qwen needs
        # no such param. SpeakableTextFilter downstream still strips any stray
        # punctuation-only fragment before TTS.
        settings=GroqLLMService.Settings(
            model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b"),
            # Unbounded output hit Groq's per-request output-token-per-minute limit
            # (OTPM 1000) on a single turn that tried to generate 1339 tokens — a
            # "request too large" 429 that no amount of retrying fixes, since it's the
            # same oversized request every time. The SDK's own retry-with-backoff kept
            # resubmitting it for ~111s of dead air until the caller spoke over it.
            # 300 comfortably covers a full kb_answer readback of the longest seeded KB
            # chunk (~110 tokens) plus lead-in/wrap-up, while a reply that overshoots it
            # just gets truncated by Groq — never another OTPM 429, never a multi-turn
            # hang. Every reply here is meant to be one short spoken sentence anyway
            # (system prompt rule 5), so this should never bind in normal operation.
            max_tokens=300,
        ),
    )
    # Deepgram Aura streaming TTS.
    tts = DeepgramTTSService(
        api_key=deepgram_key,
        voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-thalia-en"),
    )

    # Turn-taking is now a TWO-SIGNAL system, and the two have distinct jobs:
    #
    #   START / interruptions  → min-words gate (MinWordsUserTurnStartStrategy, below).
    #     A turn (and any barge-in interruption) begins on TRANSCRIBED WORDS, not a raw
    #     VAD edge: while the bot is speaking it takes >= INTERRUPTION_MIN_WORDS words to
    #     interrupt, so breath/echo/one-word blips can't cancel the reply; when the bot
    #     is silent a single word starts the turn, so onset latency is unchanged. The
    #     Silero VAD below still detects speech edges (it feeds Smart Turn its pauses).
    #   STOP / end-of-turn      → Smart Turn v3 (TurnAnalyzerUserTurnStopStrategy).
    #     A small ONNX prosody model predicts whether the caller has genuinely
    #     finished their thought, rather than timing a fixed silence. It decides
    #     FAST when confident (a falling, complete-sounding "…twenty four by seven.")
    #     and holds the turn open through a mid-answer pause that only SOUNDS
    #     unfinished ("The washing machine is working …<thinking>… all week"). This
    #     replaces the old crude stop_secs=2.0 fixed-silence endpoint that both cut
    #     people off mid-pause AND always waited the full 2s when they were done.
    #
    # VAD stop_secs is therefore no longer the primary end-of-turn signal. We return
    # it to Pipecat's recommended 0.2s (VAD_STOP_SECS) so VAD reports "silence began"
    # promptly and Smart Turn can run its inference on the pause immediately; the
    # framework warns if this drifts from 0.2 because the STT p99 safety-net budget is
    # calibrated to it. The GENEROUS backstop that guarantees a turn always ends even
    # if the model never says COMPLETE lives inside the analyzer itself
    # (SmartTurnParams.stop_secs defaults to 3.0s of hard silence → forced COMPLETE),
    # so Smart Turn owns both the fast decision and the safety net; VAD just detects
    # speech edges.
    #   confidence=0.6 — Silero's neural speech/non-speech score is the real gate and
    #     rejects breath/background noise on its own. min_volume=0.0 disables the raw
    #     amplitude gate: browser WebRTC audio arrives quiet (~3% full-scale) and the
    #     old GainAudioFilter that boosted it is gone, so any non-zero min_volume would
    #     reject normal mic input and make the agent deaf. WebRTC echo cancellation
    #     stops the bot from hearing its own playback; barge-in is gated by the
    #     min-words START strategy above, not by VAD, so noise can't interrupt while a
    #     real spoken sentence still does.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2,
                             stop_secs=0.2, min_volume=0.0),
        ),
    )

    # End-of-turn STOP strategy. START is always the min-words + transcription gate
    # (below); only the stop signal is switchable via TURN_DETECTION.
    #
    #   smart_turn (default) — Smart Turn v3. The 8.6MB ONNX is baked into the agent
    #     image (bundled with Pipecat), so passing no model path loads it from disk
    #     with no download.
    #   vad — the old fixed-silence endpointer (SpeechTimeoutUserTurnStopStrategy),
    #     with NO ML. For offline / low-CPU boxes.
    #
    # We must always build an explicit stop strategy for the `vad` case: leaving it
    # unset (passing None) does NOT disable ML — UserTurnStrategies.__post_init__
    # backfills the framework default, which is itself Smart Turn v3. So `vad` has to
    # name SpeechTimeoutUserTurnStopStrategy to genuinely opt out of the model.
    turn_detection = os.getenv("TURN_DETECTION", "smart_turn").strip().lower()
    smart_turn_cpus = int(os.getenv("SMART_TURN_CPU_COUNT", "2"))
    # Backstop is a SAFETY NET, not the decider: raised 1.0 → 2.0 so an unsure caller
    # mid-thought is not force-closed and fragmented. Smart Turn owns the real decision.
    smart_turn_stop_secs = float(os.getenv("SMART_TURN_STOP_SECS", "2.0"))
    if turn_detection == "smart_turn":
        # The stop strategy is self-driving: it consumes the InputAudioRawFrame /
        # VAD / transcription frames already flowing through the user aggregator,
        # sets its own sample rate on setup(), and syncs the analyzer's pre-speech
        # buffer to VAD start_secs — no manual audio plumbing needed. wait_for_transcript
        # keeps us on the cascade (STT) path: fire on COMPLETE *and* a finalized
        # transcript, so we never cut a turn before Deepgram has the words.
        stop_strategy: BaseUserTurnStopStrategy = TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(
                cpu_count=smart_turn_cpus,
                # Hard-silence backstop is a SAFETY NET only (2.0s). Smart Turn is the
                # primary decider and fires fast when confident; this just rescues a
                # turn the model never resolves, without cutting a mid-thought caller off.
                params=SmartTurnParams(
                    stop_secs=smart_turn_stop_secs,
                    pre_speech_ms=500,
                    max_duration_secs=8,
                ),
            ),
            wait_for_transcript=True,
        )
    else:
        # Fixed-silence fallback: end the turn a short window after VAD reports
        # silence, once a final transcript has landed. No prosody model.
        stop_strategy = SpeechTimeoutUserTurnStopStrategy(wait_for_transcript=True)
    # Barge-in gate (FIX: interruption storm). The framework default START set is
    # [VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy], and VAD-start
    # begins a user turn — and broadcasts an interruption that cancels the in-flight
    # bot reply — on a SINGLE raw VAD event (a breath, echo or one-word blip). That
    # fired ~12 generations in a 45s window, ~11 of them cancelled before a word
    # reached TTS. We replace VADUserTurnStartStrategy with Pipecat's own
    # MinWordsUserTurnStartStrategy: while the bot is speaking it requires
    # >= INTERRUPTION_MIN_WORDS transcribed words before it starts a turn (so noise /
    # echo / a single "uh" no longer interrupts), yet a real spoken sentence still cuts
    # in within ~1s. When the bot is SILENT it triggers on a single word, so normal
    # turn-taking latency is unchanged. This is the production standard (min-words
    # barge-in) and, as a bonus, means a VAD-only noise turn with no transcript never
    # starts a turn at all — so the LLM is never asked to fill silence (FIX: babbling).
    # We keep TranscriptionUserTurnStartStrategy alongside it (the min-words strategy
    # itself consumes transcripts; the pair matches the framework default shape).
    interruption_min_words = int(os.getenv("INTERRUPTION_MIN_WORDS", "3"))
    user_turn_strategies = UserTurnStrategies(
        start=[
            MinWordsUserTurnStartStrategy(min_words=interruption_min_words),
            TranscriptionUserTurnStartStrategy(),
        ],
        stop=[stop_strategy],
    )

    # Per-connection binding, set once the assessment starts.
    state = {"session_id": None, "call_id": None, "role": None, "candidate_id": None,
             "interview_id": None}

    async def _start_assessment(params: FunctionCallParams) -> None:
        res = await tools.start_assessment(
            role=params.arguments["role"],
            candidate_name=params.arguments.get("candidate_name"),
            call_id=state["call_id"],
        )
        if not res.get("ok"):
            await params.result_callback({"ok": False, "instruction":
                "That role isn't available. Ask them which role they're interviewing for — "
                "the options are Backend Engineer or Frontend Engineer."})
            return
        state["session_id"] = res["session_id"]
        state["role"] = res["role"]
        fq = res["first_question"]["prompt"]
        await params.result_callback({"ok": True, "role": res["role"],
            "total_questions": res["total_questions"], "first_question": fq, "instruction":
            f"Say one short line: 'Great, let's begin your {res['role']} screening — it's "
            f"{res['total_questions']} questions.' Then ask the first_question EXACTLY as "
            f"written and wait. Never mention scoring or whether answers are right."})

    async def _submit_answer(params: FunctionCallParams) -> None:
        sid = state["session_id"]
        if sid is None:
            await params.result_callback({"instruction":
                "No assessment started yet — ask which role they're interviewing for."})
            return
        res = await tools.submit_answer(session_id=sid, call_id=state["call_id"],
                                        transcript=params.arguments["answer"])
        if res.get("done"):
            await params.result_callback({"done": True, "instruction":
                "That was the last question. Thank them warmly for their time, tell them the "
                "team will review and be in touch, and end on a friendly note. Do NOT reveal "
                "any score or whether answers were right or wrong."})
        else:
            await params.result_callback({"done": False,
                "next_question": res.get("next_question", {}).get("prompt", ""), "instruction":
                "In ONE short, neutral sentence just acknowledge you've noted their answer — do "
                "NOT say whether it was right or wrong and do NOT hint at any score — then ask "
                "the next_question EXACTLY as written and wait. Do NOT end the call or read "
                "these instructions aloud."})

    async def _kb_answer(params: FunctionCallParams) -> None:
        res = await tools.kb_answer(query=params.arguments["query"], role=state["role"],
                                    session_id=str(state.get("call_id")))
        if res.get("escalate"):
            await params.result_callback({"escalate": True, "instruction":
                "You don't have that info. Say so briefly and offer to connect them with "
                "recruiting, then continue the assessment where you left off."})
        else:
            await params.result_callback({"answer": res.get("answer", ""), "instruction":
                "Read this answer to the caller conversationally, then continue the assessment "
                "from where you left off (re-ask the current question if needed)."})

    llm.register_function("start_assessment", _start_assessment)
    llm.register_function("submit_answer", _submit_answer)
    llm.register_function("kb_answer", _kb_answer)

    context = LLMContext(
        messages=[{"role": "system", "content": prompts.SYSTEM_AGENT}],
        tools=ToolsSchema(standard_tools=[
            START_ASSESSMENT_SCHEMA, SUBMIT_ANSWER_SCHEMA,
            KB_ANSWER_SCHEMA,
        ]),
    )
    # user_turn_strategies is the hook Pipecat 1.8 exposes for pluggable turn
    # detection: LLMContextAggregatorPair → LLMUserAggregator → UserTurnController
    # consumes it. We always pass an explicit strategy set (built above) so cpu_count
    # and the smart_turn/vad switch are ours to control and the wiring is visible in
    # this file — the framework default would silently be Smart Turn v3 either way.
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=user_turn_strategies),
    )

    pipeline = Pipeline([
        head,          # transport.input() (live) or a scripted audio source (self-test)
        vad,           # sole turn detector → emits VAD speaking frames for the aggregator
        stt,           # Deepgram transcribes only (endpointing disabled)
        user_agg,      # Smart Turn gates end-of-turn here
        llm,
        SpokenFormNormalizer(),  # a11y → accessibility, k8s → kubernetes, … before TTS
        SpeakableTextFilter(),   # drop punctuation-only fragments before TTS
        tts,
        tail,          # transport.output() (live) or a capturing sink (self-test)
        assistant_agg,
    ])
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True,
                                                        enable_metrics=True))

    # PR-407: track frame activity for inactivity timeout detection.
    last_activity = {"t": time.monotonic()}
    task.add_observer(ActivityObserver(last_activity))
    # PR-408: wire Pipecat's built-in latency observers for STT/LLM/TTS metrics.
    task.add_observer(UserBotLatencyObserver())
    task.add_observer(MetricsLogObserver())

    async def greet(*, provider_call_id: str, from_number: str, transport_kind: str) -> None:
        info = await tools.open_inbound(provider_call_id=provider_call_id,
                                        from_number=from_number, transport=transport_kind)
        state["call_id"] = info.get("call_id")
        state["candidate_id"] = (info.get("candidate") or {}).get("id")
        state["interview_id"] = (info.get("interview") or {}).get("id")
        # No identity gate: go straight into the assessment regardless of whether
        # open_inbound resolved a candidate by phone. If it did, use the name as a
        # courtesy greeting only — never a precondition to proceed.
        name = (info.get("candidate") or {}).get("name")
        kickoff = ("The call just connected. Greet me warmly, say you're the automated "
                   "screening assistant, and ask which role I'm interviewing for." +
                  (f" Address me by name ({name})." if name else ""))
        context.set_messages([
            {"role": "system", "content": prompts.SYSTEM_AGENT},
            {"role": "user", "content": kickoff},
        ])
        await task.queue_frames([LLMRunFrame()])

    return task, greet, last_activity, state


async def _duration_watchdog(task: PipelineTask, call_id: int, seconds: int) -> None:
    """PR-021: force-end a Twilio call once it exceeds the configured maximum
    duration. Mirrors the exact task.cancel(reason=...) pattern
    scripts/selftest.py already uses to stop a live PipelineTask
    deterministically. Cancelled by _on_disconnected if the call ends earlier."""
    await asyncio.sleep(seconds)
    await tools.close_call(call_id, status="DISCONNECTED")
    await task.cancel(reason="max_call_duration_exceeded")


async def _inactivity_watchdog(task: PipelineTask, call_id: int, seconds: int,
                               last_activity: dict) -> None:
    """PR-407: force-end a call after `seconds` of continuous silence from BOTH
    parties (checked by polling, since — unlike max call duration — the deadline
    resets on any activity rather than counting from connection start). Cancelled by
    _on_disconnected if the call ends first."""
    while True:
        await asyncio.sleep(5)
        if time.monotonic() - last_activity["t"] >= seconds:
            await tools.close_call(call_id, status="DISCONNECTED")
            await task.cancel(reason="inactivity_timeout")
            return


async def bot(runner_args: RunnerArguments) -> None:
    """WebRTC (browser demo) or Twilio (public phone) entry point.

    Twilio connections must redeem a one-use, Postgres-backed voice-session
    token — minted by the signature-guarded POST /twilio/voice webhook and
    embedded in the WSS URL's ?token= query param — before ANY transport or
    provider work happens. This is deliberately NOT pipecat's own --ws-auth
    mechanism: that one is per-process, in-memory (an HMAC secret and a used-
    token set that live only in this one agent process and don't survive a
    restart), gated behind pipecat's own POST /start. Our token must be minted
    in the `api` service, keyed off the Twilio-signature-verified webhook, and
    validated from here over HTTP against that same Postgres-backed state.

    Twilio vs WebRTC is detected via isinstance(runner_args,
    WebSocketRunnerArguments), not runner_args.transport_type: transport_type
    is still None here for a Twilio connection — pipecat only populates it
    inside create_transport(), which must not run before the token check.
    """
    twilio_claims: dict | None = None

    if isinstance(runner_args, WebSocketRunnerArguments):
        # The runner (pipecat/runner/run.py::_handle_telephony_ws) has already
        # called websocket.accept() before invoking bot(), so .path_params /
        # .query_params are live here — no transport built, no provider touched yet.
        # Path segment first: Twilio's real Media Streams client does not reliably
        # forward a "?token=..." query string on the actual WSS connection (confirmed
        # in production via Caddy's access log — the incoming URI arrived as bare
        # "/ws", token dropped), so the token travels as a path segment
        # (wss://.../ws/<token>, matching pipecat's own pre-registered /ws/{token}
        # route — see app/api/telephony.py::_stream_twiml). query_params is kept as
        # a fallback for any other client that does preserve it.
        token = (runner_args.websocket.path_params.get("token")
                 or runner_args.websocket.query_params.get("token"))
        if token and _TOKEN_SHAPE_RE.match(token):
            twilio_claims = await tools.consume_voice_session(token)
        else:
            twilio_claims = {"ok": False}  # obviously-garbage shape: zero DB cost
        if not twilio_claims.get("ok"):
            await runner_args.websocket.close(code=4003)
            return

    transport = await create_transport(runner_args, _transport_params())

    if twilio_claims is not None:
        # Defense-in-depth, not the primary gate (the consume above already
        # proved this connection redeemed a token minted for a signature-
        # verified webhook). create_transport() just parsed the media stream's
        # own handshake and populated runner_args.call_data in place with
        # Twilio's real CallSid. It should always equal the CallSid the token
        # was minted for one step earlier in this same function; a mismatch
        # means this connection isn't the one the token was issued to, even
        # though it presented a validly-consumed token. Close before any
        # audio/provider work.
        real_call_sid = runner_args.call_data.call_id if runner_args.call_data else None
        if real_call_sid != twilio_claims.get("provider_call_id"):
            await runner_args.websocket.close(code=4003)
            return

    task, greet, last_activity, state = await build_interview_task(
        transport.input(), transport.output(),
        handle_sigint=getattr(runner_args, "handle_sigint", False),
    )

    # Mirrors app.config.Settings.max_call_duration_seconds; the agent process
    # doesn't import app.config (see Dockerfile.agent), so it reads the same
    # env var directly — same pattern as TURN_DETECTION / SMART_TURN_STOP_SECS.
    max_call_duration = int(os.getenv("MAX_CALL_DURATION_SECONDS", "900"))
    max_inactivity = int(os.getenv("MAX_INACTIVITY_SECONDS", "120"))
    watchdog_task: asyncio.Task | None = None
    inactivity_task: asyncio.Task | None = None

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        nonlocal watchdog_task, inactivity_task
        if twilio_claims is not None:
            await greet(provider_call_id=twilio_claims["provider_call_id"],
                        from_number=twilio_claims.get("from_number") or "unknown",
                        transport_kind="twilio")
            watchdog_task = asyncio.create_task(
                _duration_watchdog(task, twilio_claims["call_id"], max_call_duration)
            )
            last_activity["t"] = time.monotonic()  # reset baseline at connect
            inactivity_task = asyncio.create_task(
                _inactivity_watchdog(task, twilio_claims["call_id"], max_inactivity, last_activity)
            )
        else:
            demo_phone = os.getenv("DEMO_CALLER_PHONE", "+919000000001")
            await greet(provider_call_id=f"WEB{int(time.time()*1000)}",
                        from_number=demo_phone, transport_kind="webrtc")

    if twilio_claims is not None:
        @transport.event_handler("on_client_disconnected")
        async def _on_disconnected(_transport, _client):
            if watchdog_task is not None:
                watchdog_task.cancel()
            if inactivity_task is not None:
                inactivity_task.cancel()
            # Cancel in-flight provider work immediately (PR-402).
            try:
                await task.cancel(reason="client_disconnected")
            except Exception:
                pass
            # Mark the interview interrupted if one is in progress.
            if state.get("interview_id") is not None:
                try:
                    await tools.mark_interrupted(interview_id=state["interview_id"],
                                                 call_id=twilio_claims["call_id"])
                except Exception:
                    pass
            # Idempotent (call_service.end_call): harmless if the watchdog's
            # own cancel() already tore down the transport and fired this too.
            await tools.close_call(twilio_claims["call_id"], status="DISCONNECTED")

    runner = PipelineRunner(handle_sigint=getattr(runner_args, "handle_sigint", False))
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
