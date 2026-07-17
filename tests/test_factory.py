"""Factory/registry wiring: mock + baofeng build; the v71 stub raises; unknown names error."""

import pytest

from radio_server.audio import AudioFrame
from radio_server.backends import (
    SHARED_CAPS,
    MockRadio,
    Radio,
    available_backends,
    create_radio,
)

from .test_aioc_baofeng import make_backend


def test_create_mock_returns_a_radio():
    radio = create_radio("mock")
    assert isinstance(radio, MockRadio)
    assert isinstance(radio, Radio)


def test_create_mock_passes_kwargs():
    radio = create_radio("mock", supports_cat=False, canned_rx=AudioFrame(b"x"))
    assert radio.supports_cat is False
    assert radio.receive() == AudioFrame(b"x")


def test_registry_lists_all_backends():
    assert set(available_backends()) == {"mock", "v71", "baofeng", "kv4p"}


def test_create_baofeng_returns_a_radio():
    # The AIOC backend is live (ADR 0029). Constructed via the factory with injected fake
    # serial/audio seams so no hardware or the 'hardware' extra is needed in CI.
    radio = make_backend()
    assert isinstance(radio, Radio)
    assert radio.capabilities() == SHARED_CAPS
    assert not hasattr(radio, "set_frequency")


def test_v71_is_wired_but_not_implemented():
    assert "v71" in available_backends()
    with pytest.raises(NotImplementedError):
        create_radio("v71")


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError):
        create_radio("nonesuch")
