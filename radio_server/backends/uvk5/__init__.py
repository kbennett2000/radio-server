"""UV-K5 (Quansheng Dock) backend package (ADR 0110).

A Quansheng UV-K6 running nicsure's "Quansheng Dock" custom firmware, wired via an
AIOC cable (serial + audio through the K1 jack — the same AIOC pattern the
``baofeng`` backend uses).

This cycle ships only the pure wire codec (:mod:`.frames`) — the stock Quansheng
UART framing (preamble / length / XOR obfuscation / CRC-16 / terminator) plus the
``0x08xx`` dock command and reply struct layouts. Serial transport, the
``Radio``/``CatRadio`` class, the ``[uvk5]`` config block, ``doctor``, and the web
UI are all deferred to later cycles (see the ADR). The open control-path decision
(keypress-simulation vs. BK4819 register-write tuning) is likewise deferred; the
codec covers both command families so neither is foreclosed.
"""

from __future__ import annotations
