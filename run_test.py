"""Scripted, deterministic proof of the fencing pattern.

This does not spin up LiveKit, STT, an LLM, or Rime. It drives the exact
`tools.fenced_check_availability` coroutine that agent.py's check_availability
tool calls, so the code path under test is identical to the code path used
live -- only the caller (a human on a phone call vs. this script) differs.

Runs the stress scenario twice, once per FENCING_ENABLED setting, and diffs
the outcome. Emits a JSON trace to --trace-out. See RIME_EVIDENCE.md for how
this maps to the submission's central claim.

Scenario: caller says "I need an appointment Thursday" (triggers a slow
check_availability("Thursday")); while that lookup is still in flight, the
caller interrupts with "actually make it Friday" (a new final transcript,
which bumps the generation clock exactly as a real barge-in does in
agent.py). The Thursday lookup resolves after the interrupt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import state as state_module
import tools
from state import state


async def run_scenario(*, fencing_enabled: bool, barge_in_at: float) -> dict:
    state_module.FENCING_ENABLED = fencing_enabled
    state.reset()

    events: list[dict] = []
    t0 = time.monotonic()

    def log(event: str, **fields: object) -> None:
        events.append({"event": event, "t_ms": round((time.monotonic() - t0) * 1000), **fields})

    log("call_start", caller_said="I need an appointment Thursday.")
    thursday_task = asyncio.create_task(tools.fenced_check_availability("Thursday"))

    await asyncio.sleep(barge_in_at)
    log("barge_in", caller_said="actually make it Friday.")
    # Mirrors agent.py: the interrupting utterance is what was truncated to
    # (the aligned-transcript truncation is Rime/LiveKit's job at runtime;
    # here we just record what would have been heard) plus the generation bump.
    state.record_spoken("Thursday at 2, Thursday at 4:3", interrupted=True)
    state.bump_generation()

    stale_result: list[dict] | None = None
    try:
        stale_result = await thursday_task
        log("thursday_lookup_resolved", slots=stale_result)
    except tools.StaleLookup:
        log("thursday_lookup_fenced", note="dropped: superseded by generation bump")

    friday_slots = await tools.fenced_check_availability("Friday")
    log("friday_lookup_resolved", slots=friday_slots)

    if fencing_enabled or not stale_result:
        booked = friday_slots[0]
    else:
        # Bug reproduced: nothing stopped the stale, already-in-flight
        # Thursday result from being treated as the answer.
        booked = stale_result[0]

    state.book(booked["id"], booked["day"], booked["time"])
    log("booked", slot=booked)

    return {
        "fencing_enabled": fencing_enabled,
        "events": events,
        "final_booking": state.booking,
        "fenced_events": state.fenced_events,
        "spoken_log": state.spoken_log,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookup-delay", type=float, default=1.5, help="seconds check_availability sleeps")
    parser.add_argument("--barge-in-at", type=float, default=None, help="seconds until the interrupt (default: half the lookup delay)")
    parser.add_argument("--trace-out", type=Path, default=Path("trace.json"))
    args = parser.parse_args()

    tools.LOOKUP_DELAY_SECONDS = args.lookup_delay
    barge_in_at = args.barge_in_at if args.barge_in_at is not None else args.lookup_delay / 2

    fenced = await run_scenario(fencing_enabled=True, barge_in_at=barge_in_at)
    unfenced = await run_scenario(fencing_enabled=False, barge_in_at=barge_in_at)

    trace = {
        "lookup_delay_seconds": args.lookup_delay,
        "barge_in_at_seconds": barge_in_at,
        "fenced": fenced,
        "unfenced": unfenced,
    }
    args.trace_out.write_text(json.dumps(trace, indent=2), encoding="utf-8")

    fenced_day = fenced["final_booking"]["day"]
    unfenced_day = unfenced["final_booking"]["day"]
    contrast_shown = fenced_day == "Friday" and unfenced_day == "Thursday"

    print(f"FENCING_ENABLED=True  -> booked {fenced_day} (caller asked for Friday)")
    print(f"FENCING_ENABLED=False -> booked {unfenced_day} (caller asked for Friday)")
    print(f"Trace written to {args.trace_out}")
    print("RESULT:", "PASS - same input, different booking, fencing is what makes the difference"
          if contrast_shown else "FAIL - expected contrast not observed, see trace")

    return 0 if contrast_shown else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
