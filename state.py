"""In-memory calendar/call state, shared between agent.py and the web panel.

Single-process, single-active-call design (see README "Out of scope"). State
is mirrored to a JSON file on every mutation so web_server.py (a separate
process) can serve it to the browser without shared memory or a DB.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_FILE = Path(os.environ.get("DATAFORGE_STATE_FILE", Path(__file__).parent / "runtime_state.json"))

# Synthetic, hardcoded availability. Fake data only, per the hard rules.
SLOTS: list[dict[str, str]] = [
    {"id": "thu-1400", "day": "Thursday", "time": "2:00 PM"},
    {"id": "thu-1630", "day": "Thursday", "time": "4:30 PM"},
    {"id": "fri-1100", "day": "Friday", "time": "11:00 AM"},
    {"id": "fri-1515", "day": "Friday", "time": "3:15 PM"},
]

FENCING_ENABLED = os.environ.get("DATAFORGE_FENCING_ENABLED", "true").lower() != "false"


@dataclass
class CalendarState:
    """The one source of truth for the demo. `generation` is the fencing clock:
    it's bumped on every new committed user turn (including a barge-in), and
    any in-flight lookup that returns after the generation has moved on is
    answering a question nobody is asking anymore."""

    booking: dict[str, Any] | None = None
    generation: int = 0
    fenced_events: list[dict[str, Any]] = field(default_factory=list)
    spoken_log: list[dict[str, Any]] = field(default_factory=list)
    timing_events: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "Rime (coda)"
    caller_name: str = "Aria"

    def bump_generation(self) -> int:
        self.generation += 1
        self._flush()
        return self.generation

    def record_spoken(self, text: str, interrupted: bool) -> None:
        self.spoken_log.append(
            {"text": text, "interrupted": interrupted, "ts": time.time()}
        )
        self._flush()

    def fence(self, description: str) -> None:
        """Record a dropped stale result. Called only when a lookup's captured
        generation no longer matches the live one."""
        self.fenced_events.append({"description": description, "ts": time.time()})
        self._flush()

    def book(self, slot_id: str, day: str, time_str: str) -> None:
        self.booking = {"slot_id": slot_id, "day": day, "time": time_str, "ts": time.time()}
        self._flush()

    def cancel_booking(self) -> dict[str, Any] | None:
        """Clear the current booking, if any. Returns what was cancelled."""
        cancelled = self.booking
        self.booking = None
        self._flush()
        return cancelled

    def record_barge_in_latency(self, latency_ms: float) -> None:
        """How long audio kept playing after the user started talking over it."""
        self.timing_events.append({"barge_in_latency_ms": latency_ms, "ts": time.time()})
        self._flush()

    def reset(self) -> None:
        self.booking = None
        self.generation = 0
        self.fenced_events = []
        self.spoken_log = []
        self.timing_events = []
        self._flush()

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking": self.booking,
            "generation": self.generation,
            "fenced_events": self.fenced_events,
            "spoken_log": self.spoken_log,
            "timing_events": self.timing_events,
            "provider": self.provider,
            "caller_name": self.caller_name,
            "fencing_enabled": FENCING_ENABLED,
            "slots": SLOTS,
        }

    def _flush(self) -> None:
        STATE_FILE.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


state = CalendarState()
state._flush()  # ensure runtime_state.json always matches the real schema,
                 # even before agent.py's entrypoint has run once
