# 0045 — Link audio correctness: DTMF mute on the Mumble feed; no busy-latch under squelch off

Status: Accepted, with the DTMF-mute mechanism superseded by
[ADR 0049](0049-realtime-dtmf-mute-and-yield.md) — the retroactive-decode delay line here is replaced
by real-time tone detection. The other half of this ADR (no Mumble→RF busy-latch under `squelch =
"off"`) remains live and unchanged.

## Context

Two field reports against the Mumble link (ADR 0041/0042):

1. **DTMF control tones are audible in the Mumble channel.** The bridge relays every AudioHub
   frame verbatim, and DTMF is decoded (multimon-ng) *after* the tone audio has already fanned
   out — there is no real-time "tone present" signal to gate on.
2. **Mumble→RF never keys, while RF→Mumble works.** The bridge defers keying while
   `rx_pump.active` is true ("don't double onto a live signal"). Under the deployment's
   `audio.squelch = "off"` (ADR 0040), the pass-through gate opens on **every** frame and never
   rejects one — and on real hardware `receive()` yields continuous non-empty PCM — so `active`
   latches `True` at the first frame and every Mumble frame is silently dropped forever. The
   mock-backend tests never caught it: the mock's empty frames keep `active` `False`.

## Decision

**DTMF mute (leak):** the bridge's RF→Mumble task runs a short **delay line**
(`DEFAULT_DTMF_MUTE_DELAY = 0.3 s`, a marked verify-against-hardware constant) behind live. The
DTMF inputs' per-key `on_digit` hook is surfaced through a public `Controller.on_digit`, wired by
the composition root to a shared `DtmfMuteGate` (`radio_server/link/mute.py`). When a digit
decodes, the bridge condemns everything buffered — the already-published tone onset never leaves
the delay line — and stays muted for `mumble.dtmf_mute_hold` (default 1.0 s, re-armed per digit).
When the hub goes quiet for a full delay window the buffer **flushes** (or clears, if muted): a
real squelch gate stops publishing between overs, and without the idle flush the tail of every
over would stall and replay stale. `mumble.dtmf_mute = false` restores the raw zero-latency
relay. Scope is the Mumble feed only — browser listeners and recordings still carry the tones
(the operator's own monitor should be faithful; a follow-up can revisit).

**Busy latch (silence):** gates now declare whether "open" means anything —
`detects_signal = True` on `AudioLevelGate`/`CatBusyGate`, `False` on the pass-through gate. The
pump only asserts `active` off a signal-aware gate. This **amends ADR 0041's collision policy**:
the defer applies only when the deployment actually has signal knowledge; under `squelch = "off"`
the bridge keys whenever the talker slot is free, and doubling onto a signal nobody detected is
accepted (the half-duplex radio was equally blind before). So this class of failure is never
silent again, the bridge counts every inbound Mumble frame into exactly one bucket
(`frames_in`, `dropped_rx_active`, `dropped_slot_busy`, `overs_keyed`) and `GET /link/status`
surfaces the block on the active entry.

## Consequences and trade-offs accepted

- RF→Mumble audio arrives ~0.3 s late when the mute is on. Accepted: a conference bridge is not
  a repeater path; inaudible control codes are worth more than 300 ms.
- The delay constant must exceed multimon's tone-onset→report latency or a leading blip of the
  first tone leaks. It is a bench-verifiable marked default (guardrail 1); raise to 0.4 if the
  HT test leaks.
- Slow hand-dialing with >1 s gaps lets inter-digit channel audio through — by design (it is
  legitimate RF audio); each tone's own onset is still swallowed retroactively.
- Under `squelch = "off"` the operating log no longer shows a (permanently latched, meaningless)
  "signal (squelch open)" line, and Mumble talkers can double onto an RF signal the server cannot
  detect. Both are the honest behavior: no squelch means no signal knowledge.
