"""End-to-end: a real DTMF-TOTP login decoded from realistic 20 ms capture blocks (ADR 0031).

This is the test cycle 31 should have had. Instead of a `FakeDtmfDecoder` returning pre-formed
entries, the controller is driven with **real `synth_dtmf` audio sliced into 20 ms blocks** (as the
AIOC delivers), through the real `BufferedDtmfInput` and **real multimon-ng** — exactly what the
single RX reader (ADR 0031) now feeds it live. It fails against the old design (a 0.5 s poll sampling
~4 % of the audio into non-contiguous fragments) and passes only when the reader hands the decoder one
contiguous capture.

The second test proves the consolidation itself: **one** `RxPump` feeds both the DTMF controller and a
browser hub subscriber from a single `receive()` — the thing two independent readers could not do.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFrame, MultimonDtmfDecoder, synth_dtmf
from radio_server.backends import MockRadio
from radio_server.controller import build_controller
from radio_server.rx import AudioHub, RxPump
from radio_server.services import StubTts

from .conftest import TEST_SECRET, make_settings

CALLSIGN = "AE9S"
_BLOCK_BYTES = 960 * CANONICAL_FORMAT.frame_bytes  # 960 samples = 20 ms at 48 kHz

requires_multimon = pytest.mark.skipif(
    shutil.which("multimon-ng") is None, reason="multimon-ng not installed; real-decode e2e"
)


def _blocks(text: str) -> list[AudioFrame]:
    """Render ``text`` as the AIOC would deliver it keyed over the air: for each key, ~0.5 s of the
    DTMF tone followed by ~0.5 s of silence, all sliced into 20 ms blocks. The 0.5 s tone fills exactly
    one decode window; the 0.5 s silence is a silent window that resets held-tone de-dup, so repeated
    digits in a code each register (the "pause between repeats" rule)."""
    silence = AudioFrame(b"\x00\x00" * 960, CANONICAL_FORMAT)  # one 20 ms block of silence
    blocks: list[AudioFrame] = []
    for ch in text:
        tone = synth_dtmf(ch, 500).samples  # 0.5 s == one 48000-byte decode window
        blocks += [
            AudioFrame(tone[i : i + _BLOCK_BYTES], CANONICAL_FORMAT)
            for i in range(0, len(tone), _BLOCK_BYTES)
        ]
        blocks += [silence] * 25  # ~0.5 s of silence between keys
    return blocks


@requires_multimon
def test_login_decodes_from_realistic_20ms_blocks(clock, code_for):
    # The exact bench failure: a valid TOTP code arriving as 20 ms blocks (not one pre-formed entry)
    # must decode through real multimon and open a session.
    code = code_for(clock.now)
    settings = make_settings({"station.callsign": CALLSIGN})  # dtmf.buffer_seconds default 0.5
    ctrl = build_controller(
        settings,
        radio=MockRadio(),
        totp_secret=TEST_SECRET,
        decoder=MultimonDtmfDecoder(),  # the REAL decoder — no fake
        tts=StubTts(),
        clock=clock,
    )
    events: list = []
    ctrl.on_event = events.append

    for block in _blocks(code + "#"):
        ctrl.step(clock.now, block)  # what the single RX reader does per frame

    assert ctrl.session.authenticated, "a TOTP code delivered in 20 ms blocks should log in"
    assert "auth_accepted" in [e.phase for e in events]


def test_one_reader_feeds_controller_and_hub_together():
    # The consolidation (ADR 0031): a single RxPump fans one receive() to BOTH the DTMF controller and
    # a browser hub subscriber — what the old two-independent-readers design could not do without them
    # stealing each other's blocks. No multimon needed: a recording stand-in captures the frames.
    class RecordingController:
        def __init__(self) -> None:
            self.frames: list[AudioFrame] = []

        def step(self, now: float, frame: AudioFrame) -> None:
            self.frames.append(frame)

    tones = [synth_dtmf("1", 40) for _ in range(5)]
    radio = MockRadio(rx_frames=list(tones))
    hub = AudioHub()
    ctrl = RecordingController()
    pump = RxPump(radio, hub, controller=ctrl, poll=0.0)

    async def _run() -> list[bytes]:
        queue = hub.subscribe()
        pump.start()
        try:
            return [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in tones]
        finally:
            await pump.stop()

    hub_frames = asyncio.run(_run())

    assert len(hub_frames) == len(tones)  # the browser hub received every frame
    assert len(ctrl.frames) >= len(tones)  # and the controller saw them too — one reader, both fed
