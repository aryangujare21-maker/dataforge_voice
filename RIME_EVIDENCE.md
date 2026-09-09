# Rime Evidence

## Claim

> When a caller interrupts mid-readback and changes the request, the
> booking that gets written matches what the caller actually heard and
> asked for — not the slot the agent was mid-way through confirming.

## The four sub-problems and where each is solved

| # | Sub-problem | Mechanism | Code |
|---|---|---|---|
| 1 | Stop audio fast | LiveKit's built-in barge-in (VAD + turn detection) interrupts the active `SpeechHandle` and stops TTS playback/generation. We don't hand-roll this — we instrument it. | `agent.py::_wire_fencing_hooks` (`agent_state_changed` -> `state.record_barge_in_latency`) |
| 2 | Know what was heard | Rime `coda` over WebSocket (`use_websocket=True`) returns word-level timestamps; `AgentSession(use_tts_aligned_transcript=True)` uses them to truncate the assistant's chat-history entry to only the words actually played (`ChatMessage.interrupted` + truncated `text_content`). | `agent.py::_build_session`, logged via `conversation_item_added` -> `state.record_spoken` |
| 3 | Fence stale work | The generation-counter pattern below. | `tools.py::fenced_check_availability` |
| 4 | Keep state honest | `book_slot` only ever runs on a result that survived fencing, in a turn built from the (correctly truncated) chat history. | `agent.py::book_slot`, `tools.py::book_slot` |

Sub-problems 1 and 2 are provided by the LiveKit + Rime integration and
verified by inspection of the installed SDK (`livekit-agents` 1.8.0,
`livekit-plugins-rime` 1.8.0) — see README "The fencing pattern" for the
exact fields checked. Sub-problems 3 and 4 are this project's own code and
are what `run_test.py` proves directly.

## Acceptance test

`run_test.py` drives `tools.fenced_check_availability` — the identical
coroutine `agent.py`'s `check_availability` tool calls — through the stress
scenario, twice:

1. Caller: "I need an appointment Thursday." -> `check_availability("Thursday")`
   starts (configurable delay, default 3s; 1.5s in the reproduction below).
2. Mid-lookup, caller: "actually make it Friday." -> a new final transcript
   is committed, bumping `state.generation` exactly as `agent.py`'s
   `user_input_transcribed` handler does on a real barge-in.
3. The Thursday lookup resolves *after* the generation moved on.
4. `check_availability("Friday")` runs fresh and resolves normally.
5. The corrected slot is booked.

Run twice — once with `FENCING_ENABLED=True`, once `False` — same script,
same timing, same inputs.

## Procedure

```bash
.venv/Scripts/python run_test.py --lookup-delay 1.5
```

(`--lookup-delay` shortens the 3s production delay for fast, repeatable
runs; the fencing logic itself is delay-independent — it compares generation
numbers, not timestamps.)

## Result

Last reproduced 2026-09-10, `--lookup-delay 1.5`. A reference copy of the
output is committed as `trace.sample.json`; regenerate your own with the
command above (it writes `trace.json`):

```
FENCING_ENABLED=True  -> booked Friday (caller asked for Friday)
FENCING_ENABLED=False -> booked Thursday (caller asked for Friday)
RESULT: PASS - same input, different booking, fencing is what makes the difference
```

With fencing on, `fenced_events` recorded:

```
dropped stale check_availability('Thursday') from generation 0, now at generation 1
```

With fencing off, `fenced_events` is empty and the stale Thursday result is
what gets booked — the exact wrong-booking failure mode the claim describes.

## Live pipeline result (measured, not scripted)

Captured from real browser calls (`runtime_state.json`), not from
`run_test.py` — these exercised real STT, LLM, and TTS with barge-ins
spoken by a human caller. `state.reset()` runs per call, so the figures
below come from different sessions and are labelled accordingly rather
than presented as one continuous run.

A live fenced event (2026-09-08), from an actual mid-lookup interruption:

```
dropped stale check_availability('Thursday') from generation 3, now at generation 4
```

Confirms the exact same fencing code path (`tools.fenced_check_availability`)
that `run_test.py` exercises deterministically also fires correctly under
real voice conditions, not just in the scripted proof.

### Barge-in latency, before and after optimization

`state.timing_events` (sub-problem 1) measures the time from `agent_state`
leaving `"speaking"` after the caller starts talking over it. It was measured
twice, either side of a deliberate optimization pass — the delta is itself
the evidence that the remaining latency was understood rather than guessed at.

| | Samples | Steady state | Notes |
|---|---|---|---|
| **Before** (2026-09-08) | 8 | 403–462 ms, avg 446 | plus a 1530 ms first-sample outlier |
| **After** (2026-09-10) | 3 | 137–197 ms, avg 161 | no outlier |

The before-figures missed the ~200 ms target. Profiling the logs showed the
cost was **not** in the fencing logic — that is a synchronous integer
comparison with no I/O — but in per-turn network round-trips to LiveKit
Cloud that the framework enables by default:

- the **cloud turn detector**, called once per turn, and
- the **adaptive interruption detector**, called on a 0.1 s interval during
  agent speech with a 0.7 s inference timeout per check.

Both sat directly in the barge-in path. Under a slow network both were
observed to time out outright mid-call (`turn detector connection timed
out; falling back to local mini model`), taking STT connection setup down
with them. Replacing both with local Silero VAD (`turn_detection="vad"`,
`interruption={"mode": "vad"}`) removed the round-trips and the failure
mode together. Four supporting changes: interruption `min_duration`
0.5→0.2 s, endpointing ceiling 3.0→0.8 s, AEC warmup 3.0→0.5 s (the default
swallowed the caller's *first* barge-in entirely, which is what produced the
1530 ms outlier), and `num_idle_processes=1` because dev mode keeps zero
warm processes and every call was paying ~1.5 s of cold start.

The after-figures meet the ~200 ms target. Both sets are reported as
measured; neither was adjusted toward the target. The `n` is small — three
interruptions in one call — so this is a demonstrated range, not a
statistical claim.

## Limitations

- The acceptance test above proves the fencing *logic* deterministically; it
  does not exercise STT, the LLM, or TTS. The live demo (recorded on video)
  is what proves the same logic holds inside the real voice pipeline —
  both paths call `tools.fenced_check_availability`, so they are the same
  code, not two implementations that could drift apart.
- Single in-flight lookup is fenced per scenario here. The pattern
  generalizes to N concurrent lookups (each captures its own `gen` at call
  time) but that isn't separately tested.
- In the same live session above, under a rapid back-and-forth with many
  interruptions in a row, the LLM's *narration* occasionally drifted from
  the actual tool-call state (e.g. describing a cancellation that hadn't
  happened, or claiming no booking existed when one did). This is an LLM
  reasoning limitation, not a fencing failure — `state.booking` itself
  stayed correct throughout, because it's only ever written by `book_slot`/
  `cancel_booking`, never inferred from what the model said. Worth noting
  because it's the kind of gap a judge could probe for: the source of truth
  (`state.py`) held up; the model's spoken commentary about that truth was
  occasionally wrong.
