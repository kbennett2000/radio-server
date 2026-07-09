"""Text-to-speech interface + a deterministic stub.

Services produce audio by rendering text through a `TtsEngine`. This cycle ships only
`StubTts`, whose output is a deterministic function of its input text — so a test can
assert exactly what a service "spoke" by inspecting `MockRadio.tx_log`. The real
CPU-friendly engine (piper) is a later cycle; it will implement the same `render`
contract, so nothing above the TTS layer changes when it lands.

Audio stays opaque `bytes` (the `AudioFrame` alias from the backend layer) — the
sample rate / width / channel format is pinned by its own ADR before real audio I/O.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..backends import AudioFrame


@runtime_checkable
class TtsEngine(Protocol):
    """Renders a line of text to a chunk of audio."""

    def render(self, text: str) -> AudioFrame: ...


class StubTts:
    """Deterministic, hardware-free TTS for tests and mock runs.

    `render` is a pure function of `text`: equal text always yields equal bytes, and
    the bytes embed the text so a test can assert precisely what was spoken. This is
    NOT real speech — it is a stand-in for piper that keeps the whole dispatch path
    deterministic.
    """

    def render(self, text: str) -> AudioFrame:
        return b"<audio:" + text.encode("utf-8") + b">"
