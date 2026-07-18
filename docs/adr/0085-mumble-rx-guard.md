# 0085 — A post-transmit RX guard: keep the TX→RX turnaround transient off Mumble

Status: Accepted

## Context

Over Mumble on the **AIOC/UV-5R**: an operator talks from the phone (Mumble→RF), releases PTT, the
radio drops — and the phone then hears a short (~0.25 s) buzz. It is the UV-5R's receiver recovering
at the **TX→RX turnaround**: the FM front-end unmutes before its squelch has settled, so a burst of
receiver hash comes out of the AIOC sound card and — because `audio.squelch="off"` passes everything
— is relayed straight to Mumble as the operator's own tail.

It is **AIOC-only**. The kv4p's SA818 module applies hardware squelch, so the transient never reaches
the wire; there is nothing to suppress there.

The duplex arbiter (ADR 0017) resumes RX the instant TX releases, with no guard — and deliberately
so: its docstring states *"the real PTT-tail / TX-to-RX turnaround timing is a bench fact (guardrail
1), not modeled here."* This ADR adds exactly that timing, as a small, opt-out guard rather than
baking a hardware constant into the arbiter.

## Decision

On the arbiter's **TX→RX edge** — any local TX source releasing — arm a short **RX guard** window
during which the **RF→Mumble relay is suppressed**, dropping the turnaround transient before it
reaches Mumble.

- **Keyed off the arbiter, not the bridge.** The guard is armed by the arbiter's `on_change` when the
  derived mode leaves `TRANSMITTING` (the composition root tracks the prior mode and calls
  `rx_guard.mute_for(window)`). The arbiter is **source-agnostic**: both the Mumble bridge's own
  Mumble→RF transmit and a **browser talker** (`/audio/tx`) funnel through `arbiter.release_tx()`, so
  a browser talker's release arms the guard exactly the same way — both produce the same turnaround.
- **The guard is the ADR 0049 timed latch.** `DtmfMuteGate` is reused a third time (after the DTMF
  mute and the ADR 0050 operator-talk yield) as a plain "suppress now, for N seconds" latch on the
  injectable monotonic clock. It is created **app-scoped** (it must outlive the per-connect bridge
  rebuilds, ADR 0042) and injected into `MumbleBridge` as `rx_guard`; `None` keeps the raw relay.
- **Scoped to the RF→Mumble relay only.** The bridge polls `self._rx_guard.muted()` at the top of
  `_rx_to_mumble` (beside the existing operator-talk yield) and drops the frame, counting it as
  `rx_guarded` in `tx_stats()` for `/link/status` diagnosability. Suppression lives in the bridge's
  relay loop, **not** at the `AudioHub`, so **browser Listen** (a separate hub subscriber) and the
  **recorder** are untouched — recording never loses audio to the guard.
- **Config: `mumble.rx_guard_seconds`,** default **0.4 s**, an advanced/bench-tuning knob
  (`coerce_nonneg_float`). Marked "verify on-air" (guardrail 1 — turnaround duration is a per-radio
  hardware fact). **0 disables** the guard (relay resumes the instant TX releases — today's
  behaviour).

### Why not broaden it

The reported symptom is the phone hearing its own turnaround, so the guard is scoped to the Mumble
feed. **Browser Listen** could reuse the very same latch if it shows the same buzz — but it is not
broadened without cause, and the recorder in particular must not drop audio. If a browser-Listen buzz
is confirmed on the bench, extending the check to that subscriber is a one-line follow-up.

## Consequences

- After a transmit ends, the post-release buzz no longer reaches Mumble on the AIOC.
- **Backend-agnostic and kv4p-benign:** the kv4p has no transient to suppress, and a brief post-TX RX
  mute on it is harmless (a keyed radio is deaf during that window anyway).
- Keep the window **short**: it clips the very start of a reply that begins inside the guard, so a
  fast back-and-forth QSO wants a small value. Tune `mumble.rx_guard_seconds` to the radio.
- No `mumble.tx_hang` change (that is the Mumble→RF quiet window — the RX side after TX is orthogonal),
  no AGC/noise-gate (a timed guard is the fix), no new backends.

**UI note (bundled, unrelated):** all collapsible groups on the browser Settings screen now default
to **collapsed** (the basic-tier groups and the Mumble-servers panel joined the already-collapsed
advanced tier), so the page opens short and calm — click a group to expand it.

Cross-refs: ADR 0017 (the arbiter whose unmodeled turnaround this fills), ADR 0049 (the
`DtmfMuteGate` timed-latch primitive), ADR 0050 (the operator-talk yield this mirrors), ADR 0045 /
0041 (the RF→Mumble relay and bridge), ADR 0029 (the AIOC backend whose receiver produces the
transient), ADR 0037 (the settings tiers touched by the collapse).
