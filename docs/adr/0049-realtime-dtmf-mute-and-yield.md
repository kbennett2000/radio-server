# ADR 0049: Real-time DTMF tone muting and Mumble→RF keying yield

## Status

Accepted (supersedes the DTMF-mute mechanism of ADR 0045; ADR 0045's Mumble→RF busy-latch fix is
untouched).

## Context

Two operator-reported field failures on the Mumble link, both confirmed on the AE9S deployment:

1. **DTMF control tones are broadcast to every Mumble listener.** ADR 0045 muted them *retroactively*:
   the tone audio fans out to the `AudioHub` first, multimon-ng *decodes* a digit later, and only then
   does `DtmfMuteGate.note_digit()` condemn a **0.3 s delay line** on the bridge's Mumble feed. That
   only works if the decode arrives within the delay window. Under the deployment's `audio.squelch =
   "off"` (a continuous, pass-through stream), multimon's streaming decode latency exceeds 0.3 s, so a
   whole hand-dialed sequence leaves the delay line before any digit is condemned. Confirmed: full
   tones, never suppressed. Raising the delay cannot reliably win this race without loading large,
   permanent latency onto a live voice link — the scheme is decode-latency-bound by construction.

2. **The operator's DTMF commands are ignored while linked, intermittently ("spotty").** With
   `mumble.tx_to_rf = true` (default), inbound Mumble voice keys the radio through a `TxSession` that
   latches `arbiter.transmitting = True` for the whole talk-spurt **plus the `tx_hang` tail**. A keyed
   radio is deaf — `RxPump` skips `receive()`/`controller.step()` while transmitting — so no DTMF is
   decoded during that window. With `tx_hang = 2.0 s` and a net whose gaps are under 2 s, the radio
   stays keyed almost continuously and the ~1 s command lands on a deaf receiver. It decodes only in a
   Mumble pause longer than the hang — hence spotty. The `rx_active` deferral that would let RF signal
   hold off keying is inert under `squelch = "off"` (the pump never asserts `active` on a signal-blind
   pass-through gate, ADR 0045), so the bridge keys right over the operator.

The operator's choice for (2): keep two-way voice, mitigate in software.

## Decision

### A real-time DTMF tone detector, not a decode-latency delay line

Add `radio_server.link.tone_detect.DtmfToneDetector`: a single-bin DFT (Goertzel-equivalent)
evaluated at the eight standard DTMF frequencies on each ~20 ms RF frame, with presence tests biased
toward **sensitivity** (each of a low- and high-group tone above an energy floor, together dominating
the frame's energy). It answers only "is a DTMF dual-tone present in this frame?" — multimon-ng still
owns digit *decode*. Detection latency is one frame, independent of the decoder.

The detector runs in the bridge's RF→Mumble task, which sees audio exactly when the radio is
receiving. It fixes **both** problems through one shared `DtmfMuteGate` ("DTMF is happening now",
armed for a hold window, `mute_for`/`muted`):

- **RF→Mumble (problem 1):** on a detected tone frame, arm the gate and drop that same frame before it
  is sent. No delay line — the decision is made on the very frame in hand. The gate's hold keeps the
  feed muted across a hand-dialed sequence.
- **Mumble→RF (problem 2):** the Mumble→RF drain treats an armed gate like a second `rx_active` — it
  withholds inbound Mumble frames and, crucially, **ends any open keyed over immediately** so the
  deaf-while-keyed receiver reopens for the rest of the command. This is squelch-independent (it works
  under `squelch = "off"`, where `rx_active` is inert), because it keys off detected tone energy, not
  the pump's signal-aware gate.

Because the detector — not multimon's decode — drives muting and the yield, the gate + detector are
built whenever `mumble.dtmf_mute` is on, **independent of the controller** (so it works on a
deployment with no TOTP secret). `controller.on_digit = note_digit` remains wired when a controller
exists, as a secondary hold-extender.

### A shorter keyed hang so the receiver reopens in conversational gaps

The yield can only fire once the radio is receiving; during continuous far-end talk it is keyed and
deaf. To create those receive windows, `mumble.tx_hang` default drops from 2.0 s to **0.8 s** so the
radio returns to RX in normal inter-utterance gaps. It stays a per-deployment setting; the trade-off
(a shorter tail re-keys more often and can clip the far end's next word onto RF) is bench-tunable. The
gate hold (`mumble.dtmf_mute_hold`) rises from 1.0 s to **2.0 s** so one detected tone protects a full
command through the between-digit gaps.

## Consequences

- **Problem 1 is cured.** Tones are gated the instant they appear, regardless of multimon latency or
  squelch mode; RF→Mumble latency drops to ~zero (the delay line is gone).
- **Problem 2 is mitigated, not cured — this is half-duplex physics.** While the far end talks
  continuously the radio is keyed and cannot hear DTMF; the shorter hang + the DTMF-priority yield make
  commands land reliably in the ordinary gaps of a net, and an in-progress command is no longer keyed
  over. `mumble.tx_to_rf = false` (receive-only) remains the zero-contention escape hatch — it never
  keys, so DTMF always decodes.
- **Deliberate trade:** a rare tone-detector false positive briefly mutes one frame of link audio or
  withholds one Mumble→RF frame. That is preferred over leaking a control tone or dropping a command;
  thresholds are marked, tunable defaults with a VERIFY-AGAINST-HARDWARE note (guardrail 1).
- Station ID is unaffected (Part 97, guardrail 5): the yield only ends *bridged* Mumble overs; the RX
  path, DTMF decode, auth, and automatic identification are untouched.
- No new runtime dependency (numpy is already used). `radio.toml.example` is regenerated
  (`mumble.tx_hang`, `mumble.dtmf_mute*` descriptions/defaults). Detector and bridge behavior are
  unit-tested; on-air feel is a hardware bench check.
