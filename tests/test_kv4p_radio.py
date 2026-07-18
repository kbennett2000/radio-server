"""Unit tests for the ``Kv4pHt`` backend (ADR 0061, ADR 0063), fake-transport only.

No serial, no threads: a :class:`FakeTransport` stands in for :class:`Kv4pTransport` via the
``_transport`` seam. It records every ``HostDesiredState`` the backend sends and every TX-audio
packet, and synthesizes a ``DeviceState`` that echoes the last desired state — adding ``TX_ACTIVE``
when PTT was requested (and the fake is set to grant it) and ``SQUELCHED`` per its ``squelched``
flag — so keying and ``status()`` behave like a (cooperative) device without any hardware.
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from radio_server.backends.base import Capability, UnsupportedCapability
from radio_server.backends.kv4p import audio as kv4p_audio
from radio_server.backends.kv4p.frames import (
    DeviceState,
    DeviceStateFlag,
    HostDesiredState,
    HostStateFlag,
    RfModuleType,
)
from radio_server.backends.kv4p.pacer import _TxPacer
from radio_server.backends.kv4p.radio import _KV4P_CAPS, Kv4pHt, Kv4pKeyingError
from radio_server.backends.kv4p.transport import Kv4pClosed, Kv4pTimeout, TxStats


# --------------------------------------------------------------------------------------
# Fake transport + helpers
# --------------------------------------------------------------------------------------


class FakeTransport:
    """A cooperative in-process stand-in for ``Kv4pTransport`` (no serial, no threads)."""

    def __init__(
        self,
        *,
        grant_tx: bool = True,
        squelched: bool = True,
        hello=None,
        window_size: int = 2048,
    ) -> None:
        self.sent: list[HostDesiredState] = []  # every desired state the backend reconciled
        self.tx_audio: list[bytes] = []  # every Opus packet the backend transmitted
        self.grant_tx = grant_tx
        self.squelched = squelched
        self._hello = hello
        self._window_size = window_size
        self._rx: deque[bytes] = deque()
        self._seq = 0
        self._last_state: DeviceState | None = None
        self.closed = False
        self._tx_stats = TxStats()  # mirrors the real transport's per-keying TX telemetry (ADR 0069)

    # -- Kv4pTransport surface --
    def connect(self, timeout: float = 2.0) -> DeviceState:
        self._last_state = self._synth(None)  # the probe response; not recorded in `sent`
        return self._last_state

    def send_desired_state(self, state: HostDesiredState) -> int:
        self._seq += 1
        self.sent.append(state)
        self._last_state = self._synth(state)
        return self._seq

    def await_applied(self, seq: int, timeout: float) -> DeviceState:
        assert self._last_state is not None
        return self._last_state

    def send_tx_audio(self, packet: bytes) -> None:
        self.tx_audio.append(packet)
        self._tx_stats.record_audio(len(packet), len(packet) + 9)  # +9 ≈ vendor header + 2 FENDs

    @property
    def tx_stats(self) -> TxStats:
        import dataclasses

        return dataclasses.replace(self._tx_stats)

    def reset_tx_stats(self) -> None:
        self._tx_stats = TxStats()

    def read_audio(self) -> bytes | None:
        return self._rx.popleft() if self._rx else None

    @property
    def device_state(self) -> DeviceState | None:
        return self._last_state

    @property
    def hello(self):
        return self._hello

    @property
    def window_size(self) -> int:
        return self._window_size

    def close(self) -> None:
        self.closed = True

    # -- test driving --
    def feed_rx(self, packet: bytes) -> None:
        self._rx.append(packet)

    def _synth(self, desired: HostDesiredState | None) -> DeviceState:
        flags = DeviceStateFlag(0)
        bw = freq_tx = freq_rx = ctcss_tx = ctcss_rx = 0
        fx = fr = 0.0
        if desired is not None:
            requested = HostStateFlag(desired.flags)
            # Echo the config/PTT bits the firmware would keep, and key iff PTT + we grant it.
            flags = DeviceStateFlag(int(requested) & 0x0FFF)
            if (requested & HostStateFlag.PTT_REQUESTED) and self.grant_tx:
                flags |= DeviceStateFlag.TX_ACTIVE
            else:
                flags &= ~DeviceStateFlag.TX_ACTIVE
            bw = desired.bw
            fx, fr = desired.freq_tx, desired.freq_rx
            ctcss_tx, ctcss_rx = desired.ctcss_tx, desired.ctcss_rx
        if self.squelched:
            flags |= DeviceStateFlag.SQUELCHED
        return DeviceState(
            applied_sequence=self._seq,
            memory_id=0,
            flags=int(flags),
            bw=bw,
            freq_tx=fx,
            freq_rx=fr,
            ctcss_tx=ctcss_tx,
            squelch=0,
            ctcss_rx=ctcss_rx,
            radio_module_status=0,
            mode=0,
            last_error=0,
            latest_rssi=0,
        )


def make_radio(fake: FakeTransport, **kwargs) -> Kv4pHt:
    kwargs.setdefault("tx_lead_seconds", 0.0)  # keep tx_audio free of lead-in blocks
    kwargs.setdefault("receive_timeout", 0.05)
    # Default OFF so RX-plumbing tests see exact 1920-sample frames; the ADR-0070 correction itself is
    # covered in test_kv4p_audio / test_native_dtmf. The wiring is asserted below.
    kwargs.setdefault("sample_rate_correction", 1.0)
    return Kv4pHt(_transport=fake, **kwargs)


def a_frame(nsamples: int = 4800) -> AudioFrame:
    """A canonical 48k silence frame long enough to produce whole Opus frames (1920 samples each)."""
    return AudioFrame(b"\x00\x00" * nsamples)


def _opus_or_skip():
    """Make libopus loadable (ADR 0056/0057), then import opuslib or skip if it isn't installed."""
    from radio_server.link._opus import ensure_opus_loadable

    ensure_opus_loadable()
    return pytest.importorskip("opuslib")


def an_opus_packet(opuslib) -> bytes:
    """One valid Opus packet (40 ms of silence) for the RX path."""
    enc = opuslib.Encoder(kv4p_audio.OPUS_RATE, kv4p_audio.OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
    enc.max_bandwidth = opuslib.BANDWIDTH_NARROWBAND
    return enc.encode(b"\x00" * kv4p_audio.FRAME_BYTES, kv4p_audio.FRAME_SAMPLES)


def flags_of(state: HostDesiredState) -> HostStateFlag:
    return HostStateFlag(state.flags)


# --------------------------------------------------------------------------------------
# The whole-word flag regression (the one that matters most)
# --------------------------------------------------------------------------------------


def test_set_frequency_then_ptt_keeps_config_valid_tx_allowed_and_rx_open():
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    flags = flags_of(fake.sent[-1])
    # Omitting any of these from a later frame silently clears it on the device.
    assert HostStateFlag.RADIO_CONFIG_VALID in flags
    assert HostStateFlag.TX_ALLOWED in flags
    assert HostStateFlag.RX_AUDIO_OPEN in flags
    assert HostStateFlag.PTT_REQUESTED in flags
    radio.close()


def test_ptt_raises_and_clears_when_device_never_keys():
    fake = FakeTransport(grant_tx=False)  # device refuses to key (its TX gate off / RF fault)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    with pytest.raises(Kv4pKeyingError):
        radio.ptt(True)
    assert radio._keyed is False  # not left in a keyed state
    assert HostStateFlag.PTT_REQUESTED not in flags_of(fake.sent[-1])  # fail-safe cleared the flag
    radio.close()


# --------------------------------------------------------------------------------------
# Units — the wire speaks floats/indices, not our types
# --------------------------------------------------------------------------------------


def test_set_frequency_converts_hz_to_float_mhz_on_both_legs():
    fake = FakeTransport()
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    sent = fake.sent[-1]
    assert sent.freq_tx == pytest.approx(146.52)  # simplex — both legs set
    assert sent.freq_rx == pytest.approx(146.52)
    radio.close()


def test_set_tone_maps_hz_to_index_tx_only():
    fake = FakeTransport()
    radio = make_radio(fake)
    radio.set_tone(146.2)  # the 23rd standard CTCSS tone
    assert fake.sent[-1].ctcss_tx == 23
    assert fake.sent[-1].ctcss_rx == 0  # RX tone squelch stays off
    radio.set_tone(None)
    assert fake.sent[-1].ctcss_tx == 0
    radio.close()


def test_unmapped_tone_raises():
    fake = FakeTransport()
    radio = make_radio(fake)
    with pytest.raises(ValueError):
        radio.set_tone(99.0)  # not within tolerance of any table entry
    radio.close()


def test_out_of_band_frequency_raises_before_sending_anything():
    fake = FakeTransport()  # VHF module default band 134–174 MHz
    radio = make_radio(fake)
    before = len(fake.sent)
    with pytest.raises(ValueError):
        radio.set_frequency(450_000_000)
    assert len(fake.sent) == before  # nothing went to the wire
    radio.close()


def test_module_type_uhf_accepts_a_uhf_frequency_without_a_hello():
    """A UHF board with no HELLO must validate against the UHF band, not the VHF default.

    HELLO is boot-only (ADR 0062), so on a server restart against a running board the module_type
    config is the only thing that sets the band. Without kv4p.module_type this UHF frequency would
    be rejected as out-of-band — the bug this setting fixes."""
    fake = FakeTransport(hello=None)  # no HELLO -> the module_type fallback decides the band
    radio = make_radio(fake, module_type="uhf")
    radio.set_frequency(445_800_000)  # in the SA818-UHF band; must NOT raise
    assert fake.sent[-1].freq_rx == pytest.approx(445.8)
    radio.close()

    # And the default (vhf) still rejects it — proving the setting is what changed the band.
    vhf = FakeTransport(hello=None)
    vhf_radio = make_radio(vhf)  # default module_type
    with pytest.raises(ValueError):
        vhf_radio.set_frequency(445_800_000)
    vhf_radio.close()


def test_module_type_accepts_a_vhf_or_uhf_string_and_a_band_enum():
    """module_type takes a 'vhf'/'uhf' string (the config spelling) or a Kv4pBand — mapped to band."""
    from radio_server.backends.kv4p.radio import Kv4pBand

    for arg in ("uhf", Kv4pBand.UHF):
        fake = FakeTransport(hello=None)
        radio = make_radio(fake, module_type=arg)
        radio.set_frequency(446_000_000)  # UHF — accepted
        radio.close()


def test_set_mode_maps_to_bandwidth_and_rejects_others():
    fake = FakeTransport()
    radio = make_radio(fake)
    radio.set_mode("NFM")
    assert fake.sent[-1].bw == 0  # 12.5 kHz
    radio.set_mode("FM")
    assert fake.sent[-1].bw == 1  # 25 kHz
    with pytest.raises(ValueError):
        radio.set_mode("USB")
    radio.close()


# --------------------------------------------------------------------------------------
# Capabilities (ADR 0063)
# --------------------------------------------------------------------------------------


def test_capabilities_are_exactly_the_kv4p_set():
    fake = FakeTransport()
    radio = make_radio(fake)
    caps = radio.capabilities()
    assert caps == _KV4P_CAPS
    assert Capability.SET_CHANNEL not in caps
    assert Capability.SCAN in caps  # the software ScanEngine can run here
    radio.close()


def test_set_channel_is_unsupported():
    fake = FakeTransport()
    radio = make_radio(fake)
    with pytest.raises(UnsupportedCapability):
        radio.set_channel(3)
    radio.close()


def test_scan_toggle_raises_no_native_scan():
    fake = FakeTransport()
    radio = make_radio(fake)
    with pytest.raises(NotImplementedError):
        radio.scan(True)
    radio.close()


# --------------------------------------------------------------------------------------
# status()
# --------------------------------------------------------------------------------------


def test_status_busy_is_the_inverse_of_squelched():
    squelched = make_radio(FakeTransport(squelched=True))
    assert squelched.status().busy is False
    open_carrier = make_radio(FakeTransport(squelched=False))
    assert open_carrier.status().busy is True
    squelched.close()
    open_carrier.close()


def test_status_reports_tx_active_and_round_trips_tuning():
    fake = FakeTransport(grant_tx=True, squelched=False)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.set_tone(146.2)
    radio.set_mode("NFM")
    status = radio.status()
    assert status.frequency == 146_520_000
    assert status.tone == pytest.approx(146.2)
    assert status.mode == "NFM"
    assert status.transmitting is False
    radio.ptt(True)
    assert radio.status().transmitting is True
    radio.close()


# --------------------------------------------------------------------------------------
# Keying discipline (one-shot vs streaming)
# --------------------------------------------------------------------------------------


def test_one_shot_transmit_self_keys_then_drops():
    _opus_or_skip()  # transmit() encodes through the Opus TX path
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    before = len(fake.sent)
    radio.transmit(a_frame())
    window = fake.sent[before:]
    assert any(HostStateFlag.PTT_REQUESTED in flags_of(s) for s in window)  # keyed for the clip
    assert HostStateFlag.PTT_REQUESTED not in flags_of(fake.sent[-1])  # dropped after
    assert radio._keyed is False
    assert fake.tx_audio  # Opus packets went out
    radio.close()


def test_streaming_holds_the_key_across_frames_and_drops_once():
    _opus_or_skip()  # transmit() encodes through the Opus TX path
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    keyed_at = len(fake.sent)
    for _ in range(3):
        radio.transmit(a_frame())  # ADR 0082: enqueues to the pacer, which sends on its own thread
    # Streaming transmits only feed the pacer — no per-frame desired-state reconciles.
    assert len(fake.sent) == keyed_at
    radio.ptt(False)
    # The pacer flushed everything enqueued on key-down, so audio did go out over this over.
    assert len(fake.tx_audio) > 0
    assert HostStateFlag.PTT_REQUESTED not in flags_of(fake.sent[-1])  # dropped once, at the end
    assert radio._keyed is False
    assert radio._pacer is None and radio._tx is None
    radio.close()


def test_tx_gain_threads_to_the_encoder_built_at_key_up():
    # ADR 0080: the kv4p.tx_gain setting must reach the TxAudioEncoder that every TX byte flows
    # through. A streaming key with tx_lead_seconds=0 encodes nothing on key-up, so this needs no
    # libopus. Both transmit paths build their encoder in the same _key_on(), so proving the
    # streaming key carries the gain proves the one-shot path does too (covered live below).
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake, tx_gain=0.5)
    assert radio._tx_gain == 0.5
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    assert radio._tx is not None
    assert radio._tx._tx_gain == 0.5  # the live encoder scales every sample before Opus
    radio.ptt(False)
    radio.close()


def test_tx_gain_defaults_to_unity_no_op():
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)  # no tx_gain set
    assert radio._tx_gain == 1.0
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    assert radio._tx is not None and radio._tx._tx_gain == 1.0
    radio.ptt(False)
    radio.close()


def _tone_frame(nsamples: int = 4800, *, freq: float = 0.03, amp: int = 8000) -> AudioFrame:
    """A canonical 48k mono s16 tone — real signal so a gain change is measurable after decode."""
    t = np.arange(nsamples)
    return AudioFrame((np.sin(t * freq) * amp).astype("<i2").tobytes())


def test_one_shot_transmit_is_attenuated_end_to_end():
    # _key_off() nulls the encoder, so inspect what actually went on the air instead: transmit the
    # same tone at unity and at 0.5, decode the emitted Opus, and confirm the one-shot TX path
    # halved the energy. This guards the one-shot path specifically (not just the streaming key).
    _opus_or_skip()

    def transmitted_rms(gain: float) -> float:
        fake = FakeTransport(grant_tx=True)
        radio = make_radio(fake, tx_gain=gain)
        radio.set_frequency(146_520_000)
        radio.transmit(_tone_frame())  # one-shot: self-keys, encodes, drops
        radio.close()
        dec = kv4p_audio.RxAudioDecoder()
        pcm = b"".join(dec.push(p).samples for p in fake.tx_audio)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        return float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0

    unity = transmitted_rms(1.0)
    half = transmitted_rms(0.5)
    assert unity > 0
    assert half == pytest.approx(unity * 0.5, rel=0.15)  # ~halved (Opus is lossy, so a tolerance)


# --------------------------------------------------------------------------------------
# receive()
# --------------------------------------------------------------------------------------


def test_receive_decodes_an_opus_packet_to_a_canonical_frame():
    opuslib = _opus_or_skip()
    fake = FakeTransport()
    radio = make_radio(fake)
    fake.feed_rx(an_opus_packet(opuslib))
    frame = radio.receive()
    assert isinstance(frame, AudioFrame)
    assert frame.format == CANONICAL_FORMAT
    assert len(frame.samples) == kv4p_audio.FRAME_BYTES  # one 40 ms packet -> one 1920-sample frame
    radio.close()


def test_sample_rate_correction_reaches_the_rx_decoder():
    # ADR 0070: the config knob must thread into the decoder so the firmware's ~2%-fast ADC is undone.
    fake = FakeTransport()
    radio = make_radio(fake, sample_rate_correction=1.02)
    assert radio._rx._device_rate == 48960  # round(48000 * 1.02) — the true device rate
    assert radio._rx._resampler is not None  # correction engaged
    radio.close()


def test_receive_drops_a_corrupt_opus_packet_without_raising():
    # ADR 0065: a corrupt/truncated packet off the wire must be dropped inside the decoder (empty
    # frame), never propagate an OpusError up the unguarded RX pump and kill the capture task.
    _opus_or_skip()
    fake = FakeTransport()
    radio = make_radio(fake)
    fake.feed_rx(b"\xff\xff\xff")  # a corrupted Opus stream
    frame = radio.receive()
    assert frame.samples == b""
    assert frame.format == CANONICAL_FORMAT
    radio.close()


def test_receive_times_out_cleanly_on_an_empty_queue():
    fake = FakeTransport()
    radio = make_radio(fake, receive_timeout=0.02)
    frame = radio.receive()
    assert frame.samples == b""  # blocked ~one timeout, then an empty frame
    assert frame.format == CANONICAL_FORMAT
    radio.close()


def test_transmit_rejects_a_non_canonical_frame():
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    from radio_server.audio import AudioFormat

    bad = AudioFrame(b"\x00\x00", AudioFormat(8000, 2, 1))
    with pytest.raises(AudioFormatMismatch):
        radio.transmit(bad)
    radio.close()


# --------------------------------------------------------------------------------------
# Constructor config (the wiring cycle: squelch / high_power / tx_allowed / frequency)
# --------------------------------------------------------------------------------------


def test_config_flags_and_squelch_ride_the_first_frame():
    fake = FakeTransport()
    radio = make_radio(fake, squelch=6, high_power=True, tx_allowed=True)
    first = fake.sent[0]
    assert first.squelch == 6
    assert HostStateFlag.RX_AUDIO_OPEN in flags_of(first)
    assert HostStateFlag.TX_ALLOWED in flags_of(first)
    assert HostStateFlag.HIGH_POWER in flags_of(first)
    radio.close()


def test_tx_allowed_false_withholds_the_flag():
    fake = FakeTransport()
    radio = make_radio(fake, tx_allowed=False)
    assert HostStateFlag.TX_ALLOWED not in flags_of(fake.sent[0])
    radio.close()


def test_high_power_false_withholds_the_flag():
    fake = FakeTransport()
    radio = make_radio(fake, high_power=False)
    assert HostStateFlag.HIGH_POWER not in flags_of(fake.sent[0])
    radio.close()


def test_no_frequency_does_not_tune_at_construction():
    fake = FakeTransport()
    radio = make_radio(fake)  # frequency defaults to None
    assert len(fake.sent) == 1  # only the initial reconcile, no tune
    only = fake.sent[0]
    assert HostStateFlag.RADIO_CONFIG_VALID not in flags_of(only)
    # freq echoes what connect() reported (0.0 from this fake) — the seed, not a hardcoded zero.
    assert only.freq_rx == 0.0 and only.freq_tx == 0.0
    radio.close()


def test_initial_reconcile_preserves_the_boards_stored_frequency():
    """The backend seeds its model from connect()'s state, so the first reconcile never persists
    freq 0.0 over the operator's stored frequency (ADR 0066)."""
    fake = FakeTransport()
    stored = DeviceState(
        applied_sequence=5, memory_id=2, flags=0, bw=1, freq_tx=146.52, freq_rx=146.52,
        ctcss_tx=0, squelch=0, ctcss_rx=0, radio_module_status=0, mode=1, last_error=0, latest_rssi=0,
    )
    fake.connect = lambda timeout=2.0: (setattr(fake, "_last_state", stored) or stored)
    radio = make_radio(fake)  # no configured frequency
    only = fake.sent[0]
    assert HostStateFlag.RADIO_CONFIG_VALID not in flags_of(only)  # still no retune
    # The board's stored frequency is carried, not zeroed.
    assert only.freq_rx == pytest.approx(146.52) and only.freq_tx == pytest.approx(146.52)
    radio.close()


def test_configured_frequency_tunes_once_at_construction():
    fake = FakeTransport()
    radio = make_radio(fake, frequency=146_520_000)
    assert len(fake.sent) == 2  # initial reconcile, then the tune
    tuned = fake.sent[-1]
    assert HostStateFlag.RADIO_CONFIG_VALID in flags_of(tuned)
    assert tuned.freq_rx == pytest.approx(146.52)
    assert tuned.freq_tx == pytest.approx(146.52)
    radio.close()


def test_out_of_band_configured_frequency_raises_from_construction():
    fake = FakeTransport()
    with pytest.raises(ValueError):  # reuses set_frequency's out-of-band validation
        make_radio(fake, frequency=450_000_000)  # UHF into the VHF default band


# --------------------------------------------------------------------------------------
# The keyed-idle TX pacer (ADR 0082) — keep the firmware fed so it never loops stale audio
# --------------------------------------------------------------------------------------
#
# Policy is tested by driving _TxPacer.tick() directly: each tick() == one advanced frame interval
# (the deterministic "fake clock"), exactly what the daemon thread does per slot. The lifecycle
# tests go through Kv4pHt but stop() the auto-started pacer thread first (stop() joins it), then
# drive ticks by hand, so the assertions are race-free.


def _rms(samples_bytes: bytes) -> float:
    s = np.frombuffer(samples_bytes, dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(s**2))) if s.size else 0.0


def test_pacer_emits_one_silence_frame_per_idle_tick():
    # The starve regression: keyed with NO real audio, every slot must still put one frame on the
    # wire so the firmware decoder never underruns and loops its last content.
    _opus_or_skip()
    sent: list[bytes] = []
    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), sent.append)
    for _ in range(5):
        pacer.tick()
    assert len(sent) == 5  # exactly one frame per idle slot
    dec = kv4p_audio.RxAudioDecoder()
    for packet in sent:
        frame = dec.push(packet)
        samples = np.frombuffer(frame.samples, dtype="<i2")
        assert samples.size == kv4p_audio.FRAME_SAMPLES  # a whole 40 ms frame
        assert _rms(frame.samples) < 50.0  # ~silence


def test_pacer_fills_gaps_with_silence_one_frame_per_slot_no_doubling():
    # Sparse real audio: a frame, then a gap, then a frame. The gap is filled with silence, the real
    # frames pass through, and EXACTLY one frame occupies each slot (the no-doubling guarantee).
    _opus_or_skip()
    sent: list[bytes] = []
    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), sent.append)
    tone = _tone_frame(kv4p_audio.FRAME_SAMPLES).samples  # exactly one frame of real signal

    pacer.enqueue(tone)
    pacer.tick()          # real
    for _ in range(3):
        pacer.tick()      # gap -> silence x3 (a multi-frame gap so Opus prediction decays)
    pacer.enqueue(tone)
    pacer.tick()          # real resumes

    assert len(sent) == 5  # exactly one per tick, never two, never zero — no doubling
    dec = kv4p_audio.RxAudioDecoder()
    energies = [_rms(dec.push(p).samples) for p in sent]
    assert energies[0] > 100.0  # real
    assert energies[3] < 50.0   # deep in the gap -> true silence (codec state has decayed)
    assert energies[4] > 100.0  # real resumes after the gap


def test_pacer_real_frame_respects_tx_gain_and_silence_is_gain_invariant():
    # tx_gain (ADR 0080) applies to real audio through the pacer path; silence is zeros, so it is
    # gain-invariant (neither attenuated nor doubled).
    _opus_or_skip()

    def real_rms(gain: float) -> float:
        sent: list[bytes] = []
        pacer = _TxPacer(kv4p_audio.TxAudioEncoder(tx_gain=gain), sent.append)
        pacer.enqueue(_tone_frame(kv4p_audio.FRAME_SAMPLES).samples)
        pacer.tick()
        return _rms(kv4p_audio.RxAudioDecoder().push(sent[0]).samples)

    unity, half = real_rms(1.0), real_rms(0.5)
    assert unity > 0
    assert half == pytest.approx(unity * 0.5, rel=0.2)  # halved (Opus is lossy)

    # A silence slot at gain 0.5 is still zeros — gain never scales the fill.
    sent: list[bytes] = []
    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(tx_gain=0.5), sent.append)
    pacer.tick()
    assert _rms(kv4p_audio.RxAudioDecoder().push(sent[0]).samples) < 50.0


def test_pacer_holds_a_sub_frame_until_it_completes():
    # A producer that enqueues less than a whole frame (Mumble delivers 20 ms / 960-sample frames)
    # must never cause a partial or double push: the remainder is held and the slot fills with
    # silence until a whole frame is available.
    _opus_or_skip()
    sent: list[bytes] = []
    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), sent.append)
    tone = _tone_frame(kv4p_audio.FRAME_SAMPLES).samples
    half = len(tone) // 2  # 960 samples

    pacer.enqueue(tone[:half])  # sub-frame: not enough for a slot
    pacer.tick()                # -> silence
    pacer.enqueue(tone[half:])  # completes the frame
    pacer.tick()                # -> exactly one real frame

    assert len(sent) == 2
    dec = kv4p_audio.RxAudioDecoder()
    assert _rms(dec.push(sent[0]).samples) < 50.0   # silence while the sub-frame was held
    assert _rms(dec.push(sent[1]).samples) > 100.0  # the completed real frame


def test_pacer_buffer_is_bounded_drop_oldest():
    # No Opus: enqueue() never encodes. A producer outpacing the drain is bounded by drop-oldest, so
    # latency stays capped; the newest audio is retained and the drop is counted (telemetry).
    frame = kv4p_audio.FRAME_BYTES
    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), lambda _p: None, max_buffer_bytes=frame * 2)
    a = b"\xaa\xaa" * kv4p_audio.FRAME_SAMPLES
    b = b"\xbb\xbb" * kv4p_audio.FRAME_SAMPLES
    c = b"\xcc\xcc" * kv4p_audio.FRAME_SAMPLES
    pacer.enqueue(a)
    pacer.enqueue(b)
    pacer.enqueue(c)  # overflows the 2-frame bound
    assert pacer.dropped_bytes == frame
    assert bytes(pacer._buf) == b + c  # oldest dropped, newest kept


def test_pacer_tick_swallows_a_credit_starved_timeout_and_survives():
    # A Kv4pTimeout from a credit-starved window must not propagate out of tick() (the thread must
    # not die with PTT asserted); the next slot simply tries again.
    _opus_or_skip()

    def timeout_send(_p):
        raise Kv4pTimeout("no window credit")

    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), timeout_send)
    pacer.tick()  # must not raise
    assert not pacer._stop.is_set()  # a timeout does not stop the pacer


def test_pacer_tick_stops_on_a_closed_transport():
    # Kv4pClosed means the transport is gone — stop pacing rather than spin on the error.
    _opus_or_skip()

    def closed_send(_p):
        raise Kv4pClosed("transport closed")

    pacer = _TxPacer(kv4p_audio.TxAudioEncoder(), closed_send)
    pacer.tick()
    assert pacer._stop.is_set()


def test_streaming_keyed_idle_emits_silence_end_to_end():
    # Through Kv4pHt: a held key with no transmit() still streams silence to the transport, so the
    # firmware is fed across a Mumble tx_hang pause. Freeze the auto thread, then drive ticks.
    _opus_or_skip()
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    radio._pacer.stop()  # join the auto thread so the count is deterministic
    before = len(fake.tx_audio)
    for _ in range(4):
        radio._pacer.tick()
    assert len(fake.tx_audio) == before + 4  # one silence frame per idle slot — no starve
    radio.ptt(False)
    radio.close()


def test_ptt_false_flushes_the_tail_and_drops_ptt_once():
    # Key-down stops the pacer, flushes the buffered remainder (never clipped), and drops PTT once.
    _opus_or_skip()
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    radio._pacer.stop()  # freeze the auto thread
    keyed_at = len(fake.sent)
    radio.transmit(a_frame(kv4p_audio.FRAME_SAMPLES // 2))  # enqueue a sub-frame (960 samples)
    tx_before = len(fake.tx_audio)
    radio.ptt(False)
    assert len(fake.tx_audio) == tx_before + 1  # the held partial shipped, zero-padded, on flush
    assert len(fake.sent) == keyed_at + 1       # exactly one desired-state reconcile: PTT off
    assert HostStateFlag.PTT_REQUESTED not in flags_of(fake.sent[-1])
    assert radio._pacer is None and radio._tx is None and radio._keyed is False
    radio.close()


def test_second_ptt_true_builds_a_fresh_pacer_and_resumes():
    # A second keying restarts cleanly: a fresh encoder + pacer, and silence flows again.
    _opus_or_skip()
    fake = FakeTransport(grant_tx=True)
    radio = make_radio(fake)
    radio.set_frequency(146_520_000)
    radio.ptt(True)
    first_pacer, first_tx = radio._pacer, radio._tx
    radio.ptt(False)
    assert radio._pacer is None and radio._tx is None

    radio.ptt(True)
    assert radio._pacer is not None and radio._pacer is not first_pacer
    assert radio._tx is not None and radio._tx is not first_tx
    radio._pacer.stop()
    before = len(fake.tx_audio)
    for _ in range(3):
        radio._pacer.tick()
    assert len(fake.tx_audio) == before + 3
    radio.ptt(False)
    radio.close()
