"""AIOC doctor diagnostics (ADR 0029 bring-up): RX-level measurement + RF-mode safety refusal.

All hardware-free: `measure_rx_levels` is driven by a `MockRadio` scripted with known frames and an
injected clock (no real sleeps), and the RF modes (`--tx-tone`/`--key-test`) are checked only for
their refuse-when-unattended guard — actual keying is never exercised in pytest. The `--link`
entry selection (`_mumble_config`, ADR 0042/0052) is driven off temp default-path config/secrets
files via a chdir — no network, no pymumble.
"""

from __future__ import annotations

import os
import shutil
import wave

import numpy as np
import pytest

from radio_server.audio import AudioFrame, DtmfFramer, MultimonDtmfDecoder, synth_dtmf
from radio_server.audio.tone import synth_tone
from radio_server.backends import MockRadio
from radio_server.doctor import (
    classify_rx_level,
    collect_dtmf,
    measure_rx_levels,
    _key_test,
    _tx_tone,
)


class TickClock:
    """Incrementing clock: each call advances by ``step`` so a duration-bounded loop terminates."""

    def __init__(self, step: float = 0.01):
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        v = self._t
        self._t += self._step
        return v


class FakeDtmfDecoder:
    """Returns a scripted digit string per ``decode()`` call (one per chunk); '' once exhausted."""

    def __init__(self, per_chunk):
        self._chunks = list(per_chunk)
        self._i = 0

    def decode(self, frame) -> str:
        v = self._chunks[self._i] if self._i < len(self._chunks) else ""
        self._i += 1
        return v


class SeqClock:
    """A deterministic clock returning successive preset values (last value repeats forever)."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


_CFG = {
    "serial_port": "/dev/ttyACM0",
    "ptt_line": "dtr",
    "input_device": "All-In-One-Cable: USB",
    "output_device": "All-In-One-Cable: USB",
    "blocksize": 960,
}


# --- measure_rx_levels -------------------------------------------------------


def test_measure_rx_levels_summarizes_scripted_frames():
    tone = synth_tone(1000.0, 20.0)  # a loud (half-scale) 20 ms tone
    # A prime read (ADR 0122) consumes the first frame before the clock starts, so supply one extra:
    # prime eats tone[0], then start=0, three loop iterations see clock<1, the fourth sees 100 and
    # stops → exactly 3 frames measured.
    radio = MockRadio(supports_cat=False, rx_frames=[tone, tone, tone, tone])
    levels = measure_rx_levels(radio, seconds=1.0, clock=SeqClock([0, 0, 0, 0, 100]))
    assert levels.frames == 3
    assert levels.peak_block_rms > 5000  # a half-scale tone is far above any threshold
    assert levels.peak_sample > 15000
    # All three frames are identical, so the overall RMS matches the per-block RMS.
    assert levels.avg_rms == pytest.approx(levels.peak_block_rms, abs=1.0)


def test_measure_rx_levels_reports_silence_when_no_audio():
    radio = MockRadio(supports_cat=False)  # canned_rx is an empty frame — skipped
    levels = measure_rx_levels(radio, seconds=1.0, clock=SeqClock([0, 0, 100]))
    assert levels.frames == 0
    assert levels.avg_rms == 0.0
    assert levels.peak_sample == 0


def test_measure_rx_levels_skips_fully_silent_frames():
    # ADR 0084: the kv4p RX now returns a full-length SILENCE frame on idle (continuity), and true
    # inter-transmission silence is all-zeros too. Those carry no received-audio level, so the level
    # diagnostic must skip them — else they dilute the avg RMS and inflate the ADR-0070 frame-rate
    # (true-ADC-clock) estimate. Only real received audio is counted.
    tone = synth_tone(1000.0, 20.0)
    silence = AudioFrame(b"\x00\x00" * 960)  # a full-length zero frame (the RX continuity fill)
    # The leading tone is the prime read (ADR 0122); the measured window is [tone, silence, tone,
    # silence], of which the two silence frames are skipped → 2 counted.
    radio = MockRadio(supports_cat=False, rx_frames=[tone, tone, silence, tone, silence])
    levels = measure_rx_levels(radio, seconds=1.0, clock=SeqClock([0, 0, 0, 0, 0, 100]))
    assert levels.frames == 2  # the two silence frames were skipped, not counted
    assert levels.avg_rms == pytest.approx(levels.peak_block_rms, abs=1.0)  # not diluted by zeros


class _GapClockRadio:
    """A radio whose receive() drives a shared monotonic clock: each 20 ms frame advances it 20 ms,
    and the FIRST receive() (the lazy capture-stream open) adds a one-time spin-up gap — the bench's
    ~13-block stream-open latency. Proves measure_rx_levels excludes that gap from the rate (ADR 0122).
    """

    def __init__(self, *, frame, frame_seconds: float, startup_gap: float):
        self._frame = frame
        self._frame_seconds = frame_seconds
        self._startup_gap = startup_gap
        self.t = 0.0
        self._first = True

    def clock(self) -> float:
        return self.t

    def receive(self):
        if self._first:
            self.t += self._startup_gap  # the stream-open latency: real time, no device samples
            self._first = False
        self.t += self._frame_seconds
        return self._frame


def test_measure_rx_levels_excludes_stream_open_latency_from_the_rate():
    # Regression (ADR 0122): the capture stream's spin-up latency must not bias the true-rate estimate.
    # 260 ms one-time open gap (~13 × 20 ms blocks), then a true 20 ms/frame cadence at 48 kHz. The
    # prime read absorbs the gap before the stopwatch starts, so the measured rate lands on nominal.
    tone = synth_tone(1000.0, 20.0)  # 960 samples = 20 ms @ 48 kHz, non-silent (so it is counted)
    radio = _GapClockRadio(frame=tone, frame_seconds=0.02, startup_gap=0.26)
    levels = measure_rx_levels(radio, seconds=1.0, clock=radio.clock)
    measured = levels.total_samples / levels.elapsed
    assert measured == pytest.approx(48000, rel=0.02)
    # Without the prime fix the 260 ms gap would drag the measured rate below 40 kHz — this guards it.
    assert measured > 45000


# --- classify_rx_level (the recommendation branch) ---------------------------


@pytest.mark.parametrize(
    "peak, expected",
    [
        (10.0, "silent"),  # nothing arriving → volume/mixer problem
        (120.0, "gated"),  # arriving but under vad_on=500 → gated out
        (800.0, "ok"),  # above threshold → gate opens
    ],
)
def test_classify_rx_level(peak, expected):
    assert classify_rx_level(peak, vad_on=500.0) == expected


# --- RF modes refuse to run unattended ---------------------------------------


def test_tx_tone_refuses_non_interactive(monkeypatch):
    # CI set → refuse before constructing the radio or transmitting anything (RF safety).
    monkeypatch.setenv("CI", "1")
    assert _tx_tone(_CFG, seconds=2.0, freq=1000.0) == 2


def test_key_test_refuses_non_interactive(monkeypatch):
    monkeypatch.setenv("CI", "1")
    assert _key_test("/dev/ttyACM0", "dtr") == 2


# --- collect_dtmf (accumulate → decode → frame) ------------------------------


def test_collect_dtmf_accumulates_into_chunks_and_frames_entries():
    # A 960-byte frame per receive(); chunk_bytes=1920 → one decode fires every 2 frames. The fake
    # decoder yields the digits "1", "2", "3#" from successive chunks; the framer submits on "#".
    frame = AudioFrame(b"\x01\x02" * 480)
    radio = MockRadio(supports_cat=False, canned_rx=frame)  # receive() always returns this frame
    decoder = FakeDtmfDecoder(per_chunk=["1", "2", "3#"])
    framer = DtmfFramer(timeout=1000.0, clock=TickClock())
    raw, entries = collect_dtmf(
        radio, decoder, framer, seconds=0.2, chunk_bytes=1920, clock=TickClock()
    )
    assert "123" in raw
    assert entries == ["123"]


def test_collect_dtmf_silent_when_no_digits_decoded():
    radio = MockRadio(supports_cat=False, canned_rx=AudioFrame(b"\x00\x00" * 480))
    decoder = FakeDtmfDecoder(per_chunk=[])  # decodes nothing
    framer = DtmfFramer(timeout=1000.0, clock=TickClock())
    raw, entries = collect_dtmf(
        radio, decoder, framer, seconds=0.2, chunk_bytes=1920, clock=TickClock()
    )
    assert raw == ""
    assert entries == []


def test_collect_dtmf_dedups_a_held_tone_but_keeps_repeats_across_a_gap():
    frame = AudioFrame(b"\x01\x02" * 480)
    radio = MockRadio(supports_cat=False, canned_rx=frame)
    # "9" held across three chunks (no gap) → one press; then a silent chunk ("") = a gap; then "9"
    # again → a second, distinct press. Result: "99", not "9999".
    decoder = FakeDtmfDecoder(per_chunk=["9", "9", "9", "", "9"])
    framer = DtmfFramer(timeout=1000.0, clock=TickClock())
    raw, entries = collect_dtmf(
        radio, decoder, framer, seconds=0.16, chunk_bytes=1920, clock=TickClock()
    )
    assert raw == "99"
    assert entries == []


def test_collect_dtmf_dedup_off_keeps_every_detection():
    frame = AudioFrame(b"\x01\x02" * 480)
    radio = MockRadio(supports_cat=False, canned_rx=frame)
    decoder = FakeDtmfDecoder(per_chunk=["5", "5", "5"])
    framer = DtmfFramer(timeout=1000.0, clock=TickClock())
    raw, _ = collect_dtmf(
        radio, decoder, framer, seconds=0.12, chunk_bytes=1920, clock=TickClock(), dedup=False
    )
    assert raw.startswith("555")


# --- _mumble_config: the --link entry selection + password resolution (ADR 0042/0052) --------

_LINK_TOML = (
    '[[mumble.servers]]\nname = "Radio Server Demo"\nhost = "demo.example"\n'
    'password = "gate-code"\n'
    '[[mumble.servers]]\nname = "club_net"\nhost = "mumble.example"\npassword = "plain"\n'
    '[[mumble.servers]]\nname = "quiet"\nhost = "h3"\n'
)


def _link_setup(tmp_path, monkeypatch, secrets_toml: str | None = None):
    # The doctor reads the default ./radio.toml and ./radio-secrets.toml (relative paths) — point
    # the cwd at a temp dir so the scenario is hermetic. The secrets file must be 0600 to load.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "radio.toml").write_text(_LINK_TOML)
    if secrets_toml is not None:
        sec = tmp_path / "radio-secrets.toml"
        sec.write_text(secrets_toml)
        sec.chmod(0o600)


def test_mumble_config_matches_display_name_or_slug(tmp_path, monkeypatch):
    from radio_server.doctor import _mumble_config

    _link_setup(tmp_path, monkeypatch)
    # Either spelling diagnoses the same entry (ADR 0052) — the free-text name or its slug.
    for spelling in ("Radio Server Demo", "radio_server_demo"):
        cfg = _mumble_config(spelling)
        assert cfg["error"] is None
        assert cfg["name"] == "Radio Server Demo"
        assert cfg["host"] == "demo.example" and cfg["port"] == 64738


def test_mumble_config_unknown_entry_reports_the_configured_names(tmp_path, monkeypatch):
    from radio_server.doctor import _mumble_config

    _link_setup(tmp_path, monkeypatch)
    cfg = _mumble_config("nope")
    assert "unknown mumble entry 'nope'" in cfg["error"]
    assert "Radio Server Demo" in cfg["error"]  # the actionable list of what IS configured


def test_mumble_config_password_precedence(tmp_path, monkeypatch):
    # Same precedence as the live client factory: mumble_password_<slug> (secrets) overrides the
    # entry's plaintext field, which overrides "".
    from radio_server.doctor import _mumble_config

    _link_setup(
        tmp_path, monkeypatch, 'mumble_password_radio_server_demo = "secret-wins"\n'
    )
    assert _mumble_config("Radio Server Demo")["password"] == "secret-wins"
    assert _mumble_config("club_net")["password"] == "plain"  # no secret -> the entry's field
    assert _mumble_config("quiet")["password"] == ""  # neither -> passwordless connect


@pytest.mark.skipif(
    shutil.which("multimon-ng") is None, reason="multimon-ng not installed; real-decode check"
)
def test_collect_dtmf_real_multimon_round_trip():
    # A MockRadio serving synthesized DTMF tones, decoded by REAL multimon-ng via collect_dtmf.
    # chunk_bytes=1 → each 200 ms tone frame forms its own chunk and decodes cleanly.
    radio = MockRadio(supports_cat=False, rx_frames=[synth_dtmf(d, 200) for d in "5#"])
    raw, entries = collect_dtmf(
        radio,
        MultimonDtmfDecoder(),
        DtmfFramer(timeout=1000.0),
        seconds=1.0,
        chunk_bytes=1,
        clock=TickClock(),
    )
    assert "5" in raw
    assert entries == ["5"]


# --- kv4p backend: dispatch, connect probe, keying test (ADR 0061/0063) -------------------------

import argparse

from radio_server.backends.kv4p.frames import (
    DeviceState,
    DeviceStateError,
    DeviceStateFlag,
    Hello,
    Version,
)
from radio_server.doctor import (
    analyze_dtmf_windows,
    format_dtmf_analysis,
    _analyze_wav,
    _build_backend,
    _check_kv4p_serial,
    _doctor_settings,
    _format_kv4p_rx_rate,
    _format_tx_stats,
    _kv4p_connect_probe,
    _kv4p_key_test,
    _kv4p_keying_core,
    _read_wav_mono16,
    _resolve_doctor_backend,
    _Report,
    _rx_capture,
    _run_kv4p,
    _sniff_pre_kiss_firmware,
    _tx_tone,
    _write_wav_mono16,
)
from radio_server.backends.kv4p.transport import TxStats

from .conftest import make_settings
from .test_kv4p_radio import FakeTransport, make_radio

_KV4P_CFG = {
    "backend": "kv4p",
    "serial_port": "/dev/ttyUSB0",
    "module_type": "uhf",
    "squelch": 4,
    "tx_lead_seconds": 0.2,
    "high_power": True,
    "tx_allowed": True,
    "frequency": 146520000,
    "sample_rate_correction": 1.02,
    "tx_gain": 1.0,
}


def _stub_create_radio(monkeypatch) -> dict:
    """Record the (backend, kwargs) _build_backend passes, without opening a real device."""
    calls: dict = {}

    def stub(backend, **kwargs):
        calls["backend"] = backend
        calls["kwargs"] = kwargs
        return MockRadio(supports_cat=(backend != "baofeng"))

    monkeypatch.setattr("radio_server.doctor.create_radio", stub)
    return calls


# --- backend dispatch --------------------------------------------------------


def test_build_backend_kv4p_threads_every_setting(monkeypatch):
    calls = _stub_create_radio(monkeypatch)
    _build_backend(_KV4P_CFG)
    assert calls["backend"] == "kv4p"
    assert calls["kwargs"] == {
        "serial_port": "/dev/ttyUSB0",
        "module_type": "uhf",
        "squelch": 4,
        "tx_lead_seconds": 0.2,
        "high_power": True,
        "tx_allowed": True,
        "frequency": 146520000,
        "sample_rate_correction": 1.02,
        "tx_gain": 1.0,
    }


def test_build_backend_baofeng_unchanged(monkeypatch):
    calls = _stub_create_radio(monkeypatch)
    _build_backend({**_CFG, "backend": "baofeng"})
    assert calls["backend"] == "baofeng"
    assert calls["kwargs"] == {
        "serial_port": "/dev/ttyACM0",
        "ptt_line": "dtr",
        "input_device": "All-In-One-Cable: USB",
        "output_device": "All-In-One-Cable: USB",
        "blocksize": 960,
    }


def test_build_backend_unknown_raises():
    with pytest.raises(ValueError, match="unsupported backend"):
        _build_backend({"backend": "nope"})


def test_resolve_backend_flag_overrides_server_backend():
    # --backend wins outright — no settings are even consulted.
    assert _resolve_doctor_backend(argparse.Namespace(backend="kv4p")) == "kv4p"
    assert _resolve_doctor_backend(argparse.Namespace(backend="baofeng")) == "baofeng"


def test_resolve_backend_reads_server_backend_kv4p(monkeypatch):
    # doctor now reads DEFAULT_CONFIG_PATH (ADR 0069), so the stub takes the path argument.
    monkeypatch.setattr(
        "radio_server.config.load_settings", lambda *a, **k: make_settings({"server.backend": "kv4p"})
    )
    assert _resolve_doctor_backend(argparse.Namespace(backend=None)) == "kv4p"


def test_resolve_backend_defaults_to_baofeng(monkeypatch):
    # The schema default is 'mock', and any non-kv4p value falls back to the AIOC checks (unchanged).
    monkeypatch.setattr("radio_server.config.load_settings", lambda *a, **k: make_settings({}))
    assert _resolve_doctor_backend(argparse.Namespace(backend=None)) == "baofeng"


def test_doctor_settings_reads_the_config_file_not_pure_defaults(monkeypatch):
    # Regression (ADR 0069): doctor called load_settings() with no path → pure defaults, silently
    # ignoring radio.toml and pointing --key-test at the default serial port/band. It must pass the path.
    from radio_server.config import DEFAULT_CONFIG_PATH

    seen: dict = {}
    monkeypatch.setattr(
        "radio_server.config.load_settings",
        lambda path=None, *a, **k: seen.update(path=path) or make_settings({}),
    )
    _doctor_settings()
    assert seen["path"] == DEFAULT_CONFIG_PATH and seen["path"] is not None


# --- connect probe -----------------------------------------------------------


def _device_state(**overrides) -> DeviceState:
    base = dict(
        applied_sequence=7,
        memory_id=0,
        flags=0,
        bw=0,
        freq_tx=146.52,
        freq_rx=146.52,
        ctcss_tx=0,
        squelch=4,
        ctcss_rx=0,
        radio_module_status=0,
        mode=1,  # DeviceMode.RX
        last_error=0,
        latest_rssi=90,
    )
    base.update(overrides)
    return DeviceState(**base)


class _ProbeTransport:
    """Minimal Kv4pTransport stand-in for the connect probe (connect/hello/device_state/close)."""

    def __init__(self, *, state, hello=None, window_size=2048, connect_exc=None):
        self._state = state
        self._hello = hello
        self._window_size = window_size
        self._connect_exc = connect_exc
        self.closed = False

    def connect(self, timeout=2.0):
        if self._connect_exc is not None:
            raise self._connect_exc
        return self._state

    @property
    def hello(self):
        return self._hello

    @property
    def device_state(self):
        return self._state

    @property
    def window_size(self):
        return self._window_size

    def close(self):
        self.closed = True


def test_connect_probe_with_hello_passes(capsys):
    report = _Report()
    version = Version(
        ver=1,
        radio_module_status=0,
        window_size=2048,
        rf_module_type=1,  # SA818_UHF — matches _KV4P_CFG's module_type="uhf" (no band mismatch)
        min_radio_freq=400.0,
        max_radio_freq=480.0,
        features=3,
    )
    state = _device_state(
        flags=int(DeviceStateFlag.TX_ALLOWED | DeviceStateFlag.RADIO_CONFIG_VALID)
    )
    fake = _ProbeTransport(state=state, hello=Hello(version=version, device_state=state))
    _kv4p_connect_probe(report, _KV4P_CFG, transport=fake)
    out = capsys.readouterr().out
    assert report.ok
    assert "HELLO received" in out and "SA818_UHF" in out
    assert "band mismatch" not in out  # reported band matches the configured band
    assert "TX_ALLOWED set" in out and "RADIO_CONFIG_VALID set" in out
    assert not fake.closed  # an injected transport is not owned/closed by the probe


def test_connect_probe_without_hello_is_informational_not_fail(capsys):
    report = _Report()
    state = _device_state(
        flags=int(DeviceStateFlag.TX_ALLOWED | DeviceStateFlag.RADIO_CONFIG_VALID)
    )
    _kv4p_connect_probe(report, _KV4P_CFG, transport=_ProbeTransport(state=state, hello=None))
    out = capsys.readouterr().out
    assert report.ok  # HELLO fires only at ESP32 boot (ADR 0062) — absent is a WARN, not a FAIL
    assert "no HELLO" in out


def test_connect_probe_surfaces_device_last_error(capsys):
    report = _Report()
    state = _device_state(
        last_error=int(DeviceStateError.RADIO_CONFIG_FAILED),
        flags=int(DeviceStateFlag.TX_ALLOWED),
    )
    _kv4p_connect_probe(report, _KV4P_CFG, transport=_ProbeTransport(state=state))
    out = capsys.readouterr().out
    assert not report.ok  # a non-NONE lastError must FAIL loudly, never a silent pass
    assert "RADIO_CONFIG_FAILED" in out


def test_connect_probe_missing_extra_degrades(monkeypatch, capsys):
    # transport=None path: Kv4pTransport construction raises RuntimeError (no hardware extra) → FAIL.
    def boom(**kwargs):
        raise RuntimeError("the kv4p backend needs the 'hardware' extra")

    monkeypatch.setattr("radio_server.backends.kv4p.transport.Kv4pTransport", boom)
    report = _Report()
    _kv4p_connect_probe(report, _KV4P_CFG)
    out = capsys.readouterr().out
    assert not report.ok
    assert "cannot open the kv4p transport" in out


# --- A2: wrong/missing hwconfig NVS -> band mismatch WARN ---------------------


def _hello(rf_module_type: int) -> Hello:
    version = Version(
        ver=1,
        radio_module_status=0,
        window_size=2048,
        rf_module_type=rf_module_type,
        min_radio_freq=134.0 if rf_module_type == 0 else 400.0,
        max_radio_freq=174.0 if rf_module_type == 0 else 480.0,
        features=3,
    )
    state = _device_state(flags=int(DeviceStateFlag.RADIO_CONFIG_VALID))
    return Hello(version=version, device_state=state)


def test_connect_probe_warns_on_band_mismatch(capsys):
    # The board reports VHF (rf_module_type=0) but _KV4P_CFG configures "uhf" -> the hwconfig NVS is
    # probably missing/wrong (ADR 0068). A WARN, not a FAIL (the HELLO still parsed fine).
    report = _Report()
    hello = _hello(rf_module_type=0)  # SA818_VHF
    _kv4p_connect_probe(
        report, _KV4P_CFG, transport=_ProbeTransport(state=hello.device_state, hello=hello)
    )
    out = capsys.readouterr().out
    assert report.ok  # a WARN does not flip report.ok
    assert "band mismatch" in out
    assert "SA818_VHF" in out and "SA818_UHF" in out  # reports VHF, configured UHF
    assert "hwconfig NVS" in out


def test_connect_probe_no_band_mismatch_when_bands_agree(capsys):
    report = _Report()
    hello = _hello(rf_module_type=1)  # SA818_UHF, matches _KV4P_CFG "uhf"
    _kv4p_connect_probe(
        report, _KV4P_CFG, transport=_ProbeTransport(state=hello.device_state, hello=hello)
    )
    assert "band mismatch" not in capsys.readouterr().out


# --- A1: pre-KISS firmware sniff ----------------------------------------------


class _FakeSniffSerial:
    """A pyserial-like stand-in: hands out ``data`` once, then EOF; records close()."""

    def __init__(self, data: bytes):
        self._chunks = [data]
        self.closed = False

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


def test_sniff_detects_pre_kiss_delimiter():
    # de ad be ef present, no KISS FEND (0xC0), no KV4P prefix -> pre-KISS board.
    data = b"boot\r\n\xde\xad\xbe\xef\x01\x02 status \xde\xad\xbe\xef"
    fake = _FakeSniffSerial(data)
    assert _sniff_pre_kiss_firmware("/dev/ttyUSB0", _open=lambda: fake) is True
    assert fake.closed  # the sniffer always closes the port it opened


def test_sniff_negative_when_kiss_fend_present():
    # A KISS FEND anywhere means this is (or could be) a KISS board -> not pre-KISS, stay inconclusive.
    data = b"\xde\xad\xbe\xef\xc0\x06KV4P"  # delimiter present but so is FEND + KV4P
    assert _sniff_pre_kiss_firmware("/dev/ttyUSB0", _open=lambda: _FakeSniffSerial(data)) is False


def test_sniff_negative_when_kv4p_prefix_present():
    data = b"\xde\xad\xbe\xef ...KV4P..."  # delimiter + KV4P prefix, no FEND -> still not pre-KISS
    assert _sniff_pre_kiss_firmware("/dev/ttyUSB0", _open=lambda: _FakeSniffSerial(data)) is False


def test_sniff_negative_when_no_delimiter():
    assert _sniff_pre_kiss_firmware("/dev/ttyUSB0", _open=lambda: _FakeSniffSerial(b"junk")) is False


def test_sniff_false_when_port_cannot_open():
    def boom():
        raise OSError("device gone")

    assert _sniff_pre_kiss_firmware("/dev/ttyUSB0", _open=boom) is False


def test_connect_probe_pre_kiss_firmware_line(capsys):
    # connect() times out AND the sniff hits -> the actionable pre-KISS FAIL, not the generic one.
    report = _Report()
    fake = _ProbeTransport(state=None, connect_exc=Exception("no ack within 2.0s"))
    _kv4p_connect_probe(
        report, _KV4P_CFG, transport=fake, sniff=lambda port, **kw: True
    )
    out = capsys.readouterr().out
    assert not report.ok
    assert "pre-KISS firmware" in out and "flash v17" in out
    assert "docs/kv4p-setup.md" in out


def test_connect_probe_generic_failure_when_sniff_inconclusive(capsys):
    report = _Report()
    fake = _ProbeTransport(state=None, connect_exc=Exception("no ack within 2.0s"))
    _kv4p_connect_probe(
        report, _KV4P_CFG, transport=fake, sniff=lambda port, **kw: False
    )
    out = capsys.readouterr().out
    assert not report.ok
    assert "no response to the connect handshake" in out
    assert "pre-KISS" not in out


def test_check_kv4p_serial_missing_device_fails(capsys):
    report = _Report()
    _check_kv4p_serial(report, "/dev/does-not-exist-kv4p-xyz")
    out = capsys.readouterr().out
    assert not report.ok
    assert "serial device missing" in out


# --- keying test -------------------------------------------------------------


def test_kv4p_keying_core_passes_when_device_reports_tx_active(capsys):
    fake = FakeTransport(grant_tx=True)
    rc = _kv4p_keying_core(make_radio(fake), seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "TX_ACTIVE confirmed" in out and "unkeyed cleanly" in out
    assert fake.closed


def test_kv4p_keying_core_fails_loudly_when_gate_withholds_tx(capsys):
    fake = FakeTransport(grant_tx=False)  # TX_ALLOWED gate off → ptt(True) raises Kv4pKeyingError
    rc = _kv4p_keying_core(make_radio(fake), seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 1  # a withheld key is a loud FAIL, never reported as success
    assert "REFUSED" in out
    assert "TX_ACTIVE confirmed" not in out
    assert fake.closed


def test_kv4p_key_test_refuses_non_interactive(monkeypatch):
    monkeypatch.setenv("CI", "1")
    # Returns 2 before building any radio (RF safety) — same guard as the baofeng --key-test.
    assert _kv4p_key_test(_KV4P_CFG) == 2


def test_kv4p_key_test_points_at_the_probe_on_open_failure(monkeypatch, capsys):
    # The first-connect race (ADR 0066/0069) surfaced here as a raw open failure with no next step.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("radio_server.doctor.sys.stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda *a: "CONFIRM")

    def boom(cfg):
        raise RuntimeError("the device never acknowledged a host frame within 2.0s")

    monkeypatch.setattr("radio_server.doctor._build_backend", boom)
    rc = _kv4p_key_test({**_KV4P_CFG})
    err = capsys.readouterr().err
    assert rc == 1
    assert "connect probe first" in err and "--backend kv4p" in err


def test_kv4p_keying_core_reports_key_up_latency(capsys):
    fake = FakeTransport(grant_tx=True)
    # Injected clock: t0=1.0, TX_ACTIVE seen at 1.05 → 50 ms; start=2.0; the seconds=0 hold exits at once.
    ticks = iter([1.0, 1.05, 2.0, 2.0])
    rc = _kv4p_keying_core(make_radio(fake), seconds=0.0, clock=lambda: next(ticks))
    out = capsys.readouterr().out
    assert rc == 0
    assert "TX_ACTIVE confirmed" in out and "keyed in 50 ms" in out


# --- TX telemetry (ADR 0069) -------------------------------------------------


def test_format_tx_stats_reports_bytes_and_a_clean_window():
    stats = TxStats(
        frames=25, opus_bytes_sum=1000, opus_bytes_min=30, opus_bytes_max=52,
        wire_bytes_sum=1225, blocked_frames=0, min_credits=1800,
    )
    lines = "\n".join(_format_tx_stats(stats, 2048))
    assert "25 Opus frames" in lines
    assert "min 30" in lines and "max 52" in lines and "mean 40.0" in lines  # 1000/25
    assert "frames per 2048-byte window" in lines
    assert "never blocked" in lines and "min credits 1800" in lines


def test_format_tx_stats_flags_a_blocked_window():
    stats = TxStats(
        frames=25, opus_bytes_sum=1000, opus_bytes_min=30, opus_bytes_max=52,
        wire_bytes_sum=1225, blocked_frames=3, min_credits=0,
    )
    lines = "\n".join(_format_tx_stats(stats, 2048))
    assert "window blocked on 3 frame(s)" in lines and "min credits 0" in lines
    assert "backpressure" in lines  # framed as pacing, not "raise the window" (device buffer is fixed)


def test_format_tx_stats_handles_no_frames():
    assert _format_tx_stats(TxStats(), 2048) == [
        "TX telemetry: no audio frames were sent (nothing to measure)."
    ]


# --- RX sample-rate estimate (ADR 0070) --------------------------------------


def test_format_kv4p_rx_rate_reports_the_true_device_rate():
    # 25.5 frames/s over a 30 s window → 25.5 × 1920 = 48960 Hz = 1.02 × 48000, the firmware offset.
    lines = "\n".join(_format_kv4p_rx_rate(frames=765, elapsed=30.0, correction=1.02))
    assert "25.50 frames/s" in lines
    assert "48,960 Hz" in lines  # fps × 1920, the true ADC clock
    assert "1.0200" in lines  # implied correction = rate / 48000
    assert "matches the value in effect" in lines


def test_format_kv4p_rx_rate_flags_a_mismatch_with_the_configured_value():
    # Device measures ~1.019 but config still says 1.02 → tell the operator the value to set.
    lines = "\n".join(_format_kv4p_rx_rate(frames=764, elapsed=30.0, correction=1.00))
    assert "set kv4p.sample_rate_correction" in lines


def test_format_kv4p_rx_rate_warns_on_a_too_short_window():
    lines = "\n".join(_format_kv4p_rx_rate(frames=128, elapsed=5.0, correction=1.02))
    assert "short window" in lines and "--seconds 30" in lines


def test_format_kv4p_rx_rate_needs_enough_frames():
    assert _format_kv4p_rx_rate(frames=1, elapsed=30.0, correction=1.02) == [
        "  RX frame rate   : too few frames to estimate the true sample rate"
    ]


class _ToneRadio:
    """Minimal kv4p-shaped radio for driving _tx_tone without hardware."""

    def __init__(self, stats: TxStats) -> None:
        self._stats = stats
        self.closed = False
        self.transmitted = None

    def transmit(self, audio) -> None:
        self.transmitted = audio

    @property
    def tx_stats(self) -> TxStats:
        return self._stats

    @property
    def window_size(self) -> int:
        return 2048

    def close(self) -> None:
        self.closed = True


class _FakeTTY:
    def isatty(self) -> bool:
        return True


def test_tx_tone_kv4p_prints_telemetry_and_a_non_alsamixer_hint(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("radio_server.doctor.sys.stdin", _FakeTTY())
    stats = TxStats(
        frames=25, opus_bytes_sum=1000, opus_bytes_min=30, opus_bytes_max=52,
        wire_bytes_sum=1225, blocked_frames=0, min_credits=1800,
    )
    radio = _ToneRadio(stats)
    monkeypatch.setattr("radio_server.doctor._build_backend", lambda cfg: radio)
    answers = iter(["CONFIRM", "n"])  # proceed past the guard, then "no tone heard"
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    rc = _tx_tone({**_KV4P_CFG}, seconds=1.0, freq=1000.0)
    out = capsys.readouterr().out
    assert "TX telemetry (25 Opus frames" in out  # the measurement rig printed
    assert "alsamixer" not in out  # the stale AIOC-only hint must not appear on the kv4p path
    assert "TX_ALLOWED gate" in out  # kv4p-specific "no tone heard" guidance instead
    assert rc == 1 and radio.closed and radio.transmitted is not None


# --- --tx-lead sweep override ------------------------------------------------


def test_tx_lead_override_flows_into_the_backend_config(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("radio_server.doctor._kv4p_config", lambda: dict(_KV4P_CFG))
    monkeypatch.setattr("radio_server.doctor._rx_level", lambda cfg, seconds: seen.update(cfg) or 0)

    def run(tx_lead):
        seen.clear()
        args = argparse.Namespace(
            serial_port=None, module_type=None, tx_lead=tx_lead,
            key_test=False, rx_level=True, tx_tone=False, dtmf=False,
            seconds=0.1, freq=1000.0,
        )
        assert _run_kv4p(args) == 0
        return seen["tx_lead_seconds"]

    assert run(0.05) == 0.05  # a swept value overrides the configured default
    assert run(0.0) == 0.0  # 0 (disable the lead) is honored, not treated as "unset"
    assert run(None) == _KV4P_CFG["tx_lead_seconds"]  # no flag → keep the configured value


# --- RX capture + direct WAV DTMF analysis (ADR 0071) ------------------------

_RATE = 48000


def _dtmf_code_pcm(digits: str, tone_ms: float = 150.0, gap_ms: float = 80.0) -> np.ndarray:
    """A clean int16 DTMF sequence (tone / silence / tone …) at the canonical 48 kHz."""
    parts: list[np.ndarray] = []
    for d in digits:
        parts.append(np.frombuffer(synth_dtmf(d, tone_ms).samples, dtype="<i2"))
        parts.append(np.zeros(int(_RATE * gap_ms / 1000), dtype="<i2"))
    return np.concatenate(parts)


def test_analyze_reads_clean_tones_and_maps_digits_verdict3():
    windows = analyze_dtmf_windows(_dtmf_code_pcm("1234#"), _RATE)
    mapped = {w.digit for w in windows if w.digit}
    assert {"1", "2", "3", "4", "#"} <= mapped  # every keyed digit recovered from the spectrum
    report = "\n".join(format_dtmf_analysis(windows, 1.0))
    assert "VERDICT (3)" in report  # present, on-frequency, not clipping → decode-path wiring


def test_analyze_flags_clipping_as_upstream_firmware_gain_verdict1():
    # An 8x boost saturates the dual-tone the way the firmware's Boost(16.0) would on a strong signal.
    clipped = np.clip(_dtmf_code_pcm("1234#").astype(np.float64) * 8, -32768, 32767).astype("<i2")
    report = "\n".join(format_dtmf_analysis(analyze_dtmf_windows(clipped, _RATE), 1.0))
    assert "VERDICT (1)" in report and "CLIPPING" in report and "16x" in report


def test_analyze_flags_off_frequency_tones_verdict2():
    n = int(_RATE * 0.15)
    t = np.arange(n) / _RATE
    parts: list[np.ndarray] = []
    for lo, hi in [(697.0, 1209.0), (770.0, 1336.0)]:  # synthesized 2% high → residual after 1.0
        s = (0.4 * np.sin(2 * np.pi * lo * 1.02 * t) + 0.4 * np.sin(2 * np.pi * hi * 1.02 * t)) * 32767
        parts.append(s.astype("<i2"))
        parts.append(np.zeros(int(_RATE * 0.08), dtype="<i2"))
    report = "\n".join(format_dtmf_analysis(analyze_dtmf_windows(np.concatenate(parts), _RATE), 1.0))
    assert "VERDICT (2)" in report and "OFF-FREQUENCY" in report


def test_analyze_reports_no_signal_on_silence():
    report = "\n".join(format_dtmf_analysis(analyze_dtmf_windows(np.zeros(_RATE, dtype="<i2"), _RATE), 1.0))
    assert "no window carried a signal" in report


def test_analyze_handles_an_empty_capture():
    assert format_dtmf_analysis([], 1.0) == ["DTMF analysis: no audio to analyze (empty capture)."]


def test_wav_roundtrips_mono16(tmp_path):
    pcm = _dtmf_code_pcm("5").tobytes()
    path = str(tmp_path / "cap.wav")
    _write_wav_mono16(path, pcm)
    samples, rate = _read_wav_mono16(path)
    assert rate == _RATE and samples.tobytes() == pcm


def test_read_wav_rejects_non_mono16(tmp_path):
    path = str(tmp_path / "stereo.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(b"\x00\x00\x00\x00")
    with pytest.raises(RuntimeError, match="mono"):
        _read_wav_mono16(path)


def test_analyze_wav_reads_a_file_and_prints_a_verdict(tmp_path, capsys):
    path = str(tmp_path / "clean.wav")
    _write_wav_mono16(path, _dtmf_code_pcm("1234#").tobytes())
    assert _analyze_wav(path) == 0
    assert "VERDICT" in capsys.readouterr().out


def test_analyze_wav_missing_file_fails_cleanly(capsys):
    assert _analyze_wav("/nonexistent/nope.wav") == 1
    assert "could not read" in capsys.readouterr().err


def test_rx_capture_writes_a_wav_and_analyzes_it(tmp_path, monkeypatch, capsys):
    frames: list[AudioFrame] = []
    for d in "1234#":
        frames.append(synth_dtmf(d, 150))
        frames.append(AudioFrame(b"\x00\x00" * int(_RATE * 0.08)))
    radio = MockRadio(supports_cat=True, rx_frames=frames)
    monkeypatch.setattr("radio_server.doctor._build_backend", lambda cfg: radio)
    out = str(tmp_path / "cap.wav")
    rc = _rx_capture(
        {"backend": "kv4p", "sample_rate_correction": 1.0},
        seconds=1.0, out_path=out, clock=SeqClock([0] * 20 + [100]),
    )
    assert rc == 0
    assert os.path.exists(out)
    assert "VERDICT" in capsys.readouterr().out


def test_rx_capture_fails_cleanly_with_no_audio(tmp_path, monkeypatch, capsys):
    radio = MockRadio(supports_cat=True)  # empty canned_rx → nothing to capture
    monkeypatch.setattr("radio_server.doctor._build_backend", lambda cfg: radio)
    out = str(tmp_path / "none.wav")
    rc = _rx_capture(
        {"backend": "kv4p", "sample_rate_correction": 1.0},
        seconds=0.5, out_path=out, clock=SeqClock([0, 0, 0, 100]),
    )
    assert rc == 1
    assert "no RX audio" in capsys.readouterr().err
    assert not os.path.exists(out)


# --- DV Dongle vocoder loopback self-test (ADR 0086) --------------------------------------------

from radio_server.doctor import (
    STAIRCASE_TONES_HZ,
    LatencyMetrics,
    VocoderMetrics,
    _synth_staircase_pcm,
    latency_metrics,
    main as doctor_main,
    staircase_pitch_metrics,
)
from radio_server.vocoder.base import PCM_BYTES_PER_FRAME, PCM_FORMAT


def test_synth_staircase_pcm_is_whole_frames_one_per_tone():
    from radio_server.doctor import _STAIRCASE_STEP_FRAMES

    pcm = _synth_staircase_pcm()
    assert len(pcm) % PCM_BYTES_PER_FRAME == 0
    assert len(pcm) // PCM_BYTES_PER_FRAME == len(STAIRCASE_TONES_HZ) * _STAIRCASE_STEP_FRAMES
    assert staircase_pitch_metrics(pcm, pcm).rms_in > 1000  # tones, not silence


def test_staircase_metrics_identity_tracks_perfectly():
    # A lossless "vocoder" (output == input): each step's pitch matches, so correlation is 1.
    pcm = _synth_staircase_pcm()
    m = staircase_pitch_metrics(pcm, pcm)
    assert isinstance(m, VocoderMetrics)
    assert m.frames == len(STAIRCASE_TONES_HZ) * 18
    assert len(m.steps) == len(STAIRCASE_TONES_HZ)
    assert m.pitch_correlation == pytest.approx(1.0)
    assert m.lag_frames == 0  # identical in/out => best alignment is zero lag
    assert m.median_pitch_err_hz == pytest.approx(0.0, abs=1.0)
    assert m.ratio == pytest.approx(1.0)
    # Each measured input step is near its intended tone.
    for (hz_in, hz_out, _), tone in zip(m.steps, STAIRCASE_TONES_HZ):
        assert hz_in == pytest.approx(tone, abs=40.0)
        assert hz_out == pytest.approx(tone, abs=40.0)


def test_latency_metrics_recovers_the_pipeline_latency():
    # ADR 0098 fast-follow: a per-frame decode of [prime silence][marker tone][flush silence] returns
    # the marker L frames late (the AMBE2000 pipeline depth). latency_metrics must recover L = onset -
    # prime from where the decoded marker first rises above silence.
    from radio_server.audio.tone import synth_tone

    prime, latency, marker, flush = 25, 5, 15, 25
    silence = bytes(PCM_BYTES_PER_FRAME)
    tone = synth_tone(1000.0, 20.0, PCM_FORMAT, ramp_ms=0.0).samples  # one 20 ms 1 kHz frame
    out = silence * (prime + latency) + tone * marker + silence * (flush - latency)

    m = latency_metrics(out, prime)
    assert isinstance(m, LatencyMetrics)
    assert m.ok
    assert m.latency_frames == latency
    assert m.onset_frame == prime + latency
    assert m.marker_hz == pytest.approx(1000.0, abs=60.0)  # confirmed as the tone, not noise
    from radio_server.doctor import _LATENCY_ONSET_RMS

    assert m.marker_rms > _LATENCY_ONSET_RMS  # well above the silence floor


def test_latency_metrics_zero_latency():
    # A pipeline with no lag: the marker emerges exactly at the prime boundary → L = 0.
    from radio_server.audio.tone import synth_tone

    prime = 10
    silence = bytes(PCM_BYTES_PER_FRAME)
    tone = synth_tone(1000.0, 20.0, PCM_FORMAT, ramp_ms=0.0).samples
    out = silence * prime + tone * 12 + silence * 12
    m = latency_metrics(out, prime)
    assert m.ok and m.latency_frames == 0 and m.onset_frame == prime


def test_latency_metrics_no_marker_is_not_ok():
    # An all-silent decode (NULL_AMBE → silence, or a wedged/deaf dongle) yields no onset: not ok.
    m = latency_metrics(bytes(PCM_BYTES_PER_FRAME) * 40, 25)
    assert not m.ok
    assert m.latency_frames is None and m.onset_frame is None


def test_staircase_metrics_recovers_a_delayed_roundtrip():
    # The real chip returns the stream after a constant, session-varying latency. A delayed-but-intact
    # round-trip must still score ~1.0 once the metric finds the lag (leading silence == startup fill).
    staircase = _synth_staircase_pcm()
    delay = 12
    delayed = bytes(delay * PCM_BYTES_PER_FRAME) + staircase
    # Without lag search a whole-step delay scrambles the fixed windows...
    unaligned = staircase_pitch_metrics(staircase, delayed, max_lag_frames=0)
    assert unaligned.pitch_correlation < 0.8
    # ...but searching the constant lag recovers the intact round-trip.
    aligned = staircase_pitch_metrics(staircase, delayed)
    assert aligned.pitch_correlation == pytest.approx(1.0)
    assert aligned.lag_frames >= 1


def test_staircase_metrics_fixed_buzz_does_not_track():
    # A constant-tone output (the classic broken-vocoder "buzz") has no per-step pitch variation, so
    # correlation is 0 — well below the loopback's 0.8 pass threshold — even though it is not silent.
    in_pcm = _synth_staircase_pcm()
    buzz = _synth_staircase_pcm(tones=(700.0,) * len(STAIRCASE_TONES_HZ))
    m = staircase_pitch_metrics(in_pcm, buzz)
    assert m.rms_out > 1000  # present, not silence
    assert m.pitch_correlation < 0.8


def test_staircase_metrics_silent_output():
    in_pcm = _synth_staircase_pcm()
    m = staircase_pitch_metrics(in_pcm, bytes(len(in_pcm)))  # decoded silence
    assert m.rms_out == 0.0
    assert m.ratio == 0.0
    assert m.pitch_correlation < 0.8


def test_vocoder_loopback_fails_loud_without_a_dongle(tmp_path, capsys):
    # No hardware: opening the (bogus) port fails, so the handler must report FAIL and exit 1 —
    # exercises the argparse wiring and the VocoderUnavailable path end to end.
    out = str(tmp_path / "loop.wav")
    rc = doctor_main(["--vocoder-loopback", "--vocoder-port", str(tmp_path / "no-such-tty"), "--out", out])
    assert rc == 1
    assert "[FAIL]" in capsys.readouterr().err
    assert not os.path.exists(out)


# =======================================================================================
# UV-K5 (Quansheng Dock) backend doctor wiring (ADR 0114)
# =======================================================================================

from radio_server.backends.uvk5.transport import Uvk5Timeout, Uvk5Transport
from radio_server.doctor import (
    _check_uvk5_serial,
    _format_soundcard_rx_rate,
    _run_uvk5,
    _uvk5_config,
    _uvk5_connect_probe,
    _uvk5_hello_probe,
    _uvk5_key_test,
    _uvk5_keying_core,
)
from tests.test_uvk5_radio import make_radio as make_uvk5_radio
from tests.test_uvk5_transport import FakeSerial, FirmwareFakeSerial

_UVK5_CFG = {
    "backend": "uvk5",
    "serial_port": "/dev/ttyACM0",
    "frequency": 145_500_000,
    "tone": None,
    "mode": "FM",
    "tx_allowed": True,
    "input_device": "All-In-One-Cable: USB",
    "output_device": "All-In-One-Cable: USB",
    "blocksize": 960,
    "tx_lead_seconds": 0.0,
    "squelch_threshold": 40,
}


class _UvkProbeTransport:
    """Stub Uvk5Transport for the connect probe: connect() returns, or raises the given exc."""

    def __init__(self, connect_exc=None):
        self._exc = connect_exc
        self.closed = False

    def connect(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return None

    def close(self):
        self.closed = True


# --- routing / dispatch ------------------------------------------------------


def test_build_backend_uvk5_threads_every_setting(monkeypatch):
    calls = _stub_create_radio(monkeypatch)
    _build_backend(_UVK5_CFG)
    assert calls["backend"] == "uvk5"
    assert calls["kwargs"] == {
        "serial_port": "/dev/ttyACM0",
        "frequency": 145_500_000,
        "tone": None,
        "mode": "FM",
        "tx_allowed": True,
        "input_device": "All-In-One-Cable: USB",
        "output_device": "All-In-One-Cable: USB",
        "blocksize": 960,
        "tx_lead_seconds": 0.0,
        "squelch_threshold": 40,
    }


def test_resolve_backend_reads_server_backend_uvk5(monkeypatch):
    monkeypatch.setattr(
        "radio_server.config.load_settings", lambda *a, **k: make_settings({"server.backend": "uvk5"})
    )
    assert _resolve_doctor_backend(argparse.Namespace(backend=None)) == "uvk5"


def test_resolve_backend_flag_overrides_to_uvk5():
    assert _resolve_doctor_backend(argparse.Namespace(backend="uvk5")) == "uvk5"


def test_uvk5_config_honours_radio_toml(monkeypatch):
    settings = make_settings({
        "server.backend": "uvk5",
        "uvk5.serial_port": "/dev/serial/by-id/usb-AIOC",
        "uvk5.frequency": 442_000_000,
        "uvk5.tone": 100.0,
        "uvk5.mode": "NFM",
        "uvk5.tx_allowed": False,
    })
    monkeypatch.setattr("radio_server.config.load_settings", lambda *a, **k: settings)
    cfg = _uvk5_config()
    assert cfg["backend"] == "uvk5"
    assert cfg["serial_port"] == "/dev/serial/by-id/usb-AIOC"
    assert cfg["frequency"] == 442_000_000
    assert cfg["tone"] == 100.0
    assert cfg["mode"] == "NFM"
    assert cfg["tx_allowed"] is False


# --- connect probe: dock / stock / dead --------------------------------------


def test_uvk5_connect_probe_dock_alive_passes(capsys):
    report = _Report()
    _uvk5_connect_probe(
        report, _UVK5_CFG, transport=_UvkProbeTransport(), hello_probe=lambda p: "0.32.21q"
    )
    out = capsys.readouterr().out
    assert report.ok
    assert "Dock firmware alive" in out
    assert "dock version" in out and "0.32.21q" in out
    assert "!=" not in out  # no wrong-version warning


def test_uvk5_connect_probe_dock_wrong_version_warns(capsys):
    report = _Report()
    _uvk5_connect_probe(
        report, _UVK5_CFG, transport=_UvkProbeTransport(), hello_probe=lambda p: "0.31.0q"
    )
    out = capsys.readouterr().out
    assert report.ok  # alive is a PASS; a version drift is a WARN, not a FAIL
    assert "Dock firmware alive" in out
    assert "!= pinned 0.32.21q" in out


def test_uvk5_connect_probe_dock_version_unanswered_is_expected_on_v3(capsys):
    # ADR 0122/0119: an unanswered plaintext HELLO is EXPECTED on the always-encrypted V3 dock fork,
    # not a fault — so it stays a PASS and the message says so (never implies a problem).
    report = _Report()
    _uvk5_connect_probe(
        report, _UVK5_CFG, transport=_UvkProbeTransport(), hello_probe=lambda p: None
    )
    out = capsys.readouterr().out
    assert report.ok  # a HELLO that goes unanswered must not fail the connect verdict
    assert "Dock firmware alive" in out
    assert "not read" in out and "expected" in out and "ADR 0119" in out


def test_uvk5_connect_probe_stock_firmware_fails(capsys):
    report = _Report()
    _uvk5_connect_probe(
        report,
        _UVK5_CFG,
        transport=_UvkProbeTransport(connect_exc=Uvk5Timeout("no answer")),
        hello_probe=lambda p: "0.32.21q",  # answers HELLO but not the dock register read
    )
    out = capsys.readouterr().out
    assert not report.ok
    assert "STOCK firmware" in out and "docs/uvk5-setup.md" in out


def test_uvk5_connect_probe_dead_generic_failure(capsys):
    report = _Report()
    _uvk5_connect_probe(
        report,
        _UVK5_CFG,
        transport=_UvkProbeTransport(connect_exc=Uvk5Timeout("no answer")),
        hello_probe=lambda p: None,  # answers neither → off/asleep/wrong port
    )
    out = capsys.readouterr().out
    assert not report.ok
    assert "no response to the register-read probe" in out
    assert "STOCK" not in out


def test_uvk5_connect_probe_missing_extra_degrades(capsys, monkeypatch):
    report = _Report()
    monkeypatch.setattr(
        "radio_server.backends.uvk5.transport.Uvk5Transport",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("needs the 'uvk5' extra")),
    )
    _uvk5_connect_probe(report, _UVK5_CFG)  # owns the transport → build raises RuntimeError
    out = capsys.readouterr().out
    assert not report.ok
    assert "cannot open the UV-K5 transport" in out


# --- HELLO probe over the firmware fake (plaintext out / plaintext in) --------


def test_uvk5_hello_probe_reads_version_from_dock():
    fake = FirmwareFakeSerial()
    fake.hello_version = b"0.32.21q".ljust(16, b"\x00")
    transport = Uvk5Transport(_serial_factory=lambda port, baud: fake, obfuscate=False)
    try:
        assert _uvk5_hello_probe("/dev/x", transport=transport) == "0.32.21q"
        # A plaintext HELLO (raw 0x0514) is what flips the fake's encryption OFF — an obfuscated
        # one would read as 0x6902 and flip it ON. So this proves plaintext-out (ADR 0114).
        assert fake.encrypted is False
    finally:
        transport.close()


def test_uvk5_hello_probe_reads_version_from_stock():
    fake = FirmwareFakeSerial()
    fake.dock = False  # stock radio: silent to dock commands, but HELLO is unguarded
    fake.hello_version = b"2.01.stock".ljust(16, b"\x00")
    transport = Uvk5Transport(_serial_factory=lambda port, baud: fake, obfuscate=False)
    try:
        assert _uvk5_hello_probe("/dev/x", transport=transport) == "2.01.stock"
    finally:
        transport.close()


def test_uvk5_hello_probe_none_when_dead():
    transport = Uvk5Transport(_serial_factory=lambda port, baud: FakeSerial(), obfuscate=False)
    try:
        assert _uvk5_hello_probe("/dev/x", transport=transport, timeout=0.3) is None
    finally:
        transport.close()


def test_uvk5_hello_probe_none_on_missing_extra(monkeypatch):
    # owns the transport and the ctor raises → None (inconclusive), never propagates.
    monkeypatch.setattr(
        "radio_server.backends.uvk5.transport.Uvk5Transport",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("needs the 'uvk5' extra")),
    )
    assert _uvk5_hello_probe("/dev/x") is None


# --- keying test -------------------------------------------------------------


def test_uvk5_keying_core_passes_when_radio_confirms_tx(capsys):
    fake = FirmwareFakeSerial()
    rc = _uvk5_keying_core(make_uvk5_radio(fake, frequency=145_500_000), seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "TX confirmed" in out and "unkeyed cleanly" in out


def test_uvk5_keying_core_fails_when_tx_not_confirmed(capsys):
    fake = FirmwareFakeSerial()
    fake.withhold_tx_confirm = True  # reg 0x30 never latches 0xC1FE → Uvk5KeyingError
    rc = _uvk5_keying_core(make_uvk5_radio(fake, frequency=145_500_000), seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "REFUSED" in out and "TX confirmed" not in out


def test_uvk5_keying_core_refused_when_tx_allowed_false(capsys):
    fake = FirmwareFakeSerial()
    radio = make_uvk5_radio(fake, frequency=145_500_000, tx_allowed=False)
    rc = _uvk5_keying_core(radio, seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "REFUSED" in out and "TX confirmed" not in out


def test_uvk5_key_test_refuses_non_interactive(monkeypatch):
    monkeypatch.setenv("CI", "1")
    assert _uvk5_key_test(_UVK5_CFG) == 2  # returns before building any radio (RF safety)


def test_uvk5_key_test_points_at_the_probe_on_open_failure(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("radio_server.doctor.sys.stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda *a: "CONFIRM")

    def boom(cfg):
        raise RuntimeError("the UV-K5 never answered within 10.0s")

    monkeypatch.setattr("radio_server.doctor._build_backend", boom)
    rc = _uvk5_key_test({**_UVK5_CFG})
    err = capsys.readouterr().err
    assert rc == 1
    assert "connect probe first" in err and "--backend uvk5" in err


# --- RX sample-rate estimate (AIOC real sound card) --------------------------


def test_format_soundcard_rx_rate_reports_measured_rate():
    lines = "\n".join(_format_soundcard_rx_rate(total_samples=48000 * 30, elapsed=30.0))
    assert "48,000 Hz" in lines and "nominal 48,000" in lines
    assert "tracks the nominal 48 kHz" in lines


def test_format_soundcard_rx_rate_flags_off_nominal():
    # 48000 samples over 30 s → 1600 Hz, wildly off nominal → a corrective line.
    lines = "\n".join(_format_soundcard_rx_rate(total_samples=48000, elapsed=30.0))
    assert "off nominal" in lines


def test_format_soundcard_rx_rate_needs_enough_samples():
    assert _format_soundcard_rx_rate(total_samples=100, elapsed=30.0) == [
        "  RX sample rate  : too few samples to estimate the true capture rate"
    ]


# --- _rx_noise: the HT-free RX self-test (ADR 0120) --------------------------

from radio_server import doctor as _doctor
from radio_server.doctor import RxLevels, _RX_NOISE_FORCE


class _FakeDockRadio:
    """A UV-K5 backend stand-in: records register writes and serves canned audio via receive()."""

    def __init__(self):
        # Fresh-idle firmware state: RX chain up, AF muted (REG_47=0x6042), loud gain.
        self.regs = {0x30: 0xBFF1, 0x47: 0x6042, 0x48: 0xB37E, 0x37: 0x9F1F}
        self.writes = []  # each entry is one _write_registers() call's pair list
        self.closed = False

    def _read_register(self, reg):
        return self.regs.get(reg, 0)

    def _write_registers(self, pairs):
        pairs = list(pairs)
        self.writes.append(pairs)
        for r, v in pairs:
            self.regs[r] = v

    def receive(self):
        return AudioFrame(b"\x00\x00" * 960)

    def close(self):
        self.closed = True


def _levels(peak):
    return RxLevels(frames=200, total_samples=192000, peak_sample=int(peak),
                    peak_block_rms=float(peak), avg_rms=float(peak), elapsed=5.0)


def _patch_backend(monkeypatch, radio, peak=None, raise_measure=False):
    monkeypatch.setattr(_doctor, "_build_backend", lambda cfg: radio)
    if raise_measure:
        def boom(r, *, seconds):
            raise RuntimeError("no capture device")
        monkeypatch.setattr(_doctor, "measure_rx_levels", boom)
    else:
        monkeypatch.setattr(_doctor, "measure_rx_levels", lambda r, *, seconds: _levels(peak))


def test_rx_noise_forces_measures_and_restores_when_alive(monkeypatch):
    radio = _FakeDockRadio()
    _patch_backend(monkeypatch, radio, peak=4000.0)
    rc = _doctor._rx_noise({"backend": "uvk5"}, seconds=0.1)
    assert rc == 0  # loud force-open noise -> RX alive
    # The force is issued first, exactly as specified, and unmutes REG_47.
    assert radio.writes[0] == list(_RX_NOISE_FORCE)
    assert (0x47, 0x6142) in radio.writes[0]
    # The last write restores every cached register (no leaked force-open state).
    assert (0x47, 0x6042) in radio.writes[-1]  # back to the cached MUTE value
    assert (0x30, 0xBFF1) in radio.writes[-1]
    assert (0x48, 0xB37E) in radio.writes[-1]
    assert radio.closed


def test_rx_noise_reports_dead_on_floor_and_still_restores(monkeypatch):
    radio = _FakeDockRadio()
    _patch_backend(monkeypatch, radio, peak=110.0)
    rc = _doctor._rx_noise({"backend": "uvk5"}, seconds=0.1)
    assert rc == 1  # noise floor -> RX dead
    assert (0x47, 0x6042) in radio.writes[-1]  # restored even when the verdict is dead
    assert radio.closed


def test_rx_noise_restores_registers_even_if_capture_fails(monkeypatch):
    radio = _FakeDockRadio()
    _patch_backend(monkeypatch, radio, raise_measure=True)
    rc = _doctor._rx_noise({"backend": "uvk5"}, seconds=0.1)
    assert rc == 1
    # Guardrail: the force-open state must not leak past the test, even on capture failure.
    assert radio.writes[0] == list(_RX_NOISE_FORCE)
    assert (0x47, 0x6042) in radio.writes[-1]
    assert radio.closed


def test_rx_noise_skips_non_uvk5_backends():
    assert _doctor._rx_noise({"backend": "kv4p"}, seconds=1.0) == 2
    assert _doctor._rx_noise({"backend": "baofeng"}, seconds=1.0) == 2


# --- --rssi: the live RSSI meter (ADR 0122) ----------------------------------


class _FakeRssiRadio:
    """UV-K5 stand-in whose reg 0x67 (RSSI) returns a scripted sequence, one per read (last repeats).
    Other regs read 0. Records close(); never keys."""

    def __init__(self, rssi_seq):
        self._seq = list(rssi_seq)
        self._i = 0
        self.closed = False

    def _read_register(self, reg):
        if reg != 0x67:
            return 0
        v = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return v

    def close(self):
        self.closed = True


def _no_sleep(_seconds):
    return None


def test_rssi_meter_streams_counts_and_busy_verdict(monkeypatch, capsys):
    # threshold 40: 10 → idle, 60 → BUSY, 200 → BUSY. Three samples over a 1 s window.
    radio = _FakeRssiRadio([10, 60, 200])
    monkeypatch.setattr(_doctor, "_build_backend", lambda cfg: radio)
    rc = _doctor._rssi_meter(
        {"backend": "uvk5", "squelch_threshold": 40}, seconds=1.0,
        clock=SeqClock([0, 0, 0, 0, 100]), sleep=_no_sleep,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "RSSI   10" in out and "RSSI   60" in out and "RSSI  200" in out
    assert out.count("BUSY (>= threshold)") == 2  # 60 and 200
    assert out.count("idle (<  threshold)") == 1   # 10
    assert "busy 2/3 (threshold 40)" in out  # the tuning summary
    assert radio.closed  # closed even though it only read


def test_rssi_meter_reports_no_samples(monkeypatch, capsys):
    radio = _FakeRssiRadio([0])
    monkeypatch.setattr(_doctor, "_build_backend", lambda cfg: radio)
    # A clock already past the window → zero reads.
    rc = _doctor._rssi_meter(
        {"backend": "uvk5", "squelch_threshold": 40}, seconds=1.0,
        clock=SeqClock([0, 100]), sleep=_no_sleep,
    )
    assert rc == 1
    assert "no RSSI samples" in capsys.readouterr().out
    assert radio.closed


def test_rssi_meter_skips_non_uvk5_backends():
    assert _doctor._rssi_meter({"backend": "kv4p"}, seconds=1.0) == 2
    assert _doctor._rssi_meter({"backend": "baofeng"}, seconds=1.0) == 2


# --- --rx-firststart-loop: the first-start dead-RX repro harness (ADR 0122) ---


def _patch_firststart(monkeypatch, radio, peaks):
    """Return `radio` from every _build_backend, and scripted peak_block_rms values from
    measure_rx_levels (one per loop iteration; last repeats). No real sleeps."""
    monkeypatch.setattr(_doctor, "_build_backend", lambda cfg: radio)
    seq = list(peaks)
    calls = {"i": 0}

    def _levels_seq(r, *, seconds):
        peak = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return RxLevels(frames=100, total_samples=96000, peak_sample=int(peak),
                        peak_block_rms=float(peak), avg_rms=float(peak), elapsed=float(seconds))

    monkeypatch.setattr(_doctor, "measure_rx_levels", _levels_seq)


def test_rx_firststart_loop_all_alive_returns_0(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142  # F3: force-open ran
    _patch_firststart(monkeypatch, radio, peaks=[4000.0])
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 3, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "F3 firmware CONFIRMED" in out
    assert out.count("ALIVE") == 3
    assert "summary: 0/3 dead starts" in out


def test_rx_firststart_loop_flags_the_radio_leg(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6042  # mute: the firmware force-open did NOT run (0x0870 lost)
    _patch_firststart(monkeypatch, radio, peaks=[110.0])  # floor
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 2, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("DEAD/RADIO") == 2  # REG_47 mute + floor → the radio leg
    assert "NOT on the F3 build" in out  # step-0 stamps the run unreliable (REG_47 never FM)
    assert "summary: 2/2 dead starts" in out


def test_rx_firststart_loop_flags_the_host_audio_leg(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142  # FM: the firmware force-open ran...
    _patch_firststart(monkeypatch, radio, peaks=[110.0])  # ...but no audio reached the AIOC (floor)
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 2, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("DEAD/HOST-AUDIO") == 2  # REG_47 FM + floor → the host-audio leg
    assert "F3 firmware CONFIRMED" in out


def test_rx_firststart_loop_counts_a_mix(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142
    _patch_firststart(monkeypatch, radio, peaks=[4000.0, 110.0, 4000.0])  # 1 dead of 3
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 3, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "summary: 1/3 dead starts" in out


def test_rx_firststart_f3_probe_detects_non_f3(monkeypatch):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6042  # idle mute — never FM
    monkeypatch.setattr(_doctor, "_build_backend", lambda cfg: radio)
    f3, reg47 = _doctor._rx_firststart_f3_probe({"backend": "uvk5"}, sleep=lambda s: None)
    assert f3 is False and reg47 == 0x6042
    assert radio.closed


def test_rx_firststart_loop_skips_non_uvk5():
    assert _doctor._rx_firststart_loop({"backend": "kv4p"}, 3) == 2
    assert _doctor._rx_firststart_loop({"backend": "baofeng"}, 3) == 2


# --- construction retry + inter-iteration settle: the harness-defect fix (ADR 0123) ---

from radio_server.backends.uvk5.transport import Uvk5Timeout


def _droppable_build(monkeypatch, seq):
    """Patch _build_backend to consume a scripted per-call outcome list: a radio (success) or the
    literal "drop" (raise Uvk5Timeout, modelling a seeding read that landed in a reset-on-open window).
    One entry per _build_backend call — step 0's probe build is first, then one per open (plus retries)."""
    items = list(seq)
    state = {"i": 0}

    def _build(cfg):
        outcome = items[state["i"]]
        state["i"] += 1
        if outcome == "drop":
            raise Uvk5Timeout("scripted reset-on-open window")
        return outcome

    monkeypatch.setattr(_doctor, "_build_backend", _build)


def _patch_measure(monkeypatch, peaks):
    """Scripted measure_rx_levels: one peak per SUCCESSFUL open (last repeats)."""
    seq = list(peaks)
    calls = {"i": 0}

    def _levels_seq(r, *, seconds):
        peak = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return RxLevels(frames=100, total_samples=96000, peak_sample=int(peak),
                        peak_block_rms=float(peak), avg_rms=float(peak), elapsed=float(seconds))

    monkeypatch.setattr(_doctor, "measure_rx_levels", _levels_seq)


def test_rx_firststart_loop_retries_construct_timeout_then_alive(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142  # F3 confirmed
    # step-0 build, then iter-0 drops once (< attempts) before succeeding.
    _droppable_build(monkeypatch, [radio, "drop", radio])
    _patch_measure(monkeypatch, peaks=[4000.0])
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 1, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("ALIVE") == 1
    assert "construct timeout" not in out  # a tolerated reset-race open is NOT counted dead
    assert "summary: 0/1 dead starts" in out


def test_rx_firststart_loop_counts_exhausted_construct_as_dead_radio(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142
    # step-0 build; iter-0 exhausts all 3 attempts (3 drops); iter-1 opens clean and is alive.
    _droppable_build(monkeypatch, [radio, "drop", "drop", "drop", radio])
    _patch_measure(monkeypatch, peaks=[4000.0])
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 2, seconds=0.1, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "construct timeout after 3 attempts  DEAD/RADIO" in out  # counted, leg-attributed, no crash
    assert out.count("ALIVE") == 1  # the loop ran the remaining iteration to completion
    assert "summary: 1/2 dead starts" in out


def test_rx_firststart_loop_settles_between_iterations_not_before_the_first(monkeypatch, capsys):
    radio = _FakeDockRadio()
    radio.regs[0x47] = 0x6142
    _droppable_build(monkeypatch, [radio, radio, radio, radio])  # step-0 + 3 clean opens, no drops
    _patch_measure(monkeypatch, peaks=[4000.0])
    sleeps: list[float] = []
    rc = _doctor._rx_firststart_loop({"backend": "uvk5"}, 3, seconds=0.1, sleep=sleeps.append)
    assert rc == 0
    # one settle before each iteration EXCEPT the first → iterations - 1; no drops, so no retry intervals.
    assert sleeps.count(_doctor._RX_FIRSTSTART_SETTLE_S) == 2
    assert _doctor._RX_FIRSTSTART_CONSTRUCT_INTERVAL_S not in sleeps


def test_build_backend_settled_retries_then_returns(monkeypatch):
    radio = _FakeDockRadio()
    _droppable_build(monkeypatch, ["drop", "drop", radio])  # 2 drops < 3 attempts
    sleeps: list[float] = []
    got = _doctor._build_backend_settled({"backend": "uvk5"}, attempts=3, interval=0.25, sleep=sleeps.append)
    assert got is radio
    assert sleeps == [0.25, 0.25]  # settled between the three tries, not after the winner


def test_build_backend_settled_reraises_after_attempts_exhausted(monkeypatch):
    _droppable_build(monkeypatch, ["drop", "drop", "drop"])
    sleeps: list[float] = []
    with pytest.raises(Uvk5Timeout):
        _doctor._build_backend_settled({"backend": "uvk5"}, attempts=3, interval=0.25, sleep=sleeps.append)
    assert sleeps == [0.25, 0.25]  # no settle after the final failed attempt


# --- _check_audio: ALSA card-id resolution is applied and reported (ADR 0124) ----------------


class _FakeSdForAudioCheck:
    """A sounddevice stand-in for :func:`_check_audio`: the bench's PortAudio table + the two
    ``check_*_settings`` probes, which accept **only** an integer index (as the real card does for
    a name PortAudio never reports)."""

    devices = [
        {"name": "All-In-One-Cable: USB Audio (hw:2,0)", "max_input_channels": 1, "max_output_channels": 1},
    ]

    def __init__(self) -> None:
        self.checked: list = []

    def query_devices(self):
        return list(self.devices)

    def _check(self, device, **kw):
        self.checked.append(device)
        if not isinstance(device, int):
            raise ValueError(f"No input device matching {device!r}")

    def check_input_settings(self, device=None, **kw):
        self._check(device, **kw)

    def check_output_settings(self, device=None, **kw):
        self._check(device, **kw)


def test_check_audio_resolves_and_reports_an_alsa_card_id(monkeypatch, tmp_path, capsys):
    """``AIOC_K6`` is not a PortAudio name; _check_audio must resolve it to the index and say so."""
    import sys

    from radio_server.backends import soundcard

    card = tmp_path / "card2"
    card.mkdir()
    (card / "id").write_text("AIOC_K6\n")
    monkeypatch.setattr(soundcard, "ALSA_SYSFS_ROOT", tmp_path)
    sd = _FakeSdForAudioCheck()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    report = _doctor._Report()
    _doctor._check_audio(report, "AIOC_K6", "AIOC_K6")

    out = capsys.readouterr().out
    assert "resolved by ALSA card id" in out
    assert "'AIOC_K6' -> PortAudio index 0" in out
    assert report.ok, out  # the probes were handed the index, so both legs accept
    assert sd.checked == [0, 0]  # never the raw string
