"""ALSA card-id device resolution for the shared sound-card seam (ADR 0124).

sounddevice matches a string device against PortAudio *names*, and those come from the ALSA card
*name* (the USB product string) — never the card *id* that udev's ``ATTR{id}`` assigns. So
``input_device = "AIOC_K6"`` could never resolve, however correct the udev rule was.
:func:`resolve_device` closes that gap **without** disturbing any config that already resolves.
"""

from __future__ import annotations

import pytest

from radio_server.backends import soundcard
from radio_server.backends.soundcard import (
    open_capture_stream,
    open_playout_stream,
    resolve_device,
)

#: The bench's real PortAudio table (ubuntuserver, 2026-07-24, card free). Every AIOC entry names
#: the card ``All-In-One-Cable``; the udev-assigned id ``AIOC_K6`` appears nowhere — the defect.
BENCH_DEVICES = [
    {"name": "HDA Intel PCH: ALC269VB Analog (hw:0,0)", "max_input_channels": 2, "max_output_channels": 2},
    {"name": "HDA ATI HDMI: 0 (hw:1,3)", "max_input_channels": 0, "max_output_channels": 8},
    {"name": "All-In-One-Cable: USB Audio (hw:2,0)", "max_input_channels": 1, "max_output_channels": 1},
    {"name": "default", "max_input_channels": 128, "max_output_channels": 128},
]


class FakeSd:
    """A sounddevice stand-in: ``query_devices`` plus recording stream factories."""

    def __init__(self, devices=None) -> None:
        self._devices = BENCH_DEVICES if devices is None else devices
        self.opened: list[dict] = []

    def query_devices(self):
        return list(self._devices)

    def RawInputStream(self, **kw):
        self.opened.append(kw)
        return _FakeStream()

    def RawOutputStream(self, **kw):
        self.opened.append(kw)
        return _FakeStream()


class _FakeStream:
    def start(self) -> None:
        pass


class SeamWithoutQueryDevices:
    """The shape the backend tests inject — stream factories only, no PortAudio surface.

    Regression guard: resolution must not reach for ``query_devices`` on such a seam.
    """

    def RawInputStream(self, **kw):
        self.kw = kw
        return _FakeStream()

    def RawOutputStream(self, **kw):
        self.kw = kw
        return _FakeStream()


@pytest.fixture
def sysfs(tmp_path):
    """A fake ``/sys/class/sound``: card2 is the AIOC, renamed ``AIOC_K6`` by the udev rule."""
    for index, card_id in ((0, "PCH"), (1, "HDMI"), (2, "AIOC_K6")):
        card = tmp_path / f"card{index}"
        card.mkdir()
        (card / "id").write_text(f"{card_id}\n")
    return tmp_path


# --- the new path ------------------------------------------------------------


def test_card_id_resolves_to_the_portaudio_index(sysfs):
    """``AIOC_K6`` -> ALSA card 2 -> the PortAudio device named ``(hw:2,0)``."""
    assert resolve_device(FakeSd(), "AIOC_K6", kind="input", sysfs_root=sysfs) == 2
    assert resolve_device(FakeSd(), "AIOC_K6", kind="output", sysfs_root=sysfs) == 2


def test_direction_picks_the_leg_being_opened(sysfs):
    """One card can expose capture and playback as separate PortAudio entries — take the right one."""
    devices = [
        {"name": "All-In-One-Cable: USB Audio (hw:2,0)", "max_input_channels": 0, "max_output_channels": 1},
        {"name": "All-In-One-Cable: USB Audio (hw:2,1)", "max_input_channels": 1, "max_output_channels": 0},
    ]
    sd = FakeSd(devices)
    assert resolve_device(sd, "AIOC_K6", kind="output", sysfs_root=sysfs) == 0
    assert resolve_device(sd, "AIOC_K6", kind="input", sysfs_root=sysfs) == 1


def test_card_id_with_no_matching_hw_device_passes_through(sysfs):
    """Card id is known but PortAudio exposes no ``(hw:2,…)`` entry — hand the string back."""
    devices = [{"name": "default", "max_input_channels": 128, "max_output_channels": 128}]
    got = resolve_device(FakeSd(devices), "AIOC_K6", kind="input", sysfs_root=sysfs)
    assert got == "AIOC_K6"


# --- no regression for what already works ------------------------------------


def test_name_substring_is_passed_through_unchanged(sysfs):
    """The documented default still resolves the old way — sounddevice does its own matching."""
    got = resolve_device(FakeSd(), "All-In-One-Cable: USB", kind="input", sysfs_root=sysfs)
    assert got == "All-In-One-Cable: USB"


def test_name_match_wins_over_a_same_named_card_id(sysfs, tmp_path):
    """Existing behaviour is tried first: a PortAudio name match is never overridden by sysfs."""
    card = tmp_path / "card3"
    card.mkdir()
    (card / "id").write_text("default\n")  # a card id that also substring-matches a PortAudio name
    assert resolve_device(FakeSd(), "default", kind="input", sysfs_root=tmp_path) == "default"


@pytest.mark.parametrize("device", [None, 0, 2])
def test_index_and_none_pass_through(device, sysfs):
    assert resolve_device(FakeSd(), device, kind="input", sysfs_root=sysfs) == device


def test_unknown_string_passes_through_for_sounddevice_to_reject(sysfs):
    """An unresolvable name is handed back so sounddevice raises its own familiar error."""
    assert resolve_device(FakeSd(), "NOPE", kind="input", sysfs_root=sysfs) == "NOPE"


def test_missing_sysfs_passes_through(tmp_path):
    """CI and macOS have no ``/sys/class/sound`` — fall through, never raise."""
    got = resolve_device(FakeSd(), "AIOC_K6", kind="input", sysfs_root=tmp_path / "absent")
    assert got == "AIOC_K6"


def test_seam_without_query_devices_passes_through(sysfs):
    """The injected backend test seam has no ``query_devices`` — must not AttributeError."""
    got = resolve_device(SeamWithoutQueryDevices(), "AIOC_K6", kind="input", sysfs_root=sysfs)
    assert got == "AIOC_K6"


def test_query_devices_failure_passes_through(sysfs):
    """A PortAudio blow-up must not mask the real error the stream open would raise."""

    class Exploding(FakeSd):
        def query_devices(self):
            raise RuntimeError("PortAudio not initialised")

    assert resolve_device(Exploding(), "AIOC_K6", kind="input", sysfs_root=sysfs) == "AIOC_K6"


# --- the streams both backends open through ----------------------------------


def test_open_capture_stream_resolves_the_card_id(sysfs, monkeypatch):
    monkeypatch.setattr(soundcard, "ALSA_SYSFS_ROOT", sysfs)
    sd = FakeSd()
    open_capture_stream(sd, device="AIOC_K6", blocksize=960)
    assert sd.opened[0]["device"] == 2


def test_open_playout_stream_resolves_the_card_id(sysfs, monkeypatch):
    monkeypatch.setattr(soundcard, "ALSA_SYSFS_ROOT", sysfs)
    sd = FakeSd()
    open_playout_stream(sd, device="AIOC_K6", blocksize=960)
    assert sd.opened[0]["device"] == 2


def test_open_capture_stream_leaves_a_working_name_alone(sysfs, monkeypatch):
    monkeypatch.setattr(soundcard, "ALSA_SYSFS_ROOT", sysfs)
    sd = FakeSd()
    open_capture_stream(sd, device="All-In-One-Cable: USB", blocksize=960)
    assert sd.opened[0]["device"] == "All-In-One-Cable: USB"
