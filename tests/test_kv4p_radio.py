"""Unit tests for the ``Kv4pHt`` backend (ADR 0061, ADR 0063), fake-transport only.

No serial, no threads: a :class:`FakeTransport` stands in for :class:`Kv4pTransport` via the
``_transport`` seam. It records every ``HostDesiredState`` the backend sends and every TX-audio
packet, and synthesizes a ``DeviceState`` that echoes the last desired state — adding ``TX_ACTIVE``
when PTT was requested (and the fake is set to grant it) and ``SQUELCHED`` per its ``squelched``
flag — so keying and ``status()`` behave like a (cooperative) device without any hardware.
"""

from __future__ import annotations

from collections import deque

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
from radio_server.backends.kv4p.radio import _KV4P_CAPS, Kv4pHt, Kv4pKeyingError
from radio_server.backends.kv4p.transport import TxStats


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
        radio.transmit(a_frame())
    # Streaming transmits send audio only — no per-frame desired-state reconciles.
    assert len(fake.sent) == keyed_at
    assert len(fake.tx_audio) > 0
    radio.ptt(False)
    assert HostStateFlag.PTT_REQUESTED not in flags_of(fake.sent[-1])  # dropped once, at the end
    assert radio._keyed is False
    radio.close()


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
