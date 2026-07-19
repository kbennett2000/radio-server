# 0086 — The vocoder seam: PCM ⇄ AMBE over the DV Dongle (AMBE2000), isolated

Status: Accepted

## Context

A future digital-voice path (D-STAR, and the digital half of a bridge) needs a **vocoder**: turn
8 kHz speech PCM into a compressed voice frame and back. Everything else that path needs — framing,
sockets, headers, bridge wiring — is ordinary plumbing over shape the project already has, **except**
the vocoder itself, which depends on a physical DSP chip and a serial protocol that cannot be proven
by inspection. So the vocoder lands **alone, before any of that plumbing exists and unwired from the
live app** — the same posture the (reverted) Codec2 seam took to open the old M17 arc: if the seam is
awkward, it should not drag framing and socket work down with it.

The first implementation is the **DV Dongle**: a DVSI **AMBE2000** full-duplex vocoder behind an FTDI
VCP. The genuine unknown is its **start-up handshake plus the AMBE2000 D-STAR full-rate config
sequence** — judge-on-the-chip facts (guardrail 1). The protocol was read from g4klx/DummyRepeater's
`Common/DVDongleController.cpp` (GPL-2) **as a specification** and reimplemented clean in Python; the
byte constants are DVSI/DV-Dongle hardware-interface facts, not ported code (talking to a device over
a wire is not a derivative work — the same stance as the kv4p frame codec vs the GPL-3 firmware).

AMBE is DVSI-patented, but the vocoding happens **on the licensed chip**: no codec, and no AMBE
source, lives in radio-server — only the serial driver that feeds the hardware. So this carries no
patent exposure and no copyleft (unlike the Codec2 seam, whose whole shape was a licensing
constraint).

## Decision

Add a `radio_server/vocoder/` package: a device-independent `Vocoder` **seam** and one concrete
`DVDongleVocoder`. **Not wired into the live app** — only the `doctor --vocoder-loopback` self-test
constructs a vocoder this cycle.

- **The seam is a `typing.Protocol`, `encode(pcm) -> ambe` / `decode(ambe) -> pcm`, one 20 ms frame at
  a time** (`vocoder/base.py`). `encode` takes an 8 kHz / 160-sample `AudioFrame` and returns 9 bytes
  of AMBE; `decode` the reverse. A `close()` completes the surface. A later **AMBE3000** (ThumbDV)
  device or a **software codec (Griffin)** implements this same protocol and drops in behind it — the
  reason the seam exists apart from the DV Dongle.

- **The seam operates at the vocoder's native 8 kHz, not the app's 48 kHz canonical audio.** Every
  real vocoder (AMBE2000, AMBE3000, Codec2/Griffin) is natively 8 kHz, so 8 kHz is the common
  denominator that keeps drop-ins rate-identical. The 48k⇄8k resample belongs at the **edge of the
  future backend** that wires a vocoder into the live path (reusing `audio/resample.py`, the same
  "resample only at the tolerant edge" rule as the DTMF and piper edges) — never inside the vocoder.
  This is a **deliberate departure** from the reverted Codec2 seam, which took the 48 kHz frame and
  resampled internally; pushing the resample out keeps the seam a pure codec. The frame is still a
  fail-loud `AudioFrame` (ADR 0006): a wrong-rate or wrong-length buffer raises `AudioFormatMismatch`
  at the boundary, before any device I/O.

- **A pure, I/O-free wire codec** (`vocoder/frames.py`), separate from the transport — the same split
  as `backends/kv4p/frames.py`. It builds the control/config/PCM/AMBE packets and streams-deframes the
  device→host bytes. DV Dongle framing is a 2-byte little-endian header carrying a 13-bit total length
  and a 3-bit type (`length = word & 0x1FFF`, `type = word >> 13`), confirmed arithmetically against
  every reference constant. The 48-byte AMBE payload is a 24-byte AMBE2000 config block (the D-STAR
  full-rate parameters, verbatim from the reference) followed by the 9-byte voice frame at offset 24.

- **`DVDongleVocoder` mirrors the kv4p transport** (`vocoder/dvdongle.py`): `pyserial` imported lazily
  behind the `hardware` extra with a `_serial_factory` test seam; a daemon reader thread
  (read→deframe→dispatch) bounded by a short read timeout and a stop `Event`; a fatal-read path that
  wakes every blocked caller; a `Condition`-guarded reply hand-off; and an idempotent, best-effort
  `close()`. Bring-up is `open()` (query name) → session `start()`, per the reference; the AMBE2000
  D-STAR config rides every AMBE packet. v1 is a **synchronous query/reply per frame** — the chip is
  full-duplex, but a blocking `encode`/`decode` is enough and keeps the seam simple.

- **A `doctor --vocoder-loopback` self-test** (`doctor.py`, `--vocoder-port`): synthesize a **staircase
  of steady 8 kHz tones** → **encode the whole stream, then decode the whole stream** (the chip is
  pipelined full-duplex; see Consequences) → resample to canonical → write a WAV (`--out`), and report
  a **pitch-tracking metric** — per-step dominant frequency in vs out, lag-aligned for the constant
  round-trip latency, Pearson-correlated across steps (AMBE is lossy, so **never** sample equality; a
  fixed buzz / noise / scrambled stream does not track). Steady steps (not a sweep) keep each step's
  pitch measurable through the codec's latency and transient smear. It is the loopback equivalent of
  DVTool's "Audio Loopback Only" and the acceptance test for the risky bring-up. Handled before the
  backend split (it drives a separate FTDI device, not the radio), so it never builds a radio config.
  Backend-independent, like `--analyze-wav`.

- **No config schema this cycle.** The driver is unwired, so nothing consumes a `[vocoder]` group. The
  port stays a marked module default (`DEFAULT_DVDONGLE_PORT`, verify-against-hardware) plus the
  doctor `--vocoder-port` flag; `config/spec.py`, the config canary, and `radio.toml.example` are
  untouched. Config integration is a later wiring cycle's concern. No new extra: the DV Dongle is a
  serial device, so it rides the existing `serial`/`hardware` extra (pyserial).

- **Every hardware fact is marked verify-against-hardware (guardrail 1).** Port, baud (230400), and the
  handshake/AMBE-config byte sequences are documented as spec-derived and confirmed by the loopback on
  the real dongle, not by the fakes. A constant that differs on the bench gets corrected there, and its
  note updated.

### Why not broaden it

No reflector/gateway protocol, no D-STAR headers or slow-data, no bridge/`Link` wiring, no DTMF, no
`Radio` backend, no factory registration. Just PCM ⇄ AMBE, isolated. Full-duplex streaming and the
48k-edge backend are later cycles once a live consumer exists.

## Consequences

- The one genuine unknown of the digital-voice path is closed and isolated: an 8 kHz PCM frame encodes
  to a 9-byte AMBE frame and decodes back, with the framing and config proven against the reference in
  fakes and — per the acceptance — on the actual dongle by the loopback. The framing/socket/backend
  work that follows is ordinary plumbing over a known-good codec.
- The seam admits the AMBE3000 and Griffin as drop-ins without touching callers: same `Vocoder`
  protocol, same 8 kHz frame. A software codec would additionally drop the serial machinery for a
  ctypes wrapper (the reverted Codec2 module is the template) behind the same surface.
- No new core dependency and no schema churn: the module uses only the stdlib plus the existing
  pyserial extra; the default test suite stays hardware-free (fakes + a fake clock), with the
  missing-pyserial path proven unconditionally.
- **Hardware-verified (bench, this device):** the handshake and the AMBE2000 D-STAR config bytes are
  correct **as reimplemented from the reference — no byte needed changing**. On the deployment dongle
  (`/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_*`, 230400 8N1) a streaming loopback of a nine-tone
  staircase spanning 300–1500 Hz round-trips with **pitch correlation 1.00, median error ~8 Hz**,
  reproducibly. The one weak spot is a pure ~300 Hz tone (near the low edge of AMBE's speech model),
  which still recovers within tolerance. So the seam sits on a proven-good codec.
- **The AMBE2000 is a pipelined, full-duplex chip — a loopback must encode the whole stream, then
  decode the whole stream, never interleave `encode`/`decode` per frame.** Bench-discovered: the
  original loopback alternated `decode(encode(frame))` per frame, which feeds the chip's two pipelines
  a dummy frame on the opposite stream each tick and reads back the wrong result, scrambling anything
  time-varying (pitch correlation collapsed to ~0 with gross frequency errors — e.g. 450 Hz → 3217 Hz).
  A single steady tone masked it entirely (a steady tone is invariant to the reordering), which is why
  the first bring-up "passed" on a 600 Hz tone. The round-trip also carries a **constant but
  session-varying latency** (~0–18 frames, the pipeline depth at stream start), so the loopback's
  metric aligns by searching that frame lag before correlating — a benign alignment that no lag can use
  to rescue a genuinely broken codec. The per-frame `Vocoder.encode`/`decode` seam is unchanged and
  correct: a real single-direction path (TX encodes a stream, RX decodes a stream) never interleaves;
  interleaving is a self-test / duplex-caller hazard, now documented on `DVDongleVocoder`.
- **Still ahead:** whether decoded AMBE is *intelligible speech* (not just pitch-faithful) through a
  live path, and full-duplex streaming, await a real consumer. Wiring a vocoder into a live
  digital-voice or bridge path (the 48k edge, framing, sockets) is the next step, and it is where the
  `[vocoder]` config group arrives.

Cross-refs: ADR 0006 (canonical audio + fail-loud `AudioFrame`), ADR 0029 (AIOC bring-up / doctor +
the missing-extra error shape), ADR 0061 (the kv4p transport + pure frame-codec split this mirrors),
the reverted Codec2 seam (commit 176ce99, the dead M17 arc — the interface-shape and geometry-discipline
template).
