"""Factory/registry wiring: mock builds; hardware stubs raise; unknown names error."""

import pytest

from radio_server.backends import MockRadio, Radio, available_backends, create_radio


def test_create_mock_returns_a_radio():
    radio = create_radio("mock")
    assert isinstance(radio, MockRadio)
    assert isinstance(radio, Radio)


def test_create_mock_passes_kwargs():
    radio = create_radio("mock", supports_cat=False, canned_rx=b"x")
    assert radio.supports_cat is False
    assert radio.receive() == b"x"


def test_registry_lists_all_backends():
    assert set(available_backends()) == {"mock", "v71", "baofeng"}


@pytest.mark.parametrize("backend", ["v71", "baofeng"])
def test_hardware_backends_are_wired_but_not_implemented(backend):
    assert backend in available_backends()
    with pytest.raises(NotImplementedError):
        create_radio(backend)


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError):
        create_radio("nonesuch")
