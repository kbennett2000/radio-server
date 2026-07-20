"""AiocBaofeng backend (ADR 0029), driven entirely by injected fake serial/audio seams.

No hardware and no 'hardware' extra are needed here: the constructor's ``_serial_factory`` / ``_audio``
seams let the full keying + audio state machine run against fakes. The one hardware-gated test at the
bottom exercises real enumeration + a capture read (never keying — RF stays out of pytest).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from radio_server.backends import SHARED_CAPS, Radio, create_radio
from radio_server.backends.aioc_baofeng import _AiocTxPacer, _default_serial_factory


class FakeSerial:
    """Records every RTS/DTR write in order; a stand-in for a pyserial ``Serial``."""

    def __init__(self) -> None:
        self._rts = None
        self._dtr = None
        self.events: list[tuple[str, bool]] = []
        self.closed = False

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value
        self.events.append(("rts", value))

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value
        self.events.append(("dtr", value))

    def close(self):
        self.closed = True


class FakeInputStream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = self.stopped = self.closed = False
        self.reads = 0

    def start(self):
        self.started = True

    def read(self, frames):
        self.reads += 1
        return b"\x00\x00" * frames, False  # (silence of `frames` int16 samples, not overflowed)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeOutputStream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = self.stopped = self.closed = False
        self.written: list[bytes] = []

    def start(self):
        self.started = True

    def write(self, data):
        self.written.append(bytes(data))

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeAudio:
    """A sounddevice-like module exposing the two Raw stream factories the backend uses."""

    def __init__(self) -> None:
        self.inputs: list[FakeInputStream] = []
        self.outputs: list[FakeOutputStream] = []

    def RawInputStream(self, **kw):
        stream = FakeInputStream(**kw)
        self.inputs.append(stream)
        return stream

    def RawOutputStream(self, **kw):
        stream = FakeOutputStream(**kw)
        self.outputs.append(stream)
        return stream


def make_backend(*, ptt_line: str = "rts", tx_lead_seconds: float = 0.0, **kwargs) -> Radio:
    """Factory-build an AiocBaofeng with fake seams. Access ``radio._serial`` / ``radio._audio_mod``
    (the injected fakes) for assertions. Shared with test_factory.

    ``tx_lead_seconds`` defaults to **0** here (not the backend's real 0.5 s) so the keying/audio
    assertions below see only the caller's frames; the TX-lead-in tests pass an explicit value.
    """
    serial = FakeSerial()
    audio = FakeAudio()
    return create_radio(
        "baofeng",
        ptt_line=ptt_line,
        tx_lead_seconds=tx_lead_seconds,
        _serial_factory=lambda port: serial,
        _audio=audio,
        **kwargs,
    )


def a_frame(nsamples: int = 4) -> AudioFrame:
    return AudioFrame(b"\x01\x02" * nsamples, CANONICAL_FORMAT)


def _drain(radio, timeout: float = 2.0) -> None:
    """Wait for the keying's pacer to finish writing everything queued (ADR 0102).

    Streaming ``transmit`` is non-blocking now — the device writes land on the pacer thread — so
    tests that assert on ``FakeOutputStream.written`` must drain first.
    """
    pacer = radio._pacer
    if pacer is not None:
        assert pacer.wait_drained(timeout)


def _await_line_low(radio, line: str, timeout: float = 2.0) -> None:
    """Poll until the PTT line goes low — for failures that unkey from the pacer thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(radio._serial, line) is False:
            return
        time.sleep(0.005)
    raise AssertionError(f"{line} still asserted after {timeout}s")


# --- construction / capabilities ---------------------------------------------


def test_capabilities_are_shared_only_no_cat():
    radio = make_backend()
    assert radio.capabilities() == SHARED_CAPS
    assert not hasattr(radio, "set_frequency")
    assert isinstance(radio, Radio)


@pytest.mark.parametrize("ptt_line", ["rts", "dtr"])
def test_construction_leaves_both_lines_low_never_keys(ptt_line):
    radio = make_backend(ptt_line=ptt_line)
    serial = radio._serial
    assert serial.rts is False and serial.dtr is False
    # The RF-safety guard: nothing was ever asserted during construction.
    assert ("rts", True) not in serial.events
    assert ("dtr", True) not in serial.events


def test_unknown_ptt_line_fails_loud():
    with pytest.raises(ValueError, match="ptt_line"):
        make_backend(ptt_line="cts")


# --- transmit: format contract + one-shot self-keying ------------------------


def test_transmit_rejects_non_canonical_format_before_touching_audio():
    radio = make_backend()
    bad = AudioFrame(b"\x00\x00", AudioFormat(8000, 2, 1))
    with pytest.raises(AudioFormatMismatch):
        radio.transmit(bad)
    # No stream opened, no line asserted — a bad frame never keys the radio.
    assert radio._audio_mod.outputs == []
    assert ("rts", True) not in radio._serial.events


@pytest.mark.parametrize("ptt_line", ["rts", "dtr"])
def test_one_shot_transmit_self_keys_then_drops(ptt_line):
    radio = make_backend(ptt_line=ptt_line)
    other = "dtr" if ptt_line == "rts" else "rts"
    frame = a_frame()

    radio.transmit(frame)

    out = radio._audio_mod.outputs[-1]
    assert out.written == [frame.samples]  # the whole clip played
    assert out.stopped and out.closed  # drained (stop) and closed
    # The configured line went up then back down; the other line was never touched high.
    assert ("dtr" if ptt_line == "rts" else "rts", True) not in radio._serial.events
    assert getattr(radio._serial, ptt_line) is False  # dropped after the clip
    assert getattr(radio._serial, other) is False
    assert radio.status().transmitting is False


def test_key_on_stream_failure_never_asserts_the_line():
    # RF-safety: if opening the audio device fails, the PTT line must never have been asserted —
    # a failed key-up must not leave the transmitter keyed.
    radio = make_backend()

    def boom_output(**kw):
        raise OSError("PortAudio device open failed")

    radio._audio_mod.RawOutputStream = boom_output
    with pytest.raises(OSError):
        radio.ptt(True)
    assert radio._serial.rts is False and radio._serial.dtr is False
    assert radio.status().transmitting is False
    assert radio._keyed is False  # a failed key-up is not "keyed"


def test_one_shot_transmit_drops_line_even_if_write_raises():
    class BoomOutput(FakeOutputStream):
        def write(self, data):
            raise RuntimeError("device error")

    radio = make_backend()

    def boom_output(**kw):
        stream = BoomOutput(**kw)
        radio._audio_mod.outputs.append(stream)
        return stream

    radio._audio_mod.RawOutputStream = boom_output
    with pytest.raises(RuntimeError):
        radio.transmit(a_frame())
    # The finally-clause must still drop the line — never leave the transmitter keyed.
    assert radio._serial.rts is False
    assert radio.status().transmitting is False


# --- ADR 0093: the transmitter can never be stranded keyed -------------------------------------


def test_key_on_lead_in_write_failure_drops_the_line_not_stranded():
    # The AIOC stuck-key class, ADR 0102 shape: the lead-in write now happens on the pacer thread,
    # so a write that raises no longer propagates out of ptt(True) — instead the pacer's on_error
    # (_key_off) must unkey from ITS thread. The invariant is unchanged: a failed lead-in write can
    # never leave the transmitter stranded keyed.
    class LeadBoomOutput(FakeOutputStream):
        def write(self, data):
            raise RuntimeError("PortAudio write failed mid lead-in")

    radio = make_backend(ptt_line="dtr", tx_lead_seconds=0.02)

    def boom(**kw):
        stream = LeadBoomOutput(**kw)
        radio._audio_mod.outputs.append(stream)
        return stream

    radio._audio_mod.RawOutputStream = boom
    radio.ptt(True)  # key-up itself succeeds; the failure fires on the pacer thread
    _await_line_low(radio, "dtr")  # ...which must drop the line (pacer on_error -> _key_off)
    assert radio.status().transmitting is False
    # And the safety lever still works: a later ptt(False) keeps the line low, no strand, no raise.
    radio.ptt(False)
    assert radio._serial.dtr is False


def test_streaming_write_failure_mid_over_unkeys_from_the_pacer_thread():
    # ADR 0102: the device dying mid-over (unplug, xrun storm) fails a write on the pacer thread;
    # the pacer must discard, stop, and unkey — never strand the key or kill the caller.
    class SecondWriteBoom(FakeOutputStream):
        def write(self, data):
            super().write(data)
            if len(self.written) >= 2:
                raise RuntimeError("device vanished mid-over")

    radio = make_backend(ptt_line="dtr")

    def boom(**kw):
        stream = SecondWriteBoom(**kw)
        radio._audio_mod.outputs.append(stream)
        return stream

    radio._audio_mod.RawOutputStream = boom
    radio.ptt(True)
    radio.transmit(a_frame(2))  # first write: fine
    radio.transmit(a_frame(3))  # second write raises on the pacer thread
    _await_line_low(radio, "dtr")
    assert radio.status().transmitting is False
    radio.ptt(False)  # still safe/idempotent afterward
    assert radio._serial.dtr is False


def test_key_off_drops_the_line_even_if_stream_stop_raises():
    # RF-safety: dropping the transmitter must never depend on the audio teardown succeeding. A
    # stop() that raises on an xrun'd/starved stream must NOT keep the line asserted.
    class StopBoomOutput(FakeOutputStream):
        def stop(self):
            raise RuntimeError("PortAudio stop failed (xrun)")

    radio = make_backend(ptt_line="dtr")

    def boom(**kw):
        stream = StopBoomOutput(**kw)
        radio._audio_mod.outputs.append(stream)
        return stream

    radio._audio_mod.RawOutputStream = boom
    radio.ptt(True)
    assert radio._serial.dtr is True
    radio.ptt(False)  # stop() raises inside _key_off — must not propagate, must still unkey
    assert radio._serial.dtr is False
    assert radio.status().transmitting is False


def test_key_off_drops_the_line_before_tearing_down_the_stream():
    # The RF-safety inversion (ADR 0093): the line is low BEFORE we touch the stream — so a drain
    # that blocks or a stop() that hangs can never keep the transmitter keyed. Prove the ordering by
    # recording the line state at the moment stop() runs.
    radio = make_backend(ptt_line="dtr")
    radio.ptt(True)
    out = radio._audio_mod.outputs[0]
    serial = radio._serial
    seen = {}
    real_stop = out.stop

    def recording_stop():
        seen["dtr_at_stop"] = serial.dtr
        real_stop()

    out.stop = recording_stop
    radio.ptt(False)
    assert seen["dtr_at_stop"] is False  # line already LOW when the stream was stopped
    assert out.stopped and out.closed


def test_ptt_off_forces_line_low_even_when_flag_says_not_keyed():
    # Defense in depth: even if `_keyed` desynced to False while the line is somehow still asserted,
    # ptt(False) must force the line low — it is the caller's unconditional safety lever.
    radio = make_backend(ptt_line="dtr")
    radio._serial.dtr = True  # simulate a physically-asserted line
    radio._keyed = False  # ...with the tracked flag desynced to "not keyed"
    radio.ptt(False)
    assert radio._serial.dtr is False
    assert radio.status().transmitting is False


# --- streaming keying: ptt(True) holds the line across frames ----------------


@pytest.mark.parametrize("ptt_line", ["rts", "dtr"])
def test_streaming_holds_one_stream_across_frames(ptt_line):
    radio = make_backend(ptt_line=ptt_line)
    radio.ptt(True)
    assert getattr(radio._serial, ptt_line) is True
    assert radio.status().transmitting is True
    assert len(radio._audio_mod.outputs) == 1  # one playback stream opened on key-up

    f1, f2 = a_frame(2), a_frame(3)
    radio.transmit(f1)
    radio.transmit(f2)
    _drain(radio)  # writes land on the pacer thread (ADR 0102)
    # Same single stream got both frames — the line was NOT dropped between frames.
    assert len(radio._audio_mod.outputs) == 1
    assert radio._audio_mod.outputs[0].written == [f1.samples, f2.samples]
    assert getattr(radio._serial, ptt_line) is True  # still keyed

    radio.ptt(False)
    assert getattr(radio._serial, ptt_line) is False
    assert radio._audio_mod.outputs[0].stopped and radio._audio_mod.outputs[0].closed
    assert radio.status().transmitting is False


def test_ptt_is_idempotent():
    radio = make_backend()
    radio.ptt(True)
    radio.ptt(True)  # no second stream, still keyed
    assert len(radio._audio_mod.outputs) == 1
    radio.ptt(False)
    radio.ptt(False)  # no-op, no error, no spurious extra drop-close
    assert radio._audio_mod.outputs[0].closed


# --- TX lead-in (ADR 0032): silence after key-up so speech isn't clipped -----


def _expected_lead_bytes(seconds: float) -> int:
    return round(CANONICAL_FORMAT.rate * seconds) * CANONICAL_FORMAT.frame_bytes


def test_one_shot_transmit_writes_lead_in_silence_before_audio():
    radio = make_backend(tx_lead_seconds=0.5)
    frame = a_frame()

    radio.transmit(frame)

    out = radio._audio_mod.outputs[-1]
    lead = b"\x00" * _expected_lead_bytes(0.5)
    # The silent lead-in is played first (radio keys up during it), then the real clip.
    assert out.written == [lead, frame.samples]
    assert set(out.written[0]) == {0}  # genuinely silent
    assert len(out.written[0]) == _expected_lead_bytes(0.5)


def test_streaming_writes_lead_in_once_at_keyup_not_per_frame():
    radio = make_backend(tx_lead_seconds=0.02)
    radio.ptt(True)  # key-up: the lead-in is queued here, once
    f1, f2 = a_frame(2), a_frame(3)
    radio.transmit(f1)
    radio.transmit(f2)
    _drain(radio)

    lead = b"\x00" * _expected_lead_bytes(0.02)
    assert radio._audio_mod.outputs[0].written == [lead, f1.samples, f2.samples]


def test_tx_lead_seconds_zero_writes_no_silence():
    radio = make_backend(tx_lead_seconds=0.0)
    frame = a_frame()
    radio.transmit(frame)
    assert radio._audio_mod.outputs[-1].written == [frame.samples]  # real audio only, no lead


def test_lead_bytes_precomputed_from_rate_and_format():
    radio = make_backend(tx_lead_seconds=0.5)
    assert radio._lead_bytes == _expected_lead_bytes(0.5) == 48000  # 24000 mono int16 samples


# --- ADR 0102: the pacer — no blocking writes on the caller, unkey discards ---


class GatedOutputStream(FakeOutputStream):
    """A slow device: every write records, then parks until the test releases the gate."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gate = threading.Event()

    def write(self, data):
        super().write(data)
        self.gate.wait(timeout=5.0)


def test_streaming_transmit_never_blocks_on_a_slow_device():
    # THE crossband fix: a device write that back-pressures (the real sounddevice contract) must
    # park the PACER thread, never the transmit() caller (the asyncio event loop). Pre-ADR 0102
    # this test would hang on the second transmit.
    radio = make_backend(ptt_line="dtr")
    gated = None

    def make_gated(**kw):
        nonlocal gated
        gated = GatedOutputStream(**kw)
        radio._audio_mod.outputs.append(gated)
        return gated

    radio._audio_mod.RawOutputStream = make_gated
    radio.ptt(True)
    f1, f2, f3 = a_frame(2), a_frame(3), a_frame(4)

    started = time.monotonic()
    radio.transmit(f1)  # pacer picks this up and parks in the gated write
    radio.transmit(f2)  # must return immediately — queued, not written
    radio.transmit(f3)
    assert time.monotonic() - started < 1.0  # no per-write blocking on the caller
    # Only the first write has happened; the rest are queued behind the parked device.
    deadline = time.monotonic() + 2.0
    while not gated.written and time.monotonic() < deadline:
        time.sleep(0.005)
    assert gated.written == [f1.samples]

    # Unkey while the device is still parked: the line must drop IMMEDIATELY (never waiting on the
    # audio path), and the queued frames must be DISCARDED (the "long FM tail" fix), not drained.
    releaser = threading.Timer(0.05, gated.gate.set)
    releaser.start()
    try:
        radio.ptt(False)
    finally:
        releaser.cancel()
        gated.gate.set()
    assert radio._serial.dtr is False
    assert radio.status().transmitting is False
    time.sleep(0.05)  # give the (stopped) pacer thread a beat: nothing further may be written
    assert gated.written == [f1.samples]  # f2/f3 discarded on unkey


def test_pacer_drop_oldest_over_the_bound_keeps_latency_bounded():
    # Direct unit test of the pacer's jitter buffer: a parked device + a bursting producer must
    # drop the OLDEST chunks (stay current) and count the discarded bytes.
    stream = GatedOutputStream()
    pacer = _AiocTxPacer(stream, max_buffer_bytes=8)
    try:
        pacer.enqueue(b"a" * 4)  # picked up by the writer -> parks in the gated write
        deadline = time.monotonic() + 2.0
        while not stream.written and time.monotonic() < deadline:
            time.sleep(0.005)
        pacer.enqueue(b"b" * 4)
        pacer.enqueue(b"c" * 4)
        pacer.enqueue(b"d" * 4)  # 12 buffered > 8: 'b' (the oldest buffered) is dropped
        assert pacer.dropped_bytes == 4
        stream.gate.set()
        assert pacer.wait_drained(2.0)
        assert stream.written == [b"a" * 4, b"c" * 4, b"d" * 4]
    finally:
        stream.gate.set()
        pacer.stop()


def test_pacer_stop_is_idempotent_and_enqueue_after_stop_is_a_noop():
    stream = FakeOutputStream()
    pacer = _AiocTxPacer(stream, max_buffer_bytes=64)
    pacer.stop()
    pacer.stop()  # idempotent
    pacer.enqueue(b"zz")  # dropped silently — a torn-down keying must not resurrect writes
    assert pacer.wait_drained(0.2)
    assert stream.written == []


# --- receive -----------------------------------------------------------------


def test_receive_lazily_opens_capture_and_returns_canonical_frame():
    radio = make_backend(blocksize=480)
    assert radio._audio_mod.inputs == []  # not opened until first receive
    frame = radio.receive()
    assert isinstance(frame, AudioFrame)
    assert frame.format == CANONICAL_FORMAT
    assert len(frame.samples) == 480 * CANONICAL_FORMAT.frame_bytes
    assert len(radio._audio_mod.inputs) == 1 and radio._audio_mod.inputs[0].started
    radio.receive()
    assert len(radio._audio_mod.inputs) == 1  # reused, not reopened


# --- status ------------------------------------------------------------------


def test_status_reports_no_busy_line():
    radio = make_backend()
    status = radio.status()
    assert status.backend == "baofeng"
    assert status.busy is False
    assert status.frequency is None and status.mode is None  # no CAT


# --- lifecycle / safety ------------------------------------------------------


def test_close_drops_line_and_closes_serial_idempotent():
    radio = make_backend()
    radio.ptt(True)
    radio.close()
    assert radio._serial.rts is False and radio._serial.dtr is False
    assert radio._serial.closed is True
    radio.close()  # idempotent


# --- lazy-import error surface (no hardware extra) ---------------------------


def test_missing_sounddevice_gives_actionable_error(monkeypatch):
    radio = make_backend()
    radio._audio_mod = None  # force the real lazy-import path
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # make `import sounddevice` raise
    with pytest.raises(RuntimeError, match="hardware.*extra"):
        radio.receive()


def test_missing_pyserial_gives_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)
    with pytest.raises(RuntimeError, match="hardware.*extra"):
        _default_serial_factory("/dev/ttyACM0")


# --- hardware-gated (enumeration + capture read only; NEVER keying) ----------

def _hw_ready() -> bool:
    """True only when the real AIOC path can actually run: the device node is present AND both
    libraries import — including PortAudio, which sounddevice loads at import and which raises
    OSError (not ImportError) when the system libportaudio2 is missing."""
    if not Path("/dev/ttyACM0").exists():
        return False
    try:
        import serial  # noqa: F401  (pyserial)
        import sounddevice  # noqa: F401  (raises OSError if libportaudio2 is absent)
    except Exception:
        return False
    return True


_HW_SKIP = pytest.mark.skipif(
    not _hw_ready(),
    reason="AIOC hardware / pyserial / sounddevice / PortAudio not present",
)


@_HW_SKIP
def test_real_aioc_capture_reads_a_block():
    # Real device: open the AIOC card and read one block. Does NOT assert RTS/DTR — keying is
    # verified only by the interactive `python -m radio_server.doctor --key-test`, never in pytest.
    radio = create_radio("baofeng")
    try:
        frame = radio.receive()
        assert frame.format == CANONICAL_FORMAT
        assert len(frame.samples) > 0
    finally:
        radio.close()
