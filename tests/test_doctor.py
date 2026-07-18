"""AIOC doctor diagnostics (ADR 0029 bring-up): RX-level measurement + RF-mode safety refusal.

All hardware-free: `measure_rx_levels` is driven by a `MockRadio` scripted with known frames and an
injected clock (no real sleeps), and the RF modes (`--tx-tone`/`--key-test`) are checked only for
their refuse-when-unattended guard — actual keying is never exercised in pytest. The `--link`
entry selection (`_mumble_config`, ADR 0042/0052) is driven off temp default-path config/secrets
files via a chdir — no network, no pymumble.
"""

from __future__ import annotations

import shutil

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
    radio = MockRadio(supports_cat=False, rx_frames=[tone, tone, tone])
    # start=0, three iterations see clock<1, the fourth sees 100 and stops → exactly 3 frames read.
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
    _build_backend,
    _check_kv4p_serial,
    _kv4p_connect_probe,
    _kv4p_key_test,
    _kv4p_keying_core,
    _resolve_doctor_backend,
    _Report,
)

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
    monkeypatch.setattr(
        "radio_server.config.load_settings", lambda: make_settings({"server.backend": "kv4p"})
    )
    assert _resolve_doctor_backend(argparse.Namespace(backend=None)) == "kv4p"


def test_resolve_backend_defaults_to_baofeng(monkeypatch):
    # The schema default is 'mock', and any non-kv4p value falls back to the AIOC checks (unchanged).
    monkeypatch.setattr("radio_server.config.load_settings", lambda: make_settings({}))
    assert _resolve_doctor_backend(argparse.Namespace(backend=None)) == "baofeng"


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
        rf_module_type=0,  # SA818_VHF
        min_radio_freq=134.0,
        max_radio_freq=174.0,
        features=3,
    )
    state = _device_state(
        flags=int(DeviceStateFlag.TX_ALLOWED | DeviceStateFlag.RADIO_CONFIG_VALID)
    )
    fake = _ProbeTransport(state=state, hello=Hello(version=version, device_state=state))
    _kv4p_connect_probe(report, _KV4P_CFG, transport=fake)
    out = capsys.readouterr().out
    assert report.ok
    assert "HELLO received" in out and "SA818_VHF" in out
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
