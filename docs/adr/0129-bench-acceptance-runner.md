# 0129 — A permanent, self-serving bench acceptance runner

Status: Accepted

## Context

Every acceptance this project has run was a one-off: a script in `/tmp`, a sequence of `doctor`
invocations, a human keying a handheld and reporting what they heard. Three consequences, all of
which actually happened:

1. **`/tmp` is wiped on reboot.** The F4/F5 proofs (`dual_tx_watch.py` and friends) lived there.
   After a reboot the bench had no way to re-prove anything, which is a large part of why the same
   questions kept being re-litigated across cycles.
2. **`doctor` cannot test the deployed system.** It opens the serial port and sound card directly,
   so the service must be *stopped* first. Stopping the service to test it is precisely what
   produced the 8-minute outage in [ADR 0127](0127-bounded-graceful-shutdown.md).
3. **A human in the loop is a bottleneck and a variable.** "Kris hears himself" is not a number.

The bench has the hardware to remove the human entirely: two radio-server instances on one box,
both on 445.800, inches apart. Each is the other's instrument.

## Decision

`scripts/bench/acceptance.py` — one script, eight stages, driving **the live HTTPS APIs of both
deployed units**. Exit 0 only if every selected stage passes. It follows the existing
`scripts/bench/dstar_decode_selftest.py` precedent (`RADIO_API_TOKEN` from the environment,
self-signed TLS context, `RESULT: PASS/FAIL`).

| stage | proves | direction |
|---|---|---|
| `systemd` | units active + enabled, lingering on, a stop under WebSocket load is clean | — |
| `web` | TLS page on the LAN address, `/capabilities`, `/services`, `/presets`, 401 without a token | — |
| `presets` | apply a preset, radio state follows, restore | — |
| `rx` | frame duty, largest inter-frame gap, whole over received, tone recovered, xrun delta | kv4p → K6 |
| `dtmf` | DTMF decoded by the deployed decoder, recorded in the operating log | kv4p → K6 |
| `auth` | a real TOTP login over RF opens a session, and logs out again | kv4p → K6 |
| `tx` | kv4p hardware carrier-detect + recovered tone from the K6's transmission | K6 → kv4p |
| `services` | a spoken announcement heard as received audio, with speech-band energy | K6 → kv4p |

**A script, not a `doctor` flag** — for reason (2) above. Acceptance must exercise the system in
the state it actually runs in.

### Measurement decisions worth keeping

- **RX duty is measured over the *active span*** (first byte → last byte), not the listening
  window. Window-relative duty can only ever report the *transmitter's* duty cycle. Continuity
  while a carrier is up is the property under test. Paired with `largest inter-frame gap` and a
  "whole over received" check, so a receiver that delivers a smooth *fragment* still fails.
- **Tone recovery is a normalised spectral ratio**, not a raw magnitude, so the threshold survives
  a change of power level or geometry. Noise floor ≈ 0.005; a recovered tone measures ≈ 1.0.
- **DTMF digit shape came from a sweep, not a guess.** Sending `123456#` from the kv4p and decoding
  the K6's captured audio offline with the deployed decoder:

  | tone / gap | amplitude | decoded (twist 10 dB) | decoded (stock 4 dB) |
  |---|---|---|---|
  | 120 / 60 ms | 0.4 | `''` — too short to even hold the RX gate open | `''` |
  | **200 / 100 ms** | **0.4** | **`123456#`** | `14` |
  | 200 / 100 ms | 0.25 | `56#` (under-driven) | `#` |
  | 300 / 150 ms | 0.4 | `123456#` | `14` |

  That right-hand column is the load-bearing part: **this hardware genuinely needs
  `audio.dtmf_reverse_twist_db = 10.0`** (ADR 0075). At the stock 4 dB limit a perfectly good
  capture decodes as `14`. See "the stash" below.
- **The `auth` stage forces logged-out before and after**, so three consecutive runs are
  independent. Session state that leaks between runs would turn the second run's TOTP entry into a
  plain command and quietly stop testing the login path.

### The stashed `dtmf.py` edit — dropped, with the reason

A stash sat on the deployed tree: `NATIVE_REVERSE_TWIST_DB 4.0 → 10.0`, hardcoded.
**Dropped.** It is fully superseded by the config knob it predates: `audio.dtmf_reverse_twist_db`
threads `spec.py:551 → engine.py:784 → GoertzelStream(reverse_twist_db=…)`, and the deployed
`radio.toml` already sets `10.0`. Applying the stash would also have broken
`tests/test_native_dtmf.py:335`, which pins the *default* at 4.0 on purpose. The sweep above is the
evidence that the setting is needed and that the deployed config already delivers it.

### Watcher tooling

`dual_tx_watch.py`, `kv4p_carrier_watch.py` and `kv4p_audio_probe.py` moved `scratchpad/` →
`scripts/bench/`, with the hardcoded token and host replaced by `RADIO_API_TOKEN` / `RADIO_HOST`.
They stay because they are live *watchers* — useful while poking at something interactively in a
way a pass/fail runner is not.

## Consequences

- One command re-proves the whole station: `RADIO_API_TOKEN=… .venv/bin/python
  scripts/bench/acceptance.py`. Run it after every deploy that touches audio, keying, or the
  controller.
- It keys the transmitter, by design. Bench frequencies, dummy load, existing TOT and automatic
  station ID untouched.
- A full run takes a few minutes, most of it spoken announcements playing out in real time.
- `scripts/bench/uvk5_tx_regs.py` stays separate: it needs the service stopped, so it cannot be a
  stage. It is the escalation path when `tx` fails (ADR 0128).
