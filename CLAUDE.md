# CLAUDE.md — Voice Appointment Agent (DataForge / Rime track)

## Context

Hackathon submission. **~22 hours left, solo, 2nd-year student.** Optimize for a
working, submittable artifact over completeness. Ship small and correct.

The developer must be able to defend every line in a live judging session. Do not
generate code they cannot explain. Prefer obvious code over clever code. Add a
short comment on anything non-trivial.

## What we are building

A **clinic appointment booking voice line**. The caller books, reschedules, or
cancels an appointment by speaking. There is no screen for the caller — voice is
the only channel.

**Rime is the primary spoken output.** Everything else (STT, LLM, orchestration,
state) is ours.

## The hard voice problem: interruption and recovery

**Central claim** (goes in `RIME_EVIDENCE.md`, must be provable):

> When a caller interrupts mid-readback and changes the request, the booking that
> gets written matches what the caller actually heard and asked for — not the slot
> the agent was mid-way through confirming.

Four sub-problems to solve:

1. **Stop audio fast.** Kill local playback and stop Rime generation on barge-in.
   Flush buffered audio. Target under ~200ms.
2. **Know what was heard.** Truncate LLM history to the words that actually played,
   using Coda's word-level timestamps. Not the full text we sent.
3. **Fence stale work.** A calendar lookup that returns *after* an interrupt is
   answering an obsolete question. Drop it — never speak it, never write it.
4. **Keep state honest.** The booking write must reflect the corrected request.

## Architecture

```
Browser mic → LiveKit Agents → STT → LLM → Rime (Coda) → speaker
                                      ↓
                              CalendarState (dict)
                                      ↓
                            on-screen booking panel
```

Single Python agent process + a simple web page showing live state.

### State shape

```python
state = {
    "slots": [...],          # hardcoded availability, synthetic
    "booking": None,         # the one write that matters
    "generation": 0,         # incremented on every user turn / interrupt
    "fenced_events": [],     # displayed on screen as proof
    "spoken_log": [],        # what the caller actually heard
}
```

### The fencing pattern (core of the submission)

```python
gen = state["generation"]
result = await check_availability(day)
if gen != state["generation"]:
    state["fenced_events"].append(f"dropped stale lookup for {day}")
    return  # do not speak, do not write
# only now safe to speak or write
```

Gate it behind a module-level `FENCING_ENABLED = True` so the A/B demo can flip it.

## Files to produce

| File | Purpose |
|---|---|
| `agent.py` | LiveKit agent: STT → LLM → Rime, barge-in handling, fencing |
| `state.py` | CalendarState, generation counter, fenced-event log |
| `tools.py` | `check_availability()` with `await asyncio.sleep(3)`; `book_slot()` |
| `run_test.py` | Scripted replay of the stress case; emits a JSON trace |
| `web/index.html` | Booking panel, fenced-events log, active-provider badge |
| `README.md` | Setup, architecture, limitations, exact Rime config |
| `RIME_EVIDENCE.md` | Claim, acceptance test, procedure, result, limitations |
| `.env.example` | Placeholders only |

## Design requirements

- **Long utterances are required.** The agent must read slots back in full:
  "I have Thursday at 2, Thursday at 4:30, Friday at 11, and Friday at 3:15."
  Short replies leave nothing to interrupt.
- **The slow tool is deliberate.** `check_availability()` sleeps 3 seconds. This is
  the in-flight work that must be fenced. Keep the delay configurable.
- **State must be visible.** The web panel shows the current booking and every
  fenced event as it happens. Judges watch this, not the logs.
- **Provider must be observable.** Render a badge: `Speech provider: Rime (coda)`.

## Rime configuration

- Model: `coda` (flagship; word-level timestamps for interruption handling)
- Verify the exact model / voice / language combination against Rime's **live
  catalog at build time** — do not hardcode a speaker list copied from docs.
- Record in the README: model ID, speaker, language, endpoint, audio format,
  transport. These are required fields.
- Use the regional endpoint closest to deployment.

## The demo (record by hour 17, no exceptions)

4–5 minutes. Must show: the user, the problem, the normal flow, the stress case,
the measured result, and which speech provider is active.

**Normal run:** "I need an appointment Thursday." Agent reads slots. Caller picks
4:30. Panel shows Thursday 4:30.

**Stress run:** Agent mid-readback — "Thursday at 2, Thursday at 4:30, Frid—" —
caller cuts in: "actually make it Friday." The Thursday lookup is still in flight.
Audio stops. Stale result returns and is fenced (visible on screen). Panel shows a
Friday slot.

**A/B:** flip `FENCING_ENABLED = False`, run the identical script. Panel books
Thursday. Same input, wrong booking. This contrast is the evidence.

## Build order

Stop wherever you are at hour 17 and record the video regardless.

1. LiveKit + Rime quickstart — talk to it once, change nothing
2. Booking panel rendering state
3. Slot readback (long utterance)
4. 3-second lookup delay
5. Barge-in: stop audio, bump generation
6. Fencing check
7. Fenced-events log + `run_test.py` JSON trace
8. README, RIME_EVIDENCE.md, .env.example
9. Video

## Hard rules

- **Never commit an API key.** A live credential in the repo voids the submission.
  `.env.example` gets placeholders only.
- **Synthetic data only.** Fake patient names, fake slots. Required for healthcare
  workflows.
- **No unverified performance numbers.** If we claim a latency, it comes from the
  trace file. Otherwise don't claim it.
- **Rime must be central**, not a greeting or a confirmation beep. Incidental use
  is a disqualifier.
- **Demoed behavior must exist in the repo.** Judges may ask for a live rerun.
- Disclose any fallback provider and keep Rime as the default judged path.

## Out of scope — do not build

Real telephony, phone-screen control, auth, database persistence, multi-user,
multi-language, custom UI polish. Note telephony as future work in the README.

## Scoring weights (what to optimize)

| Criterion | Weight |
|---|---|
| Problem and necessity of voice | 25% |
| Hard voice engineering | 25% |
| Rime integration and voice experience | 20% |
| Evidence and reproducibility | 20% |
| Demo clarity | 10% |

Evidence is worth as much as the integration. A rough build with a clean,
reproducible test beats a polished one with no proof.
