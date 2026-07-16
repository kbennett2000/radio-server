"""Mumble destination entries (ADR 0042): shape validation and DTMF collision rules.

`resolve_mumble_entries` is the fail-loud gate between the raw ``[[mumble.servers]]`` tables and
the typed tuple every consumer sees; `validate_link_digits` cross-checks the combos against the
disconnect combo and the resolved ``[services]`` keypad. Everything here is exact-string matching —
the framer submits the whole buffer, so ``"13"`` and ``"1"`` are distinct combos, not a prefix
collision.
"""

from __future__ import annotations

import pytest

from radio_server.link import (
    DEFAULT_MUMBLE_DISCONNECT_DTMF,
    MumbleEntry,
    link_username,
    mumble_password_secret,
    resolve_mumble_entries,
    validate_link_digits,
)


def _raw(**overrides):
    entry = {"name": "home", "host": "murmur.example.net"}
    entry.update(overrides)
    return entry


# --- resolve_mumble_entries: shape ----------------------------------------------------------


def test_empty_and_none_resolve_to_no_entries():
    assert resolve_mumble_entries(None) == ()
    assert resolve_mumble_entries([]) == ()


def test_minimal_entry_gets_marked_defaults():
    (entry,) = resolve_mumble_entries([_raw()])
    assert entry == MumbleEntry(name="home", host="murmur.example.net")
    assert entry.port == 64738
    assert entry.channel == "" and entry.dtmf == ""
    assert entry.tx_to_rf is True and entry.autoconnect is False


def test_full_entry_round_trips_every_field():
    (entry,) = resolve_mumble_entries(
        [
            _raw(
                name="club_net",
                port=64739,
                channel="Club Net",
                dtmf="1234",
                tx_to_rf=False,
                autoconnect=True,
            )
        ]
    )
    assert entry.name == "club_net" and entry.port == 64739
    assert entry.channel == "Club Net" and entry.dtmf == "1234"
    assert entry.tx_to_rf is False and entry.autoconnect is True


def test_username_field_fails_loud_with_the_migration_message():
    # Not a generic unknown-field typo: the field existed and configs may still carry it.
    with pytest.raises(RuntimeError, match="username is no longer configurable"):
        resolve_mumble_entries([_raw(username="w1aw-gw")])


@pytest.mark.parametrize("name", ["", "Home", "has-dash", "has space", "x" * 33])
def test_bad_name_slug_fails_loud(name):
    with pytest.raises(RuntimeError, match="name"):
        resolve_mumble_entries([_raw(name=name)])


def test_duplicate_name_fails_loud():
    with pytest.raises(RuntimeError, match="more than once"):
        resolve_mumble_entries([_raw(), _raw(dtmf="13")])


def test_missing_host_fails_loud():
    with pytest.raises(RuntimeError, match="host is required"):
        resolve_mumble_entries([_raw(host="")])


def test_unknown_field_fails_loud_as_a_typo():
    with pytest.raises(RuntimeError, match="unknown field.*pasword"):
        resolve_mumble_entries([_raw(pasword="oops")])


def test_two_autoconnect_entries_fail_loud():
    with pytest.raises(RuntimeError, match="only one entry may autoconnect"):
        resolve_mumble_entries(
            [_raw(autoconnect=True), _raw(name="away", autoconnect=True)]
        )


# --- resolve_mumble_entries: combo digits ----------------------------------------------------


@pytest.mark.parametrize("digits", ["13#", "*13", "1 3", "e"])
def test_bad_dtmf_charset_fails_loud(digits):
    with pytest.raises(RuntimeError, match="0-9/A-D"):
        resolve_mumble_entries([_raw(dtmf=digits)])


def test_empty_dtmf_means_no_combo():
    (entry,) = resolve_mumble_entries([_raw(dtmf="")])
    assert entry.dtmf == ""


def test_duplicate_dtmf_across_entries_fails_loud():
    with pytest.raises(RuntimeError, match="already assigned"):
        resolve_mumble_entries([_raw(dtmf="13"), _raw(name="away", dtmf="13")])


def test_letter_digits_are_valid_combos():
    (entry,) = resolve_mumble_entries([_raw(dtmf="1A2B")])
    assert entry.dtmf == "1A2B"


# --- validate_link_digits: cross-channel collisions ------------------------------------------

_BINDINGS = {"1": "time", "4": "station-id", "99": "logout"}


def test_clean_layout_validates():
    entries = resolve_mumble_entries([_raw(dtmf="13"), _raw(name="away", dtmf="1234")])
    validate_link_digits(entries, DEFAULT_MUMBLE_DISCONNECT_DTMF, _BINDINGS)


def test_combo_equal_to_disconnect_fails_loud():
    entries = resolve_mumble_entries([_raw(dtmf="73")])
    with pytest.raises(RuntimeError, match="disconnect_dtmf"):
        validate_link_digits(entries, "73", _BINDINGS)


def test_combo_equal_to_a_services_digit_fails_loud():
    entries = resolve_mumble_entries([_raw(dtmf="99")])
    with pytest.raises(RuntimeError, match="already bound to 'logout'"):
        validate_link_digits(entries, "73", _BINDINGS)


def test_disconnect_equal_to_a_services_digit_fails_loud():
    with pytest.raises(RuntimeError, match="disconnect_dtmf"):
        validate_link_digits((), "4", _BINDINGS)


def test_exact_string_matching_means_prefixes_do_not_collide():
    # The framer submits the whole buffered string, so "1" (a service) and "13" (an entry)
    # coexist — this is the documented non-collision.
    entries = resolve_mumble_entries([_raw(dtmf="13")])
    validate_link_digits(entries, "730", _BINDINGS)


def test_password_secret_name_shape():
    assert mumble_password_secret("home") == "mumble_password_home"


# --- link_username: the one nick the station presents everywhere -----------------------------


def test_link_username_is_the_callsign_tagged_as_the_station():
    assert link_username("AE9S") == "AE9S (radio-server)"


def test_link_username_without_a_callsign_falls_back_to_the_bare_default():
    # Bench/mock deployments run without a callsign (they never transmit) — keep them connectable.
    assert link_username(None) == "radio-server"
    assert link_username("") == "radio-server"
