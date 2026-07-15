# 0049 — The Codec2 seam: ctypes over libcodec2, mode 3200, MIT-preserving

Status: Accepted

## Context

This opens the M17 backend arc that HANDOFF has pointed at since cycle 51: the real
M17/mrefd network backend behind `create_link`, whose payload is Codec2 3200 audio. Everything
that arc needs — LSF/EOT framing, the reflector socket, the `Link` wiring — is ordinary
plumbing over shape the project already has, **except one genuine unknown**: turning the
canonical audio into Codec2 frames and back. Codec2 is a real DSP library with a C ABI and a
runtime-queried frame geometry, and it is the single piece that cannot be built or tested by
inspection. So it lands **alone**, before any socket exists — if the seam is ugly, it should
not drag the framing and socket work down with it. No sockets, no mrefd, no UDP, no M17
framing, no `Link` backend this cycle: pure codec.

The seam's shape is dictated by a **licensing constraint**, and this is the load-bearing
reason it looks the way it does. radio-server is MIT. Codec2 is LGPL-2.1 — deliberately, so
that non-GPL projects can use it, but only under LGPL's terms. The LGPL boundary turns on
linking: **dynamic** linking against an unmodified shared library keeps the calling program's
own license intact, while statically embedding the source, or depending on a GPL-licensed
Python binding, would pull this project under copyleft. So the only license-clean way for an
MIT project to use Codec2 is to load the system `libcodec2.so` dynamically at runtime and call
its C ABI directly. That is exactly what `ctypes` + `ctypes.util.find_library` do, and it is
why the module is a thin ctypes wrapper and nothing else.

Guardrail 1 (verify hardware/build facts empirically) governs the frame geometry: Codec2's
samples-per-frame and bits-per-frame are properties of the installed library and its mode, not
constants to trust from memory. The module queries them at runtime and asserts they match its
assumptions, failing loud if the installed build disagrees.

## Decision

- **Dynamic ctypes linking, and only that — no vendoring, no GPL binding.** The module is
  `radio_server/audio/codec2.py`: `ctypes.util.find_library("codec2")` → `ctypes.CDLL(path)` →
  the C functions bound with explicit `argtypes`/`restype`. The Codec2 source is **not**
  vendored, and **no** GPL-licensed Python binding (`pycodec2` and friends) is added — either
  would compromise the MIT license the dynamic-linking approach preserves. This licensing
  reasoning is the spine of the seam, recorded here so a later cycle does not "simplify" it by
  pip-installing a binding.

- **Mode 3200 only (M17 full rate), encoder + decoder.** `CODEC2_MODE_3200 = 0` (the C enum
  value). `Codec2.encode(frame) -> bytes` and `Codec2.decode(packets) -> AudioFrame` are the
  whole surface. Other Codec2 rates are a deliberate later-cycle concern; M17 full rate is 3200
  and nothing else needs the other modes yet.

- **Canonical ↔ Codec2 round-trip reuses the existing resample edge.** Codec2 3200 operates on
  8 kHz s16le mono; the project's canonical audio is 48 kHz s16le mono (ADR 0006). `encode`
  runs `resample(frame, 8000)` (the ADR 0006 `soxr` VHQ resampler) down to Codec2's rate, then
  frames and encodes; `decode` reassembles 8 kHz PCM and runs `to_canonical` back up to 48 kHz.
  This is the same "resample only at the tolerant edge" pattern as the DTMF (`to_multimon`) and
  piper (`to_canonical`) edges — a **second** resampler is explicitly not written.

- **Frame geometry is queried at runtime and asserted, never hardcoded (guardrail 1).** After
  `codec2_create`, the module reads `codec2_samples_per_frame` and `codec2_bits_per_frame` and
  **asserts** they equal its assumptions (160 samples / 64 bits = 8 bytes for mode 3200),
  raising loudly with queried-vs-expected if the installed build disagrees. `bytes_per_frame`
  is derived `(bits + 7) // 8`, not assumed. The 160/64 numbers appear only as the values the
  assertion checks against — they are never trusted as the operating geometry.

- **A missing library is a config error at construction, not a crash — fail loud by name.**
  `Codec2.__init__` calls `find_library("codec2")`; a `None` result (or an `OSError` from
  `CDLL`) raises a `RuntimeError` naming `libcodec2` and pointing at both the `codec2` extra and
  the system package — the same shape as `PiperTts`'s missing-voice `RuntimeError` (ADR 0009)
  and the AIOC backend's `_EXTRA_MSG` (ADR 0029). This module is never imported at rest: only a
  configured M17 backend constructs it, so nobody without Codec2 configured ever loads it, and
  anyone who does gets an actionable message instead of a stack trace.

- **A `codec2` optional-dependencies extra, empty but documented.** `[project.optional-
  dependencies] codec2 = []` mirrors `tts` and `hardware` as the discoverable install marker
  (`pip install 'radio-server[codec2]'`) and the skip-gate parity point. It is **empty** because
  there is no license-clean Python package to pull: the only real requirement is the out-of-band
  system library `libcodec2` (`apt install codec2` / `libcodec2-dev`), exactly like
  `libportaudio2` for sounddevice or the `multimon-ng` binary. The extra's comment records this
  so its emptiness reads as deliberate, not forgotten.

- **The default suite stays hardware- and dependency-free.** The Codec2 tests are `skipif`-gated
  on `find_library("codec2") is None`, the same posture as the AIOC hardware tests and the piper
  real-engine tests. The one test that runs unconditionally is the missing-library fail-loud
  path (driven by monkeypatching the loader), so CI proves the config-error behavior without the
  library present, and proves the real geometry/round-trip only where the library is installed.

- **Codec2 is lossy: correctness is geometry, length, frame count, and non-silence — not sample
  equality.** A 3200 bps vocoder does not reproduce its input samples; asserting sample equality
  would be wrong. The tests assert the queried geometry matches assumptions, that a 48 k frame
  round-trips to a 48 k canonical frame of the expected length, that a known-length buffer
  produces the expected frame count, and that the output is not silence. Perceptual quality — is
  the decoded audio *recognizable* — is a bench fact, verified by ear on real audio, not a unit
  test.

## Consequences

- The M17 arc's only genuine unknown is closed and isolated: a canonical 48 k `AudioFrame`
  encodes to Codec2 3200 packets and decodes back to a canonical frame, with the frame geometry
  proven against the installed library rather than assumed. The socket/framing/`Link` work that
  follows is now ordinary plumbing over a known-good codec.
- No new core runtime dependency: the module uses only stdlib `ctypes` plus the existing `numpy`
  and the ADR 0006 `resample`/`to_canonical` edge. `libcodec2` arrives out-of-band via the
  system package; the `codec2` extra is an empty marker.
- The MIT license is preserved by construction: dynamic linking only, no vendored LGPL source,
  no GPL binding. A future cycle that needs another Codec2 mode extends `encode`/`decode` and the
  geometry assertion; it must not reach for a Python binding.
- **Scope limits, deliberate:** mode 3200 only; no streaming/stateful chunk boundaries beyond
  whole-frame padding (a trailing partial frame is silence-padded to a whole Codec2 frame); no
  M17 framing, LSF/EOT, or reflector socket — all of that is the next cycle(s) of the arc.
- **Still ahead, and empirical:** whether decoded 3200 audio is intelligible through a real RF
  path, and the exact installed `libcodec2` soname/geometry on the deployment box, are
  bench/installed-build checks this software cycle cannot prove. The M17/mrefd network backend
  (LSF/EOT edges, the reflector socket, Codec2↔canonical wired into `Link`) is the next step.
