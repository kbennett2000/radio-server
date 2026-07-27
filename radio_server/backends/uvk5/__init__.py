"""UV-K5 (Quansheng Dock) backend package (ADR 0110).

A Quansheng UV-K5/K6 running a dock-mode firmware, wired via an AIOC cable (serial + audio
through the K1 jack — the same AIOC pattern the ``baofeng`` backend uses). Two firmwares
present the same wire protocol and this package drives both identically: nicsure's
"Quansheng Dock" on a classic DP32G030 radio, and ``kbennett2000/uv-k1-k5v3-firmware-custom``
on a UV-K5 **V3** (PY32F071), whose different MCU means nicsure's build cannot run on it at
all (ADR 0118). The fork's set-VFO command (``0x0873``, F6) is the one extension beyond the
classic surface; everything else here is common to both.

Shipped so far:

- :mod:`.frames` (ADR 0110) — the pure wire codec: the stock Quansheng UART framing
  (preamble / length / XOR obfuscation / CRC-16 / terminator) plus the ``0x08xx`` dock
  command and reply struct layouts.
- :mod:`.transport` (ADR 0111) — the serial I/O layer: an AIOC serial handle, a daemon
  reader thread feeding the decoder, a request/reply primitive, and a liveness
  :meth:`~.transport.Uvk5Transport.connect`. pyserial is lazily imported so this package
  stays hardware-free at import.
- :mod:`.radio` (ADR 0112, 0113) — :class:`~.radio.Uvk5Radio`, the ``CatRadio`` backend. In
  full-control ("XVFO") mode the host is the radio's brain: tune / tone / mode / key are all
  BK4819 register writes, keying is confirmed by a read-back (else ``Uvk5KeyingError``). Audio
  (the AIOC **sound-card** path) reuses the shared
  :mod:`~radio_server.backends.soundcard` seam (ADR 0113) — the same capture / playout / pacer
  machinery the ``baofeng`` backend runs; the audio stream opens around the register TX-enable.

The control path is decided (ADR 0111): **(b) BK4819 register-write tuning**, with channels
as server-side presets. Installed via the ``uvk5`` extra (serial + soundcard, ADR 0113).

Selectable and diagnosable since ADR 0114: the ``[uvk5]`` config block + factory registration
(``server.backend = "uvk5"``) and ``doctor --backend uvk5`` (connect probe with a stock-vs-dock
firmware tell, register ``--key-test``, and the shared RX diagnostics); see ``docs/uvk5-setup.md``.
Still deferred to later cycles: the server-side **presets** feature, the web UI, and the stuck-key
**watchdog/TOT** (ADR 0112 — the full-control loop has no time-out).
"""

from __future__ import annotations
