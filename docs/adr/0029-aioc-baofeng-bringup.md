# 0029 — AIOC/Baofeng backend bring-up (audio-only PTT via serial line + USB sound card, no CAT)

Status: Accepted

## Context

The whole stack was built software-first behind `MockRadio` (ADR 0001, guardrail 6) so the two real
hardware backends could be brought up last, with hardware in hand. The NA6D **AIOC cable is now
plugged in and empirically confirmed present**, so this cycle implements the real `AiocBaofeng`
backend — until now a `NotImplementedError` stub. Every layer above the radio (auth, sessions,
DTMF, TTS, CW/voice ID, RX pump, TX session, arbiter, recording, API, web UI) is unchanged: it
speaks only `transmit()`/`receive()`/`ptt()`/`status()`/`capabilities()`, so wiring those five to
real hardware is the entire job.

Confirmed empirically this cycle (guardrail 1 — none of this was asserted from memory):

- USB `1209:7388` "All-In-One-Cable", `cdc_acm` driver, serial `da3441ac`.
- **PTT serial:** `/dev/ttyACM0`; stable path `/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04`.
  Group `dialout`; the operator is in `dialout` (no sudo). Verified live: the port opens with both
  control lines held **low** (no keying).
- **Audio:** ALSA card `AllInOneCable` (`hw:CARD=AllInOneCable`, = `hw:2`), capture (`pcm0c`) +
  playback (`pcm0p`). The USB codec is **48 kHz-native** → equals `CANONICAL_FORMAT`
  (48k/s16le/mono, ADR 0006) → **no resampling** in this backend.
- The UV-5R has **no busy/COS line** (ADR 0015): RX gating is software VAD only.

Confirmed on the bench during this cycle: **PTT keys on DTR** (`--key-test`; RTS did not key this
AIOC), and live 48 kHz **RX capture reads real audio** off the card (`All-In-One-Cable: USB` → the
raw ALSA device). Full talk-through (server + browser, actual on-air TX) remains the operator's
final acceptance step.

## Decision

- **`AiocBaofeng` is a pure DI object, Settings-free** (like `MockRadio`). It takes `serial_port`,
  `ptt_line`, `input_device`, `output_device`, `blocksize`, plus test seams `_serial_factory` /
  `_audio`. The composition root (`build_app`) reads the `[baofeng]` config and passes the kwargs;
  the class never imports `config`. This keeps it exactly unit-testable against fakes with no
  hardware and no dependency inversion.
- **Two keying shapes, one `transmit()`.** `transmit()` self-keys **only when the line is not
  already held**: a one-shot clip (station ID, service TTS, REST `/transmit` — each a single
  `transmit(whole_clip)` call) asserts the line, plays, drains, drops. A streaming session
  (`TxSession`: `ptt(True)` … many `transmit(frame)` … `ptt(False)`) holds the line across frames,
  and `transmit()` only plays. The distinguishing state is `_keyed` (set by `ptt(True)`). This
  matches guardrail 2 (keying is the serial line, never a CAT `TX`) and the SignaLink "audio keys
  it" mental model, without dropping PTT between streamed frames.
- **PTT is a configurable serial control line, default DTR (confirmed on the bench).**
  `baofeng.ptt_line` is an `rts`/`dtr` enum; the backend does `setattr(serial, ptt_line, on)`. The
  true line is empirical (guardrail 1): the doctor `--key-test` confirmed **DTR** keys this NA6D AIOC
  (RTS did not), so `dtr` is the default. It stays configurable for other AIOC/radio combinations.
- **RF-safety guards.** (a) The serial port is opened with **both lines pre-set low** so a
  driver that pulses RTS/DTR on open (the Arduino-reset footgun) cannot momentarily key the
  transmitter; construction is proven to leave both lines low. (b) `close()` (also `atexit`-
  registered) always drops the line — the process can never exit with the transmitter keyed. (c)
  On key-down the playback stream is `stop()`-drained **before** the line drops, so the audio tail
  is never clipped.
- **Streams held open, 48 kHz-native.** `RawInputStream`/`RawOutputStream` (bytes in/out — no numpy
  round-trip, since `AudioFrame.samples` is already `<i2`). Capture opens lazily on first
  `receive()` and is reused; playback is open only while the line is asserted. No resampling.
- **`capabilities()` returns `SHARED_CAPS` only.** No CAT methods exist on the class, so the API
  returns **501** for `set_frequency`/`scan`/etc. (guardrail 3). `status().busy` is always `False`
  (no COS line); CAT fields stay `None`.
- **`audio.squelch=cat` is rejected for baofeng.** With no busy line the CAT gate would poll a radio
  that never reports busy, so `build_app` fails loud and points at `audio.squelch=audio` (software
  VAD) — the recommended baofeng squelch.
- **Hardware deps are the `hardware` optional extra** (`pyserial`, `sounddevice`), lazily imported
  inside the backend (mirroring the `tts`/piper pattern) so `import radio_server.backends` and CI
  stay hardware-free. `sounddevice` additionally needs the system `libportaudio2`.
- **A `doctor` diagnostic** (`python -m radio_server.doctor`) enumerates the AIOC sound card
  (48 kHz capture/playback), checks the serial port opens and `dialout` access, and reports a
  pass/fail table — all read-only, safe anywhere. Its `--key-test` is the **only** RF path: opt-in,
  refuses to run non-interactively/CI, demands a typed `CONFIRM`, asserts the configured line for a
  hard-capped ~2 s, then asks which line keyed. RF never runs in CI or pytest.

## Consequences

- **The Baofeng backend is live.** Selecting `server.backend=baofeng` builds a real radio: serial
  PTT + USB-audio TX/RX, no CAT. Verified live this cycle: the backend constructs against the real
  `/dev/ttyACM0` (lines held low, no keying), reports `SHARED_CAPS` + `busy=False`, and closes
  cleanly; the doctor's serial checks all PASS against the plugged-in AIOC (found the by-id path,
  opens without keying).
- **`uv run pytest` → 452 passed, 5 skipped.** New `tests/test_aioc_baofeng.py` drives the full
  keying/audio state machine against fake serial/audio seams (format-reject-before-audio, one-shot
  self-key + drain-then-drop, streaming holds one stream across frames, ptt idempotency, no keying
  on construction — parametrized RTS/DTR, lazy-import error surface, close/atexit line-drop). The
  factory test now builds baofeng (only `v71` still raises); the settings-API count canary moved
  31→36 and asserts the `ptt_line` enum renders. The 5th skip is the hardware-gated real-capture
  test (device present but this sandbox lacks `libportaudio2`).
- **Bench-confirmed — full talk-through works (the ADR-0001 "plug it in, it keys up clean" bar):**
  `libportaudio2` installed; doctor audio+serial PASS; the AIOC audio device resolves as
  `All-In-One-Cable: USB` and reads real 48 kHz audio; `--key-test` confirmed **DTR** keys the
  transmitter (default flipped RTS→DTR); levels tuned via `alsamixer` + the UV-5R volume (received
  signal ~5675 RMS avg, `audio.vad_on_rms=1000`/`vad_off_rms=500`); browser **Listen** gates on real
  audio, `--tx-tone` was heard on a second radio, and **Talk** (computer mic → radio) works
  end-to-end. AIOC/Baofeng is production-ready. (SignaLinkV71 remains a stub — hardware not here.)
- **Known limitations / deferred:** `receive()` blocks ~one block (~20 ms) directly on the event
  loop (`RxPump` calls it inline) — moving it to a thread executor is a separate follow-up, not
  needed at 20 ms. A backend lifecycle `close()` hook in the composition root is deferred (atexit
  covers the safety-critical line-drop). `SignaLinkV71` remains a stub (its hardware hasn't arrived);
  its Hamlib model / `rigctl` speed / `multimon-ng` flags stay verify-on-hardware.

## Numbering / branch note

Cut from a freshly-pulled `origin/master` at `1ecf9d7` (cycle 28, ADR 0028 merged via #30). Branch
`cycle-29-aioc-baofeng`, PR against `master`. ADR numbering continues at 0029 (the known duplicate
`0001` from cycle 24 is untouched).
