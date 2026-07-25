"""The bench runner's refuse-to-key guard (ADR 0135).

``scripts/bench/`` carries no test coverage, which is defensible for a diagnostic script and not
defensible for the one function standing between an unattended acceptance run and a carrier on a
live repeater's input. The guard is pure given a fake ``api``, so it costs nothing to pin.

What these tests are really protecting: the guard grew a preset deny-list on top of its bench
allow-list, and the interesting cases are all about the seam between them. A bench frequency is
normally safe — unless somebody imported a real repeater onto it, at which point "this is a bench
frequency" and "this is a live machine" are both true at once.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ACCEPTANCE = Path(__file__).resolve().parent.parent / "scripts" / "bench" / "acceptance.py"

BENCH_HZ = 445_800_000
#: A real repeater from the operator's imported set: 145.145 output, 144.545 input.
REPEATER_OUT_HZ = 145_145_000
REPEATER_IN_HZ = 144_545_000


@pytest.fixture(scope="module")
def acceptance():
    spec = importlib.util.spec_from_file_location("bench_acceptance", _ACCEPTANCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_acceptance"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(acceptance, monkeypatch, tmp_path):
    """`bench_frequency_only` with both of its data sources faked.

    Returns a callable taking the station's reported (frequency, tx_frequency) plus the two preset
    sources, and giving back whether the guard would allow keying.
    """

    def run(*, rx=BENCH_HZ, tx=None, api_presets=None, file_presets=None, status_code=200):
        def fake_api(base, method, path, body=None, raw=None, timeout=15.0):
            if path == "/status":
                return status_code, {"frequency": rx, "tx_frequency": tx}
            if path == "/presets":
                if api_presets is None:
                    return 500, {}
                return 200, {"presets": api_presets}
            raise AssertionError(f"unexpected call {method} {path}")

        monkeypatch.setattr(acceptance, "api", fake_api)
        if file_presets is None:
            monkeypatch.setattr(acceptance, "RADIO_CONFIG_PATH", tmp_path / "absent.toml")
        else:
            toml = tmp_path / "radio.toml"
            toml.write_text("".join(
                "[[presets]]\nname = {!r}\nfrequency = {}\n{}\n".format(
                    p["name"], p["frequency"],
                    f"tx_frequency = {p['tx_frequency']}" if p.get("tx_frequency") else "",
                )
                for p in file_presets
            ))
            monkeypatch.setattr(acceptance, "RADIO_CONFIG_PATH", toml)
        stage = acceptance.Stage(name="test")
        allowed = acceptance.bench_frequency_only("https://x", stage, "test")
        return allowed, stage

    return run


def preset(name, frequency, tx_frequency=None):
    row = {"name": name, "frequency": frequency}
    if tx_frequency is not None:
        row["tx_frequency"] = tx_frequency
    return row


def test_a_bench_frequency_with_the_real_repeaters_elsewhere_is_allowed(guard):
    allowed, _ = guard(api_presets=[preset("W0CRA", REPEATER_OUT_HZ, REPEATER_IN_HZ)])
    assert allowed


def test_a_repeater_imported_onto_a_bench_frequency_is_refused(guard):
    """The hazard the allow-list alone cannot see.

    445.800 is a bench frequency AND, in this configuration, a real machine's output. The old guard
    said yes because it only asked the first question.
    """
    allowed, stage = guard(api_presets=[preset("Some Repeater", BENCH_HZ, BENCH_HZ - 5_000_000)])
    assert not allowed
    assert "REFUSING to key" in "\n".join(stage.notes)


def test_both_preset_sources_unreadable_refuses(guard):
    """Fail closed. An empty deny-list means 'nothing is forbidden', which is exactly backwards."""
    allowed, stage = guard(api_presets=None, file_presets=None)
    assert not allowed
    assert "could not read the configured presets" in "\n".join(stage.notes)


def test_the_two_sources_are_unioned_not_first_wins(guard):
    """A server that loaded zero presets must not unlock what the file forbids.

    This is the restart case: the API answers 200 with an empty list, which is a successful read of
    nothing. Trusting it alone would leave every repeater in the file keyable.

    The station must sit on a BENCH frequency for this to test what it claims. Point it at a real
    repeater instead and the allow-list refuses first, the deny-list is never consulted, and the
    test passes whether or not the sources are unioned — which is exactly how it was written the
    first time, and mutation testing is what caught it.
    """
    allowed, _ = guard(
        rx=BENCH_HZ,
        api_presets=[],
        file_presets=[preset("Some Repeater", BENCH_HZ, BENCH_HZ - 5_000_000)],
    )
    assert not allowed


def test_the_bench_exemption_is_by_frequency_not_by_name(guard):
    """Naming a real repeater 'Bench Split' must not exempt it.

    The runner's own fixtures are presets on bench frequencies, so the deny-list has to exempt
    something — and if it exempted a *name*, the exemption would be forgeable by a CHIRP import.
    """
    allowed, _ = guard(
        rx=REPEATER_OUT_HZ,
        api_presets=[preset("Bench Split", REPEATER_OUT_HZ, REPEATER_IN_HZ)],
    )
    assert not allowed


def test_an_armed_split_to_a_repeater_input_is_refused_even_from_a_bench_rx(guard):
    """What actually goes on the air is the TX leg, and that is what gets checked.

    Listening on the bench pair while armed to a real input is the precise shape of the accident
    this guard exists to prevent: every status field looks like the bench except the one that keys.
    """
    allowed, stage = guard(
        rx=BENCH_HZ,
        tx=REPEATER_IN_HZ,
        api_presets=[preset("W0CRA", REPEATER_OUT_HZ, REPEATER_IN_HZ)],
    )
    assert not allowed
    assert "REFUSING to key" in "\n".join(stage.notes)


def test_the_bench_split_fixture_itself_still_keys(guard):
    """The guard must not refuse the bench. Both legs of the split fixture are bench frequencies."""
    allowed, _ = guard(
        rx=BENCH_HZ,
        tx=446_400_000,
        api_presets=[preset("Bench Split", BENCH_HZ, 446_400_000),
                     preset("W0CRA", REPEATER_OUT_HZ, REPEATER_IN_HZ)],
    )
    assert allowed


def test_the_kv4p_tuning_raster_still_counts_as_the_same_channel(guard):
    """445799988 is 445.800 by every measure that matters (the kv4p quantises to 2500 Hz)."""
    allowed, _ = guard(rx=BENCH_HZ - 12, api_presets=[preset("W0CRA", REPEATER_OUT_HZ)])
    assert allowed


def test_a_status_read_failure_refuses(guard):
    allowed, stage = guard(status_code=500, api_presets=[])
    assert not allowed
    assert "could not read /status" in "\n".join(stage.notes)
