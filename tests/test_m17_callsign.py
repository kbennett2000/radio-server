"""M17 base-40 callsign encoding (ADR 0050): golden vectors, exact round-trip, fail-loud.

The wire format is exact (unlike the lossy Codec2 seam), so these assert exact bytes. The golden
vectors come straight from the M17 specification's address-encoding appendix; the fail-loud cases
are this project's deliberate divergence from the spec's coercing reference encoder.
"""

import pytest

from radio_server.link.m17.callsign import (
    ALPHABET,
    BROADCAST,
    EMPTY,
    MAX_CALLSIGN_LEN,
    STANDARD_MAX,
    CallsignError,
    decode_callsign,
    encode_callsign,
)


# --- golden vectors (from the spec) ------------------------------------------


def test_spec_golden_vectors():
    # "A" -> 1, "AB1CD" -> 0x9FDD51 (the spec's worked example). Emitted big-endian in 6 bytes.
    assert encode_callsign("A") == (1).to_bytes(6, "big")
    assert encode_callsign("AB1CD") == (0x9FDD51).to_bytes(6, "big")
    assert decode_callsign((0x9FDD51).to_bytes(6, "big")) == "AB1CD"


# --- round-trip over the whole alphabet --------------------------------------


@pytest.mark.parametrize(
    "callsign",
    [
        "W1AW",       # letters + digit
        "KE0ABC",
        "AB1CD",
        "N0CALL",
        "M17-M17",    # hyphen
        "A/P",        # slash
        "K.9",        # dot
        "ABCDEFGHI",  # exactly 9 chars, max length
        "0123456789"[:9],
        "-/.",        # the three punctuation symbols
    ],
)
def test_round_trip_exact(callsign):
    assert decode_callsign(encode_callsign(callsign)) == callsign


def test_every_alphabet_character_round_trips():
    # Skip index 0 (space): trailing/leading spaces are no-ops, so they don't survive a round-trip.
    for ch in ALPHABET[1:]:
        assert decode_callsign(encode_callsign(ch)) == ch


# --- reserved / broadcast values ---------------------------------------------


def test_empty_and_space_only_map_to_reserved_zero():
    assert encode_callsign("") == EMPTY
    assert encode_callsign("   ") == EMPTY  # space has value 0
    assert decode_callsign(EMPTY) == ""


def test_trailing_spaces_do_not_change_the_address():
    assert encode_callsign("ABC") == encode_callsign("ABC      ")


def test_broadcast_is_not_a_decodable_callsign():
    assert BROADCAST == b"\xff" * 6
    assert decode_callsign(BROADCAST) is None  # caller compares raw bytes to BROADCAST


def test_extended_range_address_decodes_to_none():
    # Just above the standard range (40**9): valid 6 bytes, but not a base-40 string.
    extended = (STANDARD_MAX + 1).to_bytes(6, "big")
    assert decode_callsign(extended) is None


# --- fail loud (our divergence from the spec's coercing encoder) -------------


def test_length_over_nine_fails_loud():
    with pytest.raises(CallsignError) as excinfo:
        encode_callsign("A" * (MAX_CALLSIGN_LEN + 1))
    assert "maximum" in str(excinfo.value)


def test_invalid_character_fails_loud_by_name():
    with pytest.raises(CallsignError) as excinfo:
        encode_callsign("W1@X")
    msg = str(excinfo.value)
    assert "'@'" in msg
    assert "alphabet" in msg


def test_lowercase_is_rejected_not_coerced():
    # The spec's reference encoder folds lowercase to uppercase; we fail loud instead, so that a
    # round-trip is exact and a misconfigured callsign never silently changes case on the air.
    with pytest.raises(CallsignError):
        encode_callsign("w1aw")


def test_decode_wrong_length_is_local_misuse_and_raises():
    with pytest.raises(CallsignError):
        decode_callsign(b"\x00" * 5)
