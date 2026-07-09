# 0001 — Two-backend radio abstraction with mock-first build

Status: Accepted

## Context

The server must control two different radios with different capabilities: a Kenwood
TM-V71A (full CAT control plus audio/PTT via a SignaLink) and a Baofeng UV-5R
(audio/PTT only, via an NA6D AIOC cable, no serial control). All higher-level
features (DTMF decode, TOTP auth, sessions, dispatch, TTS) operate on sound-card
audio and are independent of which radio is attached. Hardware is in transit, so
the software must be buildable and testable before any radio is connected.

## Decision

- Define a single Radio protocol. Shared methods: transmit, receive, ptt, status.
  CAT-only methods (set_frequency, set_channel, set_tone, set_mode, scan) are
  implemented only by the TM-V71A backend.
- Provide three implementations:
  - SignaLinkV71: audio-triggered PTT plus CAT via Hamlib.
  - AiocBaofeng: explicit RTS-line PTT via pyserial; CAT methods unsupported.
  - MockRadio: records TX audio, serves canned RX, fakes status/busy.
- All application logic depends only on the shared surface and is tested against
  MockRadio. The real backends are brought up last, against hardware.
- Capability differences are surfaced explicitly via capabilities(); the API
  rejects CAT operations in Baofeng mode rather than silently ignoring them.

## Consequences

- The entire stack (auth, sessions, services, scan logic, API) is unit-testable
  offline with no radio present.
- Adding a third radio later is one new backend class, with no changes to services.
- The PTT keying divergence (audio-trigger vs RTS) is isolated to the two real
  backends; nothing above the radio layer knows which is loaded.
- CAT-only features degrade cleanly (explicit "unsupported") rather than failing
  silently in Baofeng mode.
- Trade-off: a mock backend can mask real-world audio, RFI, and timing issues, so a
  dedicated hardware bring-up phase is required to validate what the mock cannot
  (audio routing, DTMF robustness under weak signal, squelch-poll timing, RFI).
