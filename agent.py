"""LiveKit voice agent for clinic appointment booking.

Pipeline: browser mic -> LiveKit -> Deepgram (STT) -> Groq/GPT-OSS-120B (LLM) ->
Rime/coda (TTS, streamed over WebSocket for word-level timestamps) -> speaker.

Rime is the spoken output for every turn; everything else is ours. See
RIME_EVIDENCE.md for the interruption/fencing claim this file exists to prove,
and README.md for the exact model/voice/endpoint configuration.
"""
from __future__ import annotations

import logging
import time

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    RunContext,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.llm import ChatMessage, StopResponse
from livekit.agents.voice.turn import (
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)
from livekit.plugins import deepgram, groq, rime, silero

import tools
from state import state

load_dotenv()
logger = logging.getLogger("dataforge-voice")

INSTRUCTIONS = """
You are Aria, a cheerful clinic scheduling assistant answering a phone line.
Callers book, reschedule, or cancel appointments by speaking to you.

- Always read back available slots in full, in one sentence, e.g. "I have
  Thursday at 2, Thursday at 4:30, Friday at 11, and Friday at 3:15." before
  asking the caller to choose.
- If the caller talks over you and changes their request, follow the NEW
  request. Never act on what you were about to say before they cut in.
- Call check_availability(day) to look up open slots before reading them back.
- Call book_slot(slot_id) only after the caller has confirmed one specific slot.
- If the caller wants to cancel their appointment, confirm which one if it's
  not obvious, then call cancel_booking. To reschedule, just book a new slot
  -- it replaces the old one.
- This is a phone call, not a chat window: keep every other response short.
"""


@function_tool
async def check_availability(context: RunContext, day: str) -> str:
    """Look up open appointment slots for a day.

    Args:
        day: The day name to check, e.g. "Thursday" or "Friday".
    """
    try:
        slots = await tools.fenced_check_availability(day)
    except tools.StaleLookup:
        # The caller already moved on. Per the fencing pattern: don't speak
        # this, don't write it, don't even let the LLM see a stray result.
        raise StopResponse() from None

    if not slots:
        return f"No open slots on {day}."
    readable = ", ".join(f"{s['day']} at {s['time']}" for s in slots)
    return f"Open slots: {readable}."


@function_tool
async def book_slot(context: RunContext, slot_id: str) -> str:
    """Book a specific appointment slot by id, as returned by check_availability.

    Args:
        slot_id: The slot id to book (e.g. "thu-1400").
    """
    slot = await tools.book_slot(slot_id)
    if slot is None:
        return f"No such slot: {slot_id}."
    state.book(slot["id"], slot["day"], slot["time"])
    return f"Booked {slot['day']} at {slot['time']}."


@function_tool
async def cancel_booking(context: RunContext) -> str:
    """Cancel the caller's currently booked appointment, if any."""
    cancelled = state.cancel_booking()
    if cancelled is None:
        return "There's no appointment currently booked to cancel."
    return f"Cancelled the {cancelled['day']} at {cancelled['time']} appointment."


def _build_session(vad: silero.VAD) -> AgentSession:
    return AgentSession(
        stt=deepgram.STT(model="nova-3", language="en-US"),
        # Groq: free tier, no billing required, and fast enough to matter for
        # a real-time voice agent (this is the same reason latency-sensitive
        # voice stacks reach for Groq over a standard hosted LLM API).
        # Verified live against Groq's /v1/models on 2026-09-08 -- their
        # catalog changes, so re-check before relying on this if it's been
        # a while (see README).
        llm=groq.LLM(model="openai/gpt-oss-120b", reasoning_effort="low"),
        # use_websocket=True is required for Coda's word-level timestamps,
        # which is what lets use_tts_aligned_transcript truncate the chat
        # history to what the caller actually heard on a barge-in.
        tts=rime.TTS(model="coda", speaker="lyra", lang="eng", use_websocket=True),
        vad=vad,
        use_tts_aligned_transcript=True,
        # The browser's echo canceller needs a moment to converge, and until
        # it does the agent can hear itself and self-interrupt. The 3s
        # default blocks the caller's first barge-in entirely -- which is
        # the exact thing this demo is about. Short window + headphones.
        aec_warmup_duration=0.5,
        turn_handling=TurnHandlingOptions(
            # Local Silero VAD only. The default turn detector and the
            # "adaptive" interruption detector both call LiveKit Cloud
            # (agent-gateway) per decision, with a 0.7s inference timeout --
            # latency on every turn, and an outright failure mode when the
            # network is slow (observed: both timed out mid-call).
            turn_detection="vad",
            interruption=InterruptionOptions(mode="vad", min_duration=0.2),
            # Defaults wait up to 3s before committing a turn the model is
            # unsure about. Our utterances are short and unambiguous (a day,
            # a time, yes/no), so that ceiling is pure latency, not safety.
            endpointing=EndpointingOptions(mode="fixed", min_delay=0.2, max_delay=0.8),
            # Run Rime ahead of turn confirmation too, not just the LLM, so
            # the first audio frame is ready the moment the turn commits.
            preemptive_generation=PreemptiveGenerationOptions(
                enabled=True, preemptive_tts=True
            ),
        ),
    )


def _wire_fencing_hooks(session: AgentSession) -> None:
    """Bump the generation clock on every committed user turn (a fresh
    request or a barge-in both look the same here: new final transcript),
    and log the interruption/timing evidence the web panel and
    RIME_EVIDENCE.md draw on."""

    barge_in_started_at: dict[str, float] = {}

    @session.on("user_state_changed")
    def _on_user_state_changed(ev: UserStateChangedEvent) -> None:
        if ev.new_state == "speaking" and session.agent_state == "speaking":
            barge_in_started_at["ts"] = time.time()

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final:
            state.bump_generation()

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev) -> None:  # noqa: ANN001 - AgentStateChangedEvent
        if ev.old_state == "speaking" and ev.new_state != "speaking":
            started = barge_in_started_at.pop("ts", None)
            if started is not None:
                state.record_barge_in_latency((time.time() - started) * 1000)

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if isinstance(item, ChatMessage) and item.role == "assistant":
            state.record_spoken(item.text_content or "", interrupted=item.interrupted)


def prewarm(proc: JobProcess) -> None:
    """Load the VAD model when the worker process starts, not when the first
    call arrives -- otherwise the first caller pays for it mid-conversation."""
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    state.reset()
    session = _build_session(ctx.proc.userdata["vad"])
    _wire_fencing_hooks(session)

    await session.start(
        agent=Agent(instructions=INSTRUCTIONS, tools=[check_availability, book_slot, cancel_booking]),
        room=ctx.room,
    )


if __name__ == "__main__":
    # dev mode keeps 0 idle processes by default, so every call pays ~1.5s to
    # spawn a process and load the VAD model before the caller is even heard.
    # One warm spare removes that from the first call.
    cli.run_app(
        WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, num_idle_processes=1)
    )
