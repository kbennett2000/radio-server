"""Mumble destination entries (ADR 0042/0052): shape validation and DTMF collision rules.

`resolve_mumble_entries` is the fail-loud gate between the raw ``[[mumble.servers]]`` tables and
the typed tuple every consumer sees; `validate_link_digits` cross-checks the combos against the
disconnect combo and the resolved ``[services]`` keypad. Everything here is exact-string matching —
the framer submits the whole buffer, so ``"13"`` and ``"1"`` are distinct combos, not a prefix
collision. Names are free text (ADR 0052); the identifier roles moved to the derived ``slug``
(`slugify`), so uniqueness is enforced on slugs and the password secret is keyed by slug.
"""

from __future__ import annotations

import pytest

from radio_server.link import (
    DEFAULT_MUMBLE_DISCONNECT_DTMF,
    MumbleEntry,
    link_username,
    mumble_password_secret,
    resolve_mumble_entries,
    slugify,
    validate_link_digits,
)
from radio_server.link.entries import MAX_NAME_LENGTH


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
    assert entry == MumbleEntry(name="home", host="murmur.example.net", slug="home")
    assert entry.port == 64738
    assert entry.channel == "" and entry.dtmf == ""
    assert entry.tx_to_rf is True and entry.autoconnect is False
    assert entry.password == ""


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
                password="gate-code",
            )
        ]
    )
    assert entry.name == "club_net" and entry.slug == "club_net" and entry.port == 64739
    assert entry.channel == "Club Net" and entry.dtmf == "1234"
    assert entry.tx_to_rf is False and entry.autoconnect is True
    assert entry.password == "gate-code"


def test_username_field_fails_loud_with_the_migration_message():
    # Not a generic unknown-field typo: the field existed and configs may still carry it.
    with pytest.raises(RuntimeError, match="username is no longer configurable"):
        resolve_mumble_entries([_raw(username="w1aw-gw")])


# --- names are free text (ADR 0052); the derived slug carries the identifier roles -----------


def test_free_text_name_is_valid_and_derives_its_slug():
    (entry,) = resolve_mumble_entries([_raw(name="Radio Server Demo")])
    assert entry.name == "Radio Server Demo"
    assert entry.slug == "radio_server_demo"


@pytest.mark.parametrize("name", ["", "   ", "x" * (MAX_NAME_LENGTH + 1)])
def test_empty_or_oversized_name_fails_loud(name):
    with pytest.raises(RuntimeError, match=f"must be 1-{MAX_NAME_LENGTH} characters"):
        resolve_mumble_entries([_raw(name=name)])


def test_name_without_any_alphanumeric_fails_loud():
    # "!!!" slugifies to "" — there is no identifier to derive, so the entry is rejected.
    with pytest.raises(RuntimeError, match="letter or digit"):
        resolve_mumble_entries([_raw(name="!!!")])


def test_incoming_slug_field_is_tolerated_and_recomputed():
    # The settings editor round-trips whole serialized entries, so a "slug" key must not be a
    # typo error — but a stale value is never trusted, always recomputed from the name.
    (entry,) = resolve_mumble_entries([_raw(name="Radio Server Demo", slug="stale_value")])
    assert entry.slug == "radio_server_demo"


def test_duplicate_name_fails_loud_on_the_slug():
    with pytest.raises(RuntimeError, match="collides with"):
        resolve_mumble_entries([_raw(), _raw(dtmf="13")])


def test_distinct_names_with_the_same_slug_collide():
    # "Club Net" and "club_net" derive the same identifier — one secret name, one URL segment —
    # so the collision names both entries and the shared slug.
    with pytest.raises(RuntimeError, match=r"'club_net' collides with 'Club Net'.*both shorten to 'club_net'"):
        resolve_mumble_entries([_raw(name="Club Net"), _raw(name="club_net")])


# --- slugify: the name → identifier derivation (ADR 0052) ------------------------------------


def test_slugify_lowercases_and_collapses_runs_to_underscores():
    assert slugify("Radio Server Demo") == "radio_server_demo"
    assert slugify("  W1AW -- Club Net!  ") == "w1aw_club_net"


def test_slugify_maps_a_legacy_slug_name_to_itself():
    # Pre-0052 configs named entries in the slug alphabet already; their secret names, env-var
    # suffixes and URL segments must not shift under them.
    for legacy in ("home", "club_net", "w1aw_2"):
        assert slugify(legacy) == legacy


def test_slugify_caps_at_32_without_a_dangling_separator():
    assert slugify("x" * 40) == "x" * 32
    assert slugify("a" * 31 + " b") == "a" * 31  # the cap never leaves a trailing "_"


def test_missing_host_fails_loud():
    with pytest.raises(RuntimeError, match="host is required"):
        resolve_mumble_entries([_raw(host="")])


def test_unknown_field_fails_loud_as_a_typo():
    with pytest.raises(RuntimeError, match="unknown field.*pasword") as excinfo:
        resolve_mumble_entries([_raw(pasword="oops")])
    # The known-fields hint lists the real config surface: `password` yes, derived `slug` no.
    assert "password" in str(excinfo.value) and "slug" not in str(excinfo.value)


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


def test_password_secret_name_is_keyed_by_slug():
    assert mumble_password_secret("home") == "mumble_password_home"
    # Free-text names reach the secrets channel through their slug (ADR 0052).
    assert (
        mumble_password_secret(slugify("Radio Server Demo"))
        == "mumble_password_radio_server_demo"
    )


def test_default_disconnect_combo_pairs_with_the_two_digit_keypad():
    # ADR 0051/0052: the shipped keypad is two-digit (01/02/99), so the disconnect default moved
    # off the old "73" onto the same scheme.
    assert DEFAULT_MUMBLE_DISCONNECT_DTMF == "98"


# --- link_username: the one nick the station presents everywhere -----------------------------


def test_link_username_is_the_callsign_tagged_as_the_station():
    assert link_username("AE9S") == "AE9S (radio-server)"


def test_link_username_without_a_callsign_falls_back_to_the_bare_default():
    # Bench/mock deployments run without a callsign (they never transmit) — keep them connectable.
    assert link_username(None) == "radio-server"
    assert link_username("") == "radio-server"
