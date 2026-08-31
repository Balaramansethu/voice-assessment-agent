"""Pipecat voice bot (Pipecat 1.7 API): WebRTC ⇄ Groq Whisper (STT) ⇄ Groq LLM
⇄ Kokoro (local TTS).

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
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.agent import prompts
from app.agent import tools

import numpy as _np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter


class GainAudioFilter(BaseAudioFilter):
    """Amplifies incoming mic audio BEFORE VAD/STT. Browser WebRTC audio arrives
    quiet (~3% FS); this lifts it so Silero VAD detects speech and Whisper can
    transcribe it. Runs inside the input transport, ahead of the VAD analyzer."""

    def __init__(self, gain: float = 8.0):
        self._gain = gain

    async def start(self, sample_rate: int):
        pass

    async def stop(self):
        pass

    async def process_frame(self, frame):
        pass

    async def filter(self, audio: bytes) -> bytes:
        s = _np.frombuffer(audio, dtype=_np.int16).astype(_np.float32) * self._gain
        s = _np.clip(s, -32768, 32767).astype(_np.int16)
        return s.tobytes()

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


def _transport_params() -> dict:
    return {
        # Browser: WebRTC audio is quiet (~3% FS) so we boost it before VAD.
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_in_filter=GainAudioFilter(gain=8.0),
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
    stt = GroqSTTService(
        api_key=groq_key,
        settings=GroqSTTService.Settings(model=os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")),
    )
    llm = GroqLLMService(
        api_key=groq_key,
        settings=GroqLLMService.Settings(
            model=os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-120b"),
            # gpt-oss is a reasoning model; without this its chain-of-thought comes
            # back in the `reasoning` field and Pipecat speaks it aloud. "hidden"
            # drops reasoning so only the final answer is spoken. Pipecat spreads
            # `extra` as top-level create() kwargs, so Groq-specific params must go
            # inside `extra_body` (the OpenAI SDK forwards it to Groq).
            extra={"extra_body": {"reasoning_format": "hidden", "reasoning_effort": "low"}},
        ),
    )
    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(voice=os.getenv("TTS_VOICE", "af_heart")),
    )

    # VAD must be a pipeline processor in Pipecat 1.7 (NOT a TransportParams field).
    # It emits UserStarted/StoppedSpeaking, which drives the segmented Groq STT.
    # stop_secs is generous (1.5s) so a natural pause mid-answer doesn't end the
    # turn — this prevents answers being truncated or landing on the wrong question,
    # which we saw on phone calls. Confidence 0.5 reduces false triggers on line noise.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.5, start_secs=0.2,
                             stop_secs=1.5, min_volume=0.0),
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
        vad,           # emits UserStarted/StoppedSpeaking → drives segmented STT
        stt,
        user_agg,
        llm,
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

        context.set_messages([
            {"role": "system", "content": prompts.SYSTEM_AGENT},
            {"role": "system", "content":
             "This is the very first turn. Greet the caller warmly, say you're the automated "
             "screening assistant, and ask which role they're interviewing for (for example "
             "Backend Engineer or Frontend Engineer). Do NOT call any function on this turn — "
             "wait for them to name a role."},
        ])
        await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner(handle_sigint=getattr(runner_args, "handle_sigint", False))
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
