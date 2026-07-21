"""UV-K5 (Quansheng Dock) backend package (ADR 0110).

A Quansheng UV-K6 running nicsure's "Quansheng Dock" custom firmware, wired via an
AIOC cable (serial + audio through the K1 jack — the same AIOC pattern the
``baofeng`` backend uses).

Shipped so far:

- :mod:`.frames` (ADR 0110) — the pure wire codec: the stock Quansheng UART framing
  (preamble / length / XOR obfuscation / CRC-16 / terminator) plus the ``0x08xx`` dock
  command and reply struct layouts.
- :mod:`.transport` (ADR 0111) — the serial I/O layer: an AIOC serial handle, a daemon
  reader thread feeding the decoder, a request/reply primitive, and a liveness
  :meth:`~.transport.Uvk5Transport.connect`. pyserial is lazily imported so this package
  stays hardware-free at import.

The control path is decided (ADR 0111): **(b) BK4819 register-write tuning**, with channels
as server-side presets. Still deferred to later cycles: the ``Radio``/``CatRadio`` class,
the ``[uvk5]`` config block + factory registration, ``doctor``, the presets feature, and the
web UI.
"""

from __future__ import annotations
