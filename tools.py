"""Plain async functions the LiveKit function-tools in agent.py call into.

Kept separate from agent.py (no LiveKit imports here) so run_test.py can
exercise the fencing logic without spinning up a real voice session.
"""
from __future__ import annotations

import asyncio
import os

import state as state_module
from state import SLOTS, state

# The deliberately slow tool: this is the in-flight work that must be fenced.
# Configurable so run_test.py can shrink it for fast scripted replay.
LOOKUP_DELAY_SECONDS = float(os.environ.get("DATAFORGE_LOOKUP_DELAY", "3.0"))


class StaleLookup(Exception):
    """Raised by fenced_check_availability when the generation moved on while
    the lookup was in flight. Callers must not speak or write the result."""

    def __init__(self, day: str) -> None:
        super().__init__(f"stale availability lookup for {day!r}")
        self.day = day


async def check_availability(day: str) -> list[dict[str, str]]:
    """Look up open slots for a day. Slow on purpose (LOOKUP_DELAY_SECONDS)
    to create the in-flight-work window the fencing pattern has to close."""
    await asyncio.sleep(LOOKUP_DELAY_SECONDS)
    return [slot for slot in SLOTS if slot["day"].lower() == day.lower()]


async def fenced_check_availability(day: str) -> list[dict[str, str]]:
    """The fencing pattern (core of the submission): capture the generation
    clock, await the slow lookup, then refuse the result if a newer user turn
    or a barge-in has already moved the generation on. Both agent.py (the
    live voice path) and run_test.py (the scripted proof) call this same
    function, so the demoed behavior and the tested behavior are one code
    path, not two. Reads state_module.FENCING_ENABLED (not a copied name) so
    run_test.py can flip it at runtime for the A/B comparison."""
    gen = state.generation
    slots = await check_availability(day)
    if state_module.FENCING_ENABLED and gen != state.generation:
        state.fence(
            f"dropped stale check_availability({day!r}) from generation {gen}, "
            f"now at generation {state.generation}"
        )
        raise StaleLookup(day)
    return slots


async def book_slot(slot_id: str) -> dict[str, str] | None:
    """Resolve a slot id to its (day, time). Instant: this is the write step,
    not the slow one, so it never needs fencing on its own."""
    for slot in SLOTS:
        if slot["id"] == slot_id:
            return slot
    return None
