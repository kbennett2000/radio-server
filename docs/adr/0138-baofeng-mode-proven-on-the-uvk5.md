# 0138 — Baofeng mode works on the UV-K5: the dock is optional for repeater work

Status: Accepted

Executes Gates 0 and 1 of ADR [0137](0137-let-the-radio-be-a-radio.md).

## Context

ADR 0137 measured the dock path doing everything right on the air — tone 149x over floor, split on
the correct leg, all 41 presets arming correctly — and still no repeater opens. The operator's
question was the one worth answering instead:

> "I can buy this radio stock out of the box and program repeaters into it and it would work fine.
> So why don't we just load repeaters into the radio and select them from the UI?"

Two gates, both now run against hardware.

## Gate 0 — the AIOC's PTT line keys this radio

`scripts/bench/aioc_ptt_gate0.py`. Stops the uvk5 service, asserts one serial control line, asks the
kv4p witness whether RF appeared. The witness is what makes it an answer: the plan this came from
proposed watching the radio's TX LED, and an LED says "something happened", not "RF appeared on the
right frequency".

| line | witness RMS |
|---|---|
| baseline, nothing keyed | 0.0 |
| **DTR** | **1926.8**, and **1970.5** on a repeat |
| RTS | 0.0 |

DTR keys it. RTS does nothing — which confirms `DEFAULT_PTT_LINE = PttLine.DTR` (ADR 0029) on this
radio too, empirically rather than by inheritance.

## Gate 1 — and it actually talks

`scripts/bench/baofeng_mode_acceptance.py` drives the **real `AiocBaofeng` class** — the one
`server.backend = "baofeng"` instantiates — through a full `transmit()`, and asks the witness whether
the audio came back. Not a level: *is the tone I sent the tone I hear*, with an untransmitted 1600 Hz
band as a negative control so broadband noise cannot pass as success.

| run | recovered 1000 Hz | control 1600 Hz | ratio |
|---|---|---|---|
| 1 | 909.88 | 14.13 | 64x |
| 2 | 927.19 | 161.48 | 6x |
| 3 | 924.39 | 171.45 | 5x |
| 4 | 910.84 | 13.85 | 66x |

The recovered tone is **910–927 across every run**; only the noise band moves. `transmit()` blocked
for 5.03 s against a 5 s clip, so the blocking contract holds.

**The tone went out through the AIOC's sound card, the radio's own firmware transmitted it, and it
came back.** No dock, no register writes, no CAT. Every register question this arc has chased for
four cycles is moot for repeater work, because the firmware does what it does under a human thumb.

## The finding that nearly faked a failure

The first Gate 1 run produced **no RF at all** — no exception, correct 5 s block, and silence. A
later back-to-back pair failed the same way, and so did a bare `ptt(True)` control.

Cause: radio-server holds the UV-K5 inside the dock's `0x0870` full-control loop, which blocks the
firmware's main loop — and **the main loop is what samples the hardware PTT pin** (`app.c:1441`,
ADR 0120's starvation finding). Stopping the service *mid-handshake* leaves the radio inside that
loop with nothing left to send `0x0871`, and it then ignores its own PTT input entirely.

Measured, not inferred:

| dock session up for | then stopped, DTR asserted |
|---|---|
| ~8 s | **deaf** at +2 s, +8 s and +20 s |
| ~40 s | keys first try — twice, 1926.8 and 1970.5 |

A wedged radio and a radio the AIOC cannot key are **identical at the witness**, and that is exactly
the question these scripts exist to answer. So both now refuse to confuse them:

- neither will stop a dock session that has been up less than `SETTLED_SECONDS` (30 s);
- when a transmit produces no RF, Gate 1 re-tests a **bare PTT assert from the same process**, and
  reports **INCONCLUSIVE** rather than failure if that is silent too, naming the wedge as the likely
  cause.

Same error class as ADR 0136's window and ADR 0137's disagreeing controls: an instrument that cannot
tell "no signal" from "no measurement" will eventually report the wrong one.

**Consequence: dock mode and Baofeng mode are mutually exclusive.** The radio cannot be a register
slave and a radio at the same time, and there is only one AIOC serial interface (`ttyACM0`) so the
dock link and the PTT line cannot both be open anyway.

## What Baofeng mode costs

Honest list, all confirmed in source:

- **Arbitrary frequency, split, tone and mode become `501`.** Only what the operator has dialled or
  stored on the radio is reachable. `chirp.py` still fills `[[presets]]`, but the radio holds its own
  copy and nothing reconciles them.
- **No read-back of anything.** There is no path in this repo to read the radio's front-panel VFO,
  which is why both scripts require the operator to park it on the bench frequency and why the
  witness cross-check is mandatory rather than decorative.
- **No confirmed keying.** `_confirm_keyed` (`radio.py:875-911`) proves the dock is transmitting; DTR
  has no such confirmation.
- **Frequency-addressed scanning is impossible** (`scan/engine.py:97-118`).

## Decision

For repeater work on the UV-K5, run it as `server.backend = "baofeng"`: the operator selects the
repeater channel on the radio, radio-server supplies audio and PTT. That is the project brief's
Baofeng mode, it needs **no new backend class**, and it is now bench-proven on this radio.

The dock stays for what only it can do — arbitrary tuning, split, read-back — and is the right mode
when the operator wants radio-server to drive the frequency. It is not the right mode for "open a
repeater".

## What this does not claim

- **No repeater has been opened yet.** Everything here is bench frequencies into a witness inches
  away. The real acceptance is a courtesy tone coming back, and that needs the operator to select a
  repeater channel and let the service transmit through it.
- **Absolute RF power is still unmeasured** — ADR 0137's open gap, unchanged. A witness this close
  would hear a microwatt, so none of these numbers speak to whether the radio reaches a repeater.
- **Nothing on 2 m was measured.** SA818-UHF witness.
- The audio *level* into the radio is unexamined — `transmit()` was fed a 0.5-amplitude tone and the
  result was only ever read as a ratio against a control band, never as deviation.
