"""kv4p HT backend package (ADR 0061).

This cycle ships only the pure wire codec (:mod:`.frames`) — KISS framing, the
kv4p vendor-frame envelope, and the on-wire struct layouts. Serial I/O, the audio
codec, and the ``Kv4pHt`` backend class are deferred to later cycles (see the ADR).
"""

from __future__ import annotations
