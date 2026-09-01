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

import os
import time

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame, LLMRunFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
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
        "candidate_name": {"type": "string", "description": "Their name, if they gave it."},
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


class SpeakableTextFilter(FrameProcessor):
    """Drops LLM text fragments that have nothing speakable in them.

    The LLM occasionally streams TextFrames whose stripped content is only
    punctuation or a parenthetical aside ("...", "…", "( Noted )"). Fed to TTS
    these produce "… .. Sorry…" garbage speech. We sit between the LLM and TTS
    and swallow any TextFrame with no alphanumeric characters; every other frame
    (LLMFullResponseStart/End, control frames) passes through untouched so the
    TTS service's own sentence aggregation is unaffected.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # LLMTextFrame subclasses TextFrame; matching TextFrame covers both.
        if isinstance(frame, TextFrame) and not any(c.isalnum() for c in frame.text):
            return  # nothing to say — drop it before it reaches TTS
        await self.push_frame(frame, direction)


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


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, _transport_params())

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
        ),
    )
    # Deepgram Aura streaming TTS.
    tts = DeepgramTTSService(
        api_key=deepgram_key,
        voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-thalia-en"),
    )

    # Silero VAD is the SINGLE turn-taking authority (Deepgram endpointing is off).
    # It emits the VADUserStarted/StoppedSpeaking frames the context aggregator turns
    # into user turns and interruptions. Tuning:
    #   stop_secs=2.0 — a longer trailing-silence window so a natural mid-answer pause
    #     ("The washing machine is working … twenty four by seven") stays ONE turn and
    #     one submit_answer call, instead of splitting onto the wrong question.
    #   confidence=0.6 — Silero's neural speech/non-speech score is the real gate and
    #     rejects breath/background noise on its own. min_volume=0.0 disables the raw
    #     amplitude gate: browser WebRTC audio arrives quiet (~3% full-scale) and the
    #     old GainAudioFilter that boosted it is gone, so any non-zero min_volume would
    #     reject normal mic input and make the agent deaf. WebRTC echo cancellation
    #     stops the bot from hearing its own playback, so we don't need the gate for
    #     barge-in control. Real speech still interrupts.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2,
                             stop_secs=2.0, min_volume=0.0),
        ),
    )

    # Per-connection binding, set once the assessment starts.
    state = {"session_id": None, "call_id": None, "role": None}

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
        res = await tools.submit_answer(session_id=sid, transcript=params.arguments["answer"])
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
            START_ASSESSMENT_SCHEMA, SUBMIT_ANSWER_SCHEMA, KB_ANSWER_SCHEMA,
        ]),
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        vad,           # sole turn detector → emits VAD speaking frames for the aggregator
        stt,           # Deepgram transcribes only (endpointing disabled)
        user_agg,
        llm,
        SpeakableTextFilter(),   # drop punctuation-only fragments before TTS
        tts,
        transport.output(),
        assistant_agg,
    ])
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        # Create a call record for tracing; the agent then greets and asks which role.
        info = await tools.open_inbound(provider_call_id=f"WEB{int(time.time()*1000)}",
                                        from_number="browser-webrtc", transport="webrtc")
        state["call_id"] = info.get("call_id")

        # The greeting kickoff must be a USER message, not a system one: qwen's chat
        # template raises "No user query found in messages" if a turn has only system
        # messages (gpt-oss tolerated it; qwen does not). Framing it as the call
        # connecting makes the model greet naturally in response.
        context.set_messages([
            {"role": "system", "content": prompts.SYSTEM_AGENT},
            {"role": "user", "content":
             "The call just connected. Greet me warmly, say you're the automated screening "
             "assistant, and ask which role I'm interviewing for (for example Backend Engineer "
             "or Frontend Engineer). Do NOT call any function yet — wait for me to name a role."},
        ])
        await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner(handle_sigint=getattr(runner_args, "handle_sigint", False))
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
