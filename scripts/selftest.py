"""Synthetic self-test harness: drive a FULL interview through the REAL voice
pipeline with no human or microphone, then auto-score it against a standards gate.

WHAT IT ACTUALLY DOES
  1. Synthesizes candidate speech (name+role, then 5 frontend answers of varying
     quality) with Deepgram Aura TTS at the pipeline's 16 kHz input rate.
  2. Feeds that audio through the ACTUAL `build_interview_task()` pipeline — Silero
     VADProcessor, Deepgram Nova-3 STT, Smart Turn v3, qwen LLM + the three tools
     (which call the api over HTTP), SpokenFormNormalizer / SpeakableTextFilter, and
     Deepgram Aura TTS — swapping ONLY the transport ends for a scripted audio source
     and a capturing sink. Pacing is realistic: 20 ms audio chunks in real time,
     ~1.7 s of silence between turns, and one low-level NOISE burst injected into a
     silence gap to prove noise no longer triggers interruptions.
  3. A PipelineTask observer captures every TTS text / TTS start-stop / interruption /
     LLM-run frame with timestamps. Persisted results are read back over HTTP from the
     api (summary + the raw answers endpoint).
  4. Scores the run against the gate below and prints a PASS/FAIL scorecard with the
     measured numbers, exiting non-zero on any failure.

HONESTY ABOUT COVERAGE
  Fully exercised end-to-end through real audio: GREETING, NO SPURIOUS INTERRUPTIONS,
  REPLY LATENCY (TTFB per turn), NO BABBLE, PUNCTUATION/PROSODY, ANSWER ALIGNMENT,
  PERSISTED RESULT.
  NOT covered here: REAL BARGE-IN. Cutting the bot off mid-utterance offline is
  timing-fragile (it needs >=3 transcribed words to land WHILE TTS audio is still
  playing back into VAD, which this scripted source does not loop back). We SKIP it and
  say so, rather than fake it. Everything else is measured on real audio.

NOT a pytest test — it hits live Groq + Deepgram and loads the Smart Turn ONNX model.
Run it standalone inside the agent container:

    docker compose exec -T agent python -m scripts.selftest

EXIT CODES (so a loop runner can distinguish outcomes):
    0  all standards passed
    1  ran fully, but one or more standards FAILED (real numbers on the scorecard)
    2  BLOCKED — the LLM provider rate-limited the whole run (Groq free-tier tokens-per-
       day exhausted); environment issue, not an agent failure. Retry after quota reset.
    3  could not resolve the session for another reason (see the printed captured errors)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

# LangSmith trace-flush can hang a short-lived process on exit; turn it off before any
# app import wires a client, and print unbuffered so a hang is diagnosable.
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    ErrorFrame,
    TTSStartedFrame,
    TTSTextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from app.agent.pipeline import build_interview_task  # noqa: E402

# ─────────────────────────── tunable thresholds ───────────────────────────
# All gates in one place so the operator can tune them.
SAMPLE_RATE = 16_000            # pipeline input rate (Silero/Smart Turn expect 16 kHz)
CHUNK_MS = 20                   # audio pushed in 20 ms frames, paced in real time
SILENCE_BETWEEN_TURNS_S = 1.7   # gap after each utterance (1.5–2 s window)
NOISE_BURST_S = 0.4             # length of the low-level noise probe in one gap
NOISE_LEVEL = 0.03              # noise amplitude (fraction of full scale) — below speech
LEAD_SILENCE_S = 0.3            # brief silence before the first utterance
GREETING_DEADLINE_S = 3.0       # greeting TTS must start within this of session start
TTFB_P95_MAX_S = 3.5            # p95 reply latency (end-of-turn → first TTS token)
TTFB_HARD_MAX_S = 8.0           # no single reply may exceed this
TURN_RESPONSE_TIMEOUT_S = 30.0  # per-turn wait for the bot to reply before giving up
FINAL_DRAIN_S = 8.0             # after the last answer, wait this long for completion
API_BASE = os.getenv("API_BASE", "http://api:8000")
# P1's scope-gated auth applies to /assessment/by_call (agent-or-recruiter) and
# /assessment/{id}/summary|answers (recruiter) — this harness runs in the agent
# container (no app.config import, per Dockerfile.agent's split), so it reads the
# same env vars directly, matching app/agent/tools.py's own pattern.
_AGENT_HEADERS = {"X-Agent-Key": os.getenv("AGENT_SHARED_KEY", "dev-agent-key-change-me")}
_RECRUITER_HEADERS = {"X-Recruiter-Key": os.getenv("RECRUITER_SHARED_KEY", "dev-recruiter-key-change-me")}

# Utterances the synthetic candidate "speaks": name+role, then 5 frontend answers.
# Quality is deliberately varied (strong → weak) so grading differs across positions.
NAME_ROLE = "My name is Bala, and I'm interviewing for the Frontend Engineer role."
# One utterance per answer, kept short enough that Smart Turn treats each as a SINGLE
# turn (long multi-sentence answers get split across turns by end-of-turn detection,
# which is realistic but makes 1:1 alignment ambiguous). Quality still varies
# strong → weak so grading differs across positions. Keyword sets in _ANSWER_KEYWORDS
# must stay in sync with these.
ANSWERS = [
    # Strong: closures.
    "A closure is a function bundled with references to its surrounding variables, so "
    "it keeps access to them even after the outer function returns.",
    # Strong: the virtual DOM.
    "The virtual DOM is an in-memory copy of the real DOM that the framework diffs on "
    "each change to apply only the minimal updates.",
    # Partial: debouncing, roughly right but thin.
    "Debouncing means you wait until the user stops typing before you fire the handler, "
    "like a search box.",
    # Weak: CSS specificity, vague and muddled.
    "CSS specificity is about which style wins, I think the last one usually wins.",
    # Partial: accessibility, decent but incomplete.
    "For accessibility I add alt text to images and use semantic HTML so it works with "
    "a keyboard.",
]

# Babble patterns: the agent must NEVER apologize for or narrate silence.
_BABBLE_RE = re.compile(
    r"\b(sorry|apolog|are you there|let'?s continue|i'?m here|take your time|"
    r"still there|pause|hello\?)\b",
    re.IGNORECASE,
)
_SENTENCE_MARK_RE = re.compile(r"[.?!]")


def log(msg: str) -> None:
    """Unbuffered, timestamped progress line so a hang is diagnosable."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────── audio synthesis ───────────────────────────
async def _synthesize(text: str, key: str) -> np.ndarray:
    """Deepgram Aura → raw 16 kHz mono PCM16 (container=none), returned as int16."""
    url = ("https://api.deepgram.com/v1/speak"
           f"?model={os.getenv('DEEPGRAM_TTS_VOICE', 'aura-2-thalia-en')}"
           f"&encoding=linear16&sample_rate={SAMPLE_RATE}&container=none")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, headers={"Authorization": f"Token {key}"},
                         json={"text": text})
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.int16).copy()


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


def _noise(seconds: float) -> np.ndarray:
    """Low-level white noise — a realistic room/line hiss, well below speech level."""
    n = int(seconds * SAMPLE_RATE)
    amp = int(NOISE_LEVEL * 32767)
    return np.random.default_rng(7).integers(-amp, amp, size=n, dtype=np.int16)


# ─────────────────────────── scripted source / sink ───────────────────────────
class ScriptedAudioSource(FrameProcessor):
    """Pipeline head in place of `transport.input()`. Passes control frames through and
    lets the harness push InputAudioRawFrames downstream, paced in real time."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def push_audio(self, pcm: np.ndarray) -> None:
        """Emit `pcm` as 20 ms InputAudioRawFrames, sleeping between them so VAD and
        Smart Turn see the same real-time cadence a live transport produces."""
        step = int(SAMPLE_RATE * CHUNK_MS / 1000)
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step].tobytes()
            await self.push_frame(
                InputAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
            await asyncio.sleep(CHUNK_MS / 1000)


class CapturingSink(FrameProcessor):
    """Pipeline tail in place of `transport.output()`. Passes everything through (so the
    downstream assistant aggregator still runs) — capture happens in the observer."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class CaptureObserver(BaseObserver):
    """Records TTS text / start / interruption / user-stop frames with ABSOLUTE
    monotonic timestamps (session-start is subtracted only when we score).

    Two subtleties learned from a live run:
      * A single interruption BROADCAST is observed as many InterruptionFrame pushes
        (one per processor it fans out to). We collapse pushes within INT_DEDUP_S into
        one logical broadcast so the count matches the pipeline's own log.
      * The session-start clock for the greeting deadline is when greet() fires, NOT
        observer construction (audio synthesis happens first) — the driver sets it via
        mark_session_start().
    """

    INT_DEDUP_S = 0.5   # InterruptionFrames within this window == one broadcast

    def __init__(self) -> None:
        super().__init__()
        self.session_start: float | None = None
        self.tts_text: list[tuple[float, str]] = []     # (abs_t, text) per TTSTextFrame
        self.tts_started: list[float] = []              # abs_t of each TTSStartedFrame
        self.interruptions: list[float] = []            # abs_t, deduped to one/broadcast
        self.user_stops: list[float] = []               # abs_t of each UserStoppedSpeaking
        self.errors: list[tuple[str, str]] = []         # (category, message) per ErrorFrame

    def mark_session_start(self) -> None:
        self.session_start = time.monotonic()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        t = time.monotonic()
        if isinstance(frame, TTSTextFrame):
            self.tts_text.append((t, frame.text))
        elif isinstance(frame, TTSStartedFrame):
            self.tts_started.append(t)
        elif isinstance(frame, InterruptionFrame):
            if not self.interruptions or (t - self.interruptions[-1]) > self.INT_DEDUP_S:
                self.interruptions.append(t)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_stops.append(t)
        elif isinstance(frame, ErrorFrame):
            # `category` is an ErrorCategory enum in this Pipecat — stringify both fields.
            self.errors.append((str(getattr(frame, "category", "") or ""),
                                str(getattr(frame, "error", ""))[:200]))

    @property
    def rate_limited(self) -> bool:
        """True if any pushed ErrorFrame was a provider rate limit (429 TPD/TPM)."""
        return any("rate_limit" in f"{cat} {msg}".lower() or "429" in msg
                   for cat, msg in self.errors)


# ─────────────────────────── the run ───────────────────────────
async def _run_pipeline(obs: CaptureObserver, source: ScriptedAudioSource,
                        provider_call_id: str) -> list[tuple[float, float, str]]:
    """Build the real task with our source/sink, then drive the scripted conversation.

    Runs the PipelineTask and the audio-driving coroutine concurrently; when the script
    finishes it queues an EndFrame so the runner returns and the process can exit.
    """
    sink = CapturingSink()
    task, greet, last_activity, _state = await build_interview_task(source, sink)
    # build_interview_task constructs the task with PipelineParams defaults
    # (audio_in_sample_rate=16000, audio_out_sample_rate=24000), which already match our
    # synthesized 16 kHz input — no param override needed.
    task.add_observer(obs)
    quiet_windows: list[tuple[float, float, str]] = []  # (start, end, kind) per gap

    key = os.environ["DEEPGRAM_API_KEY"]
    log("Synthesizing candidate audio with Deepgram Aura …")
    name_role_pcm = await _synthesize(NAME_ROLE, key)
    answer_pcms = [await _synthesize(a, key) for a in ANSWERS]
    log(f"Audio ready: name+role {name_role_pcm.size/SAMPLE_RATE:.1f}s, "
        f"5 answers {[round(p.size/SAMPLE_RATE, 1) for p in answer_pcms]}")

    async def _wait_for_reply(after_count: int, timeout: float) -> bool:
        """Block until a new TTS utterance starts (tts_started grows past after_count)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(obs.tts_started) > after_count:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _drive() -> None:
        # 1) Greeting: the on_client_connected equivalent. The session-start clock (used
        #    for the greeting deadline) begins the instant greet() fires — after audio
        #    synthesis, so synthesis time never counts against the 3 s greeting budget.
        obs.mark_session_start()
        # Must match a seeded candidate's phone (same fallback bot() uses for the WebRTC
        # demo path) — since P1's invitation-code identity work, any unrecognized
        # from_number now gets challenged for an invitation code instead of greeted
        # directly, and this synthetic candidate never speaks one.
        demo_phone = os.getenv("DEMO_CALLER_PHONE", "+919000000001")
        await greet(provider_call_id=provider_call_id,
                    from_number=demo_phone, transport_kind="webrtc")
        log("Greeting kickoff queued; waiting for greeting TTS …")
        await _wait_for_reply(after_count=0, timeout=GREETING_DEADLINE_S + 5)

        # 2) name+role, then each answer. After every utterance: a silence gap; in the
        #    first gap we also inject a NOISE burst to prove it can't start a turn. We
        #    record each gap's absolute [start, end] window so the interruption gate can
        #    ask specifically: did anything interrupt DURING injected silence/noise?
        turns = [name_role_pcm, *answer_pcms]
        for idx, pcm in enumerate(turns):
            before = len(obs.tts_started)
            label = "name+role" if idx == 0 else f"answer {idx}"
            log(f"Speaking {label} ({pcm.size/SAMPLE_RATE:.1f}s) …")
            await source.push_audio(np.concatenate([_silence(LEAD_SILENCE_S), pcm]))

            # Silence gap. In the first gap, splice in a low-level noise burst.
            if idx == 0:
                log("Injecting NOISE burst into the silence gap …")
                gap = np.concatenate([
                    _silence(SILENCE_BETWEEN_TURNS_S / 2),
                    _noise(NOISE_BURST_S),
                    _silence(SILENCE_BETWEEN_TURNS_S / 2),
                ])
                kind = "silence+noise"
            else:
                gap = _silence(SILENCE_BETWEEN_TURNS_S)
                kind = "silence"
            gap_start = time.monotonic()
            await source.push_audio(gap)
            quiet_windows.append((gap_start, time.monotonic(), kind))

            ok = await _wait_for_reply(before, TURN_RESPONSE_TIMEOUT_S)
            if not ok:
                log(f"WARNING: no bot reply after {label} within "
                    f"{TURN_RESPONSE_TIMEOUT_S}s — continuing.")
                # Fail fast on a total provider outage: if the greeting never spoke AND
                # the provider is already 429-ing, the whole run is blocked — don't grind
                # through six more 30s waits.
                if not obs.tts_started and obs.rate_limited:
                    log("Provider rate-limited before any speech — aborting drive early.")
                    return

        # 3) Let the final "thanks, we'll be in touch" turn and DB stamp settle.
        log(f"Last answer sent; draining {FINAL_DRAIN_S}s for completion …")
        await asyncio.sleep(FINAL_DRAIN_S)

    # Drive the pipeline with the SAME official runner the live bot uses (task.run()
    # directly needs a WorkerParams the runner builds). We run the runner in the
    # background, drive the scripted conversation, then cancel the task deterministically
    # — queueing an EndFrame here instead stalls waiting for it to propagate through a
    # cascade STT that has already disconnected, so the process would hang until the
    # 5-minute idle timeout (observed). cancel() + a bounded wait returns promptly.
    from pipecat.pipeline.runner import PipelineRunner  # local: heavy import
    runner = PipelineRunner(handle_sigint=False)
    run_task = asyncio.ensure_future(runner.run(task))
    try:
        await _drive()
    finally:
        log("Drive complete; cancelling pipeline task …")
        await task.cancel(reason="selftest complete")
        try:
            await asyncio.wait_for(run_task, timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            run_task.cancel()
    log("Pipeline stopped.")
    return quiet_windows


def _resolve_session_id(provider_call_id: str) -> int | None:
    """Recover the assessment session created during this run.

    The tools bind `session_id` inside build_interview_task's closure, out of our reach,
    so we resolve it over HTTP: the api maps our unique provider_call_id → its Call row →
    the AssessmentSession opened against that call_id during the run."""
    with httpx.Client(base_url=API_BASE, timeout=10) as c:
        r = c.get("/assessment/by_call", params={"provider_call_id": provider_call_id},
                  headers=_AGENT_HEADERS)
        if r.status_code == 200:
            return r.json().get("session_id")
    return None


# ─────────────────────────── scoring ───────────────────────────
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _score(obs: CaptureObserver, summary: dict, answers: list[dict],
           quiet_windows: list[tuple[float, float, str]]) -> list[tuple]:
    """Return [(name, passed, measured)] for every standard in the gate."""
    results: list[tuple[str, bool, str]] = []
    session_start = obs.session_start or (obs.tts_started[0] if obs.tts_started else 0.0)

    # GREETING: TTS starts within the deadline OF SESSION START (when greet() fired, not
    # observer creation — audio synthesis precedes it), and asks for name + role.
    greet_t = (obs.tts_started[0] - session_start) if obs.tts_started else None
    greet_text = " ".join(t for _, t in obs.tts_text[:12]).lower()
    asks_name = "name" in greet_text
    asks_role = "role" in greet_text or "interview" in greet_text
    greet_ok = greet_t is not None and greet_t <= GREETING_DEADLINE_S and asks_name and asks_role
    results.append(("GREETING", greet_ok,
                    f"first TTS @ {greet_t:.2f}s (<= {GREETING_DEADLINE_S}s), "
                    f"asks_name={asks_name} asks_role={asks_role}"))

    # NO SPURIOUS INTERRUPTIONS: zero interruption broadcasts that land INSIDE an injected
    # silence/noise window. A broadcast fires on every legitimate barge-in/turn-start too,
    # so we don't count those — only interruptions during a gap prove noise/silence wrongly
    # cancelled a generation (the storm this pipeline was fixed to prevent).
    in_quiet = [t for t in obs.interruptions
                if any(lo <= t <= hi for lo, hi, _ in quiet_windows)]
    results.append(("NO SPURIOUS INTERRUPTIONS", len(in_quiet) == 0,
                    f"interruptions during silence/noise = {len(in_quiet)} (expect 0); "
                    f"{len(obs.interruptions)} total broadcasts, "
                    f"{len(quiet_windows)} quiet windows"))

    # REAL BARGE-IN: not exercised offline — see module docstring. Reported as SKIP.
    results.append(("REAL BARGE-IN", None, "SKIPPED (not feasible without audio loopback)"))

    # REPLY LATENCY: for each bot utterance after a user turn, end-of-turn → first TTS
    # token. End-of-turn is the UserStoppedSpeaking frame the aggregator emits when the
    # turn closes; TTFB is (first TTS token after it − that stop). We take the FIRST TTS
    # start after each user-stop so a multi-sentence reply counts its first token only.
    # p95 < 3.5s, none > 8s.
    ttfbs: list[float] = []
    for stop_t in obs.user_stops:
        later = [s for s in obs.tts_started if s >= stop_t]
        if later:
            ttfbs.append(later[0] - stop_t)
    p95 = _percentile(ttfbs, 0.95)
    worst = max(ttfbs) if ttfbs else 0.0
    lat_ok = bool(ttfbs) and p95 < TTFB_P95_MAX_S and worst <= TTFB_HARD_MAX_S
    results.append(("REPLY LATENCY", lat_ok,
                    f"n={len(ttfbs)} p95={p95:.2f}s (< {TTFB_P95_MAX_S}) "
                    f"max={worst:.2f}s (<= {TTFB_HARD_MAX_S})"))

    # NO BABBLE: no agent utterance apologizes for / narrates silence.
    babble = [t for _, t in obs.tts_text if _BABBLE_RE.search(t)]
    results.append(("NO BABBLE", len(babble) == 0,
                    f"babble utterances = {len(babble)} {babble[:3]}"))

    # PUNCTUATION/PROSODY: every non-trivial spoken utterance carries a sentence mark.
    # We join TTS text between consecutive TTSStarted boundaries into utterances.
    utterances = _group_utterances(obs)
    unmarked = [u for u in utterances if len(u.split()) >= 4 and not _SENTENCE_MARK_RE.search(u)]
    results.append(("PUNCTUATION/PROSODY", len(unmarked) == 0,
                    f"non-trivial utterances={len(utterances)} without a . ? ! = "
                    f"{len(unmarked)} {[u[:40] for u in unmarked[:2]]}"))

    # ANSWER ALIGNMENT: exactly 5 answers, positions 1..5, transcripts 1:1 with what we
    # injected (no dup, none shifted). We match on strong keywords per answer since STT
    # is lossy; alignment means answer i's transcript matches injected utterance i.
    positions = [a["position"] for a in answers]
    n_ok = len(answers) == 5 and positions == [1, 2, 3, 4, 5]
    align_ok, align_detail = _check_alignment(answers)
    results.append(("ANSWER ALIGNMENT", n_ok and align_ok,
                    f"count={len(answers)} positions={positions} :: {align_detail}"))

    # PERSISTED RESULT: session row carries the recruiter-facing aggregate.
    p = summary.get("persisted", {})
    name = summary.get("candidate_name")
    score = p.get("overall_score")
    persisted_ok = (
        bool(name)
        and isinstance(score, (int, float)) and 0.0 <= score <= 10.0
        and p.get("rating") is not None
        and isinstance(p.get("passed_count"), int)
        and p.get("completed_at") is not None
    )
    results.append(("PERSISTED RESULT", persisted_ok,
                    f"name={name!r} score={score} rating={p.get('rating')} "
                    f"passed={p.get('passed_count')} completed_at={p.get('completed_at')}"))

    return results


def _group_utterances(obs: CaptureObserver) -> list[str]:
    """Join TTS text fragments into utterances split on TTSStarted boundaries."""
    if not obs.tts_started:
        return []
    bounds = obs.tts_started + [float("inf")]
    utterances: list[str] = []
    for lo, hi in zip(bounds, bounds[1:]):
        text = "".join(txt for t, txt in obs.tts_text if lo <= t < hi).strip()
        if text:
            utterances.append(text)
    return utterances


# Distinctive keywords per injected answer (order matters — index == position-1).
_ANSWER_KEYWORDS = [
    ["closure", "function", "variable"],
    ["virtual", "dom", "diff"],
    ["debounc", "typing", "wait"],
    ["specificity", "style", "css"],
    ["accessibilit", "alt", "keyboard", "semantic"],
]


def _check_alignment(answers: list[dict]) -> tuple[bool, str]:
    """Each stored answer i should match injected utterance i's keywords, and no two
    should collide — proves nothing shifted or duplicated."""
    by_pos = {a["position"]: (a.get("transcript") or "").lower() for a in answers}
    hits, detail = [], []
    for pos in range(1, 6):
        text = by_pos.get(pos, "")
        kws = _ANSWER_KEYWORDS[pos - 1]
        matched = sum(1 for k in kws if k in text)
        ok = matched >= 1
        hits.append(ok)
        detail.append(f"p{pos}:{'ok' if ok else 'MISS'}({matched}/{len(kws)})")
    return all(hits), " ".join(detail)


def _print_scorecard(results: list[tuple], provider_call_id: str, session_id) -> bool:
    print("\n" + "=" * 72, flush=True)
    print(f" SELF-TEST SCORECARD   call={provider_call_id}  session={session_id}", flush=True)
    print("=" * 72, flush=True)
    all_pass = True
    for name, passed, measured in results:
        if passed is None:
            tag = "SKIP"
        elif passed:
            tag = "PASS"
        else:
            tag = "FAIL"
            all_pass = False
        print(f" [{tag}] {name:<26} {measured}", flush=True)
    print("=" * 72, flush=True)
    print(f" RESULT: {'PASS' if all_pass else 'FAIL'}", flush=True)
    print("=" * 72 + "\n", flush=True)
    return all_pass


async def main() -> int:
    provider_call_id = f"SELFTEST{int(time.time()*1000)}"
    obs = CaptureObserver()
    source = ScriptedAudioSource()

    log(f"Starting self-test run (call {provider_call_id}) …")
    quiet_windows = await _run_pipeline(obs, source, provider_call_id)

    session_id = _resolve_session_id(provider_call_id)
    if session_id is None:
        if obs.rate_limited:
            log("BLOCKED: the LLM provider returned 429 rate-limit for the whole run, so "
                "no assessment session was created. This is an ENVIRONMENT limit (Groq "
                "free-tier tokens-per-day exhausted), not an agent failure. Retry once the "
                "daily quota resets. Sample error: "
                f"{obs.errors[0][1] if obs.errors else '(none captured)'}")
            return 2
        log("ERROR: could not resolve the assessment session for this run "
            f"(no session, no rate-limit error). Captured errors: {obs.errors[:2]}")
        return 3

    with httpx.Client(base_url=API_BASE, timeout=10) as c:
        summary = c.get(f"/assessment/{session_id}/summary", headers=_RECRUITER_HEADERS).json()
        answers = c.get(f"/assessment/{session_id}/answers",
                        headers=_RECRUITER_HEADERS).json().get("answers", [])

    log(f"Captured: {len(obs.tts_started)} TTS utterances, "
        f"{len(obs.interruptions)} interruptions, {len(answers)} persisted answers.")
    results = _score(obs, summary, answers, quiet_windows)
    ok = _print_scorecard(results, provider_call_id, session_id)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
