"""Channel presets: the model + fail-loud validator, the pure capability split, the apply seam,
and the HTTP API (ADR 0115).

Everything is hardware-free against `MockRadio`. Two axes prove the capability split: a full-control
backend (`supports_cat=True`) honours every field; an audio-only backend (`supports_cat=False`) can't
tune, so `POST /presets/apply` returns the same 501 as `/frequency`. A partial-capability stub (a
`MockRadio` advertising `SET_FREQUENCY`/`SET_MODE` but not `SET_TONE`) exercises the per-field skip path
that no real backend hits today.

The load-bearing proofs:
- `resolve_presets` fails loud on a bad tone / duplicate name / malformed frequency / unknown field /
  bad mode, and returns `()` when dormant.
- `split_preset_fields` reports honoured vs skipped in the machine-readable `Capability` vocabulary.
- `apply_preset` tunes through the existing surface, applying what the backend supports and reporting
  the rest.
- The API: `GET /presets` lists with per-backend honoured fields; `POST /presets/apply` changes state
  and pushes a `status` event; unknown name → 404; mid-TX → 409; mid-scan stops the scan first;
  audio-only → 501.
"""

from __future__ import annotations

import warnings

import pytest

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import (
    CAT_CAPS,
    FULL_CAPS,
    SHARED_CAPS,
    MockRadio,
    RadioUnavailable,
)
from radio_server.backends.base import Capability, RadioStatus
from radio_server.presets import (
    POWER_LEVELS,
    Preset,
    apply_preset,
    resolve_presets,
    split_preset_fields,
)

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PRESETS = (
    Preset("2m Simplex", 146_520_000),
    Preset("Club Output", 146_940_000, tx_tone=100.0, mode="NFM"),
)


# --- resolve_presets: happy path + fail-loud ---------------------------------------------

def test_resolve_presets_happy_path_and_defaults():
    got = resolve_presets(
        [
            {"name": "2m Simplex", "frequency": 146_520_000},
            {"name": "Rptr", "frequency": 146_940_000, "tx_tone": 100.0, "mode": "nfm"},
        ]
    )
    # mode defaults to FM and is upper-cased; tone omitted → None.
    assert got == (
        Preset("2m Simplex", 146_520_000, tx_tone=None, mode="FM"),
        Preset("Rptr", 146_940_000, tx_tone=100.0, mode="NFM"),
    )


def test_resolve_presets_empty_is_dormant():
    assert resolve_presets(None) == ()
    assert resolve_presets([]) == ()


def test_resolve_presets_rejects_non_ctcss_tone():
    with pytest.raises(RuntimeError, match="not a standard CTCSS tone"):
        resolve_presets([{"name": "x", "frequency": 146_520_000, "tx_tone": 100.5}])


def test_resolve_presets_rejects_duplicate_name_case_insensitively():
    with pytest.raises(RuntimeError, match="collides"):
        resolve_presets(
            [
                {"name": "Home", "frequency": 146_520_000},
                {"name": "home", "frequency": 146_940_000},
            ]
        )


@pytest.mark.parametrize("freq", [-5, 0, "146520000", 146.5, True])
def test_resolve_presets_rejects_malformed_frequency(freq):
    with pytest.raises(RuntimeError, match="frequency"):
        resolve_presets([{"name": "x", "frequency": freq}])


def test_resolve_presets_requires_frequency():
    with pytest.raises(RuntimeError, match="frequency is required"):
        resolve_presets([{"name": "x"}])


def test_resolve_presets_rejects_bad_mode():
    """`mode = "AM"` is the mistake the two fields exist to keep apart, so it stays the fixture.

    `mode` is bandwidth (FM/NFM) and `modulation` is the demodulator (FM/AM) — see
    :func:`test_a_preset_can_ask_for_am_and_it_is_not_the_same_field_as_mode`. Writing the
    demodulator into `mode` must fail loud rather than be quietly accepted as a synonym.
    """
    with pytest.raises(RuntimeError, match="mode"):
        resolve_presets([{"name": "x", "frequency": 146_520_000, "mode": "AM"}])


def test_resolve_presets_rejects_unknown_field():
    with pytest.raises(RuntimeError, match="unknown field"):
        resolve_presets([{"name": "x", "frequency": 146_520_000, "offset": 600000}])


def test_resolve_presets_rejects_blank_and_overlong_name():
    with pytest.raises(RuntimeError, match="name"):
        resolve_presets([{"name": "  ", "frequency": 146_520_000}])
    with pytest.raises(RuntimeError, match="name"):
        resolve_presets([{"name": "x" * 65, "frequency": 146_520_000}])


# --- split_preset_fields: the pure honoured/skipped split --------------------------------

def test_split_full_caps_honours_every_present_field():
    honoured, skipped = split_preset_fields(PRESETS[1], FULL_CAPS)
    assert honoured == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert skipped == []


def test_split_omits_tone_when_preset_has_none():
    # A tone-less preset never reports a tone gap, even on a backend without SET_TONE.
    honoured, skipped = split_preset_fields(PRESETS[0], SHARED_CAPS)
    assert honoured == []
    assert {s["field"] for s in skipped} == {"frequency", "mode", "modulation"}


def test_split_partial_caps_reports_tone_skipped():
    partial = frozenset({Capability.SET_FREQUENCY, Capability.SET_MODE})
    honoured, skipped = split_preset_fields(PRESETS[1], partial)
    assert honoured == ["set_frequency", "set_mode"]
    assert skipped == [
        {"field": "modulation", "capability": "set_modulation"},
        {"field": "tx_tone", "capability": "set_tone"},
    ]


def test_split_audio_only_skips_all_present_fields():
    honoured, skipped = split_preset_fields(PRESETS[1], SHARED_CAPS)
    assert honoured == []
    assert {s["capability"] for s in skipped} == {
        "set_frequency", "set_mode", "set_modulation", "set_tone",
    }


# --- apply_preset: the seam over the existing radio surface ------------------------------

def test_apply_preset_tunes_full_backend():
    radio = MockRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, PRESETS[1])
    assert applied == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert skipped == []
    st = radio.status()
    assert (st.frequency, st.mode, st.tone) == (146_940_000, "NFM", 100.0)


class _PartialCatRadio(MockRadio):
    """A CAT backend that tunes and sets mode but has no CTCSS — the capability gap no real backend
    has today, so the per-field skip path is testable end-to-end."""

    def capabilities(self):
        return frozenset(FULL_CAPS - {Capability.SET_TONE})

    def set_tone(self, tone):  # pragma: no cover - must never be reached
        raise AssertionError("set_tone must not be called when SET_TONE is unadvertised")


def test_apply_preset_skips_tone_on_partial_backend():
    radio = _PartialCatRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, PRESETS[1])
    assert applied == ["set_frequency", "set_mode", "set_modulation"]
    assert skipped == [{"field": "tx_tone", "capability": "set_tone"}]
    # Frequency + mode DID land; the tone was skipped, not silently attempted.
    assert radio.status().frequency == 146_940_000
    assert radio.status().tone is None


# --- modulation (ADR 0150) ---------------------------------------------------------------

AIRBAND = Preset("Denver Tower", 118_300_000, modulation="AM")


def test_a_preset_can_ask_for_am_and_it_is_not_the_same_field_as_mode():
    """The whole point of the field: airband is AM, and no preset could say so before F7.

    `mode` and `modulation` are different radio settings that both spell one of their values
    `"FM"` — bandwidth versus demodulator — so this pins that an AM preset still carries an
    FM-family bandwidth rather than the two having collapsed into one.
    """
    (got,) = resolve_presets(
        [{"name": "Denver Tower", "frequency": 118_300_000, "modulation": "am"}]
    )
    assert got.modulation == "AM"  # upper-cased at load, like `mode`
    assert got.mode == "FM"        # untouched: this is bandwidth, and it is not AM


def test_a_preset_that_says_nothing_about_modulation_is_fm():
    """Absent means FM, NOT "leave whatever is set" — the opposite of `power` (ADR 0146/0150).

    A demodulator belongs to what you are listening to, so a channel list where one entry is
    airband must return to FM when the operator taps a repeater. If this defaulted to `None` and
    were applied conditionally, that repeater would be inaudible with nothing reporting why.
    """
    (got,) = resolve_presets([{"name": "x", "frequency": 146_520_000}])
    assert got.modulation == "FM"
    assert got.power is None  # the contrast, asserted rather than described


@pytest.mark.parametrize("bad", ["USB", "SSB", "wide", "", "AM/FM"])
def test_resolve_presets_rejects_a_modulation_no_radio_here_accepts(bad):
    """USB included: the wire reserves its number and the firmware refuses the value at F7.
    Accepting it here would produce an `ERR_FIELD` over the air instead of a startup error."""
    with pytest.raises(RuntimeError, match="modulation"):
        resolve_presets([{"name": "x", "frequency": 146_520_000, "modulation": bad}])


def test_an_am_preset_is_applied_and_reported_on_a_capable_backend():
    radio = MockRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, AIRBAND)
    assert "set_modulation" in applied
    assert skipped == []
    st = radio.status()
    assert st.modulation == "AM"
    # And the consequence a host must not have to infer: in AM this radio will not key.
    assert st.tx_ok is False


class _NoModulationRadio(MockRadio):
    """A CAT backend without the demodulator command — every real one but a UV-K5 on F7."""

    def capabilities(self):
        return frozenset(FULL_CAPS - {Capability.SET_MODULATION})

    def set_modulation(self, modulation):  # pragma: no cover - must never be reached
        raise AssertionError("set_modulation must not be called when it is unadvertised")


def test_an_am_preset_on_a_backend_without_it_still_tunes_and_says_what_it_dropped():
    """The honoured/skipped contract where it matters most: the operator gets the frequency, and
    is told plainly that the radio is not demodulating what the channel needs — rather than
    listening to silence on an airband frequency and wondering."""
    radio = _NoModulationRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, AIRBAND)
    assert "set_modulation" not in applied
    assert radio.status().frequency == 118_300_000  # the tune DID land
    assert {"field": "modulation", "capability": "set_modulation"} in skipped


def test_switching_from_an_am_preset_back_to_fm_restores_the_demodulator():
    """The reason it is written unconditionally rather than only when a preset asks for AM."""
    radio = MockRadio(supports_cat=True)
    apply_preset(radio, AIRBAND)
    assert radio.status().modulation == "AM"
    apply_preset(radio, PRESETS[0])  # says nothing about modulation
    assert radio.status().modulation == "FM"
    assert radio.status().tx_ok is True


# --- repeater split + rx_tone (ADR 0133) -------------------------------------------------

REPEATER = Preset(
    "W0CRA 145.46", 145_460_000, tx_frequency=144_860_000, tx_tone=107.2, rx_tone=107.2
)


def test_a_repeater_preset_carries_both_legs_and_derives_its_offset():
    assert REPEATER.offset == -600_000
    assert PRESETS[0].offset is None  # simplex has no offset, not a zero one


def test_offset_is_not_an_input_spelling():
    """Storage is the absolute TX frequency; `offset` is derived. Writing it is a typo, and the
    fail-loud unknown-field rule is what says so (ADR 0133)."""
    with pytest.raises(RuntimeError, match="unknown field"):
        resolve_presets([{"name": "x", "frequency": 145_460_000, "offset": -600_000}])


def test_a_tx_frequency_equal_to_the_receive_one_is_refused():
    with pytest.raises(RuntimeError, match="simplex"):
        resolve_presets(
            [{"name": "x", "frequency": 145_460_000, "tx_frequency": 145_460_000}]
        )


def test_the_old_tone_spelling_fails_with_the_rewrite():
    """A rename, not an alias: say what to change rather than quietly accepting both (ADR 0133)."""
    with pytest.raises(RuntimeError, match=r"tone -> tx_tone"):
        resolve_presets([{"name": "x", "frequency": 146_940_000, "tone": 100.0}])


def test_split_reports_the_tx_leg_skipped_on_a_backend_without_it():
    partial = frozenset(FULL_CAPS - {Capability.SET_SPLIT})
    honoured, skipped = split_preset_fields(REPEATER, partial)
    assert honoured == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert {"field": "tx_frequency", "capability": "set_split"} in skipped


def test_rx_tone_is_always_reported_unhonoured_even_on_a_full_backend():
    """Nothing implements RX tone squelch, so storing it silently would be the dropped-field bug
    guardrail 3 exists to prevent."""
    _honoured, skipped = split_preset_fields(REPEATER, FULL_CAPS)
    rx = [s for s in skipped if s["field"] == "rx_tone"]
    assert len(rx) == 1
    assert rx[0]["capability"] == ""  # no Capability backs it — do not invent one
    assert "not implemented" in rx[0]["reason"]


def test_applying_a_repeater_preset_arms_both_legs():
    radio = MockRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, REPEATER)
    assert applied == ["set_frequency", "set_split", "set_mode", "set_modulation", "set_tone"]
    st = radio.status()
    assert (st.frequency, st.tx_frequency, st.tone) == (145_460_000, 144_860_000, 107.2)
    assert [s["field"] for s in skipped] == ["rx_tone"]


class _NoSplitRadio(MockRadio):
    """A CAT backend without split — kv4p today."""

    def capabilities(self):
        return frozenset(FULL_CAPS - {Capability.SET_SPLIT})

    def set_split(self, tx_hz):  # pragma: no cover - must never be reached
        raise AssertionError("set_split must not be called when SET_SPLIT is unadvertised")


def test_a_repeater_preset_on_a_simplex_backend_still_tunes_and_says_what_it_dropped():
    """The honoured/skipped contract at its most load-bearing: the operator gets the repeater's
    output to listen to, and is told plainly that transmitting through it will not work."""
    radio = _NoSplitRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, REPEATER)
    assert applied == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert radio.status().frequency == 145_460_000  # the RX leg DID land
    assert {"field": "tx_frequency", "capability": "set_split"} in skipped


def test_applying_a_simplex_preset_disarms_a_previous_split():
    """Switching from a repeater back to a simplex channel must not inherit the old TX leg."""
    radio = MockRadio(supports_cat=True)
    apply_preset(radio, REPEATER)
    apply_preset(radio, PRESETS[0])
    assert radio.status().tx_frequency is None


def test_a_tone_less_preset_clears_the_previous_channels_tone():
    """Found on the bench, and it is the same fault as a leaked split: a preset describes a COMPLETE
    channel, so no tone means NO tone — not "keep the last repeater's".

    Applying the tone-less bench preset after a repeater one left `tone: 100.0` still set and riding
    on every subsequent transmission. It went unnoticed while no configured preset had a tone; with a
    channel list full of repeaters on different tones it is a live hazard (ADR 0133).
    """
    radio = MockRadio(supports_cat=True)
    apply_preset(radio, REPEATER)
    assert radio.status().tone == 107.2
    applied, _ = apply_preset(radio, PRESETS[0])
    assert radio.status().tone is None
    assert "set_tone" not in applied  # nothing was set, so nothing is reported as applied


# --- HTTP API ----------------------------------------------------------------------------

def _client(radio: MockRadio, presets=PRESETS) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(radio, api_token=TOKEN, presets=presets))


def test_get_presets_lists_with_honoured_fields_on_cat_backend():
    body = _client(MockRadio(supports_cat=True)).get("/presets", headers=AUTH).json()
    names = [p["name"] for p in body["presets"]]
    assert names == ["2m Simplex", "Club Output"]
    club = body["presets"][1]
    assert club["frequency"] == 146_940_000
    assert club["tx_tone"] == 100.0
    assert club["honoured"] == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert club["unsupported"] == []


def test_get_presets_reports_all_unsupported_on_audio_only():
    body = _client(MockRadio(supports_cat=False)).get("/presets", headers=AUTH).json()
    club = body["presets"][1]
    assert club["honoured"] == []
    assert {u["capability"] for u in club["unsupported"]} == {
        "set_frequency",
        "set_mode",
        "set_modulation",
        "set_tone",
    }
    assert not set(club["honoured"]) & {str(c) for c in CAT_CAPS}


def test_get_presets_empty_when_none_configured():
    body = _client(MockRadio(), presets=()).get("/presets", headers=AUTH).json()
    assert body == {"presets": []}


def test_apply_preset_changes_state_and_reports_applied():
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post("/presets/apply", json={"name": "Club Output"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == ["set_frequency", "set_mode", "set_modulation", "set_tone"]
    assert body["skipped"] == []
    assert body["status"]["frequency"] == 146_940_000
    assert radio.status().frequency == 146_940_000


def test_apply_preset_is_case_insensitive():
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post("/presets/apply", json={"name": "club output"}, headers=AUTH)
    assert resp.status_code == 200
    assert radio.status().frequency == 146_940_000


def test_apply_unknown_preset_is_404():
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post("/presets/apply", json={"name": "nope"}, headers=AUTH)
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
    assert radio.status().frequency is None


def test_apply_preset_reports_a_radio_that_is_not_answering_as_503_with_the_reason():
    """The operator's failure, end to end.

    A UV-K5 that has been switched off (or power-cycled mid-session) raises `TuneError`, and
    `TuneError` used to be caught nowhere — so the web UI showed
    `Request failed (500): Internal Server Error`, which tells the one person standing next to
    the radio nothing they can act on. The backend's sentence has to survive to the client.
    """
    class RadioThatIsNotThere(MockRadio):
        def set_frequency(self, hz: int) -> None:
            raise RadioUnavailable(
                "the radio did not answer the handshake — check it is powered on"
            )

    radio = RadioThatIsNotThere(supports_cat=True)
    resp = _client(radio).post("/presets/apply", json={"name": "Club Output"}, headers=AUTH)
    assert resp.status_code == 503
    assert "powered on" in resp.json()["detail"]


def test_set_frequency_reports_a_radio_that_is_not_answering_as_503():
    """Registered app-wide, so the single-field routes get it without their own try/except."""
    class RadioThatIsNotThere(MockRadio):
        def set_frequency(self, hz: int) -> None:
            raise RadioUnavailable("the radio stopped answering — check the AIOC cable")

    resp = _client(RadioThatIsNotThere(supports_cat=True)).post(
        "/frequency", json={"hz": 146_520_000}, headers=AUTH
    )
    assert resp.status_code == 503
    assert "AIOC" in resp.json()["detail"]


def test_apply_preset_501_on_audio_only_names_set_frequency():
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post("/presets/apply", json={"name": "2m Simplex"}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_frequency"


def test_apply_preset_pushes_a_status_event():
    radio = MockRadio(supports_cat=True)
    with _client(radio) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            ws.receive_json()  # the initial status snapshot on connect
            client.post("/presets/apply", json={"name": "Club Output"}, headers=AUTH)
            evt = ws.receive_json()
    assert evt["type"] == "status"
    assert evt["data"]["frequency"] == 146_940_000


def test_apply_preset_refused_409_while_transmitting():
    radio = MockRadio(supports_cat=True)
    with _client(radio) as client:
        client.app.state.arbiter.acquire_tx()
        resp = client.post("/presets/apply", json={"name": "Club Output"}, headers=AUTH)
    assert resp.status_code == 409
    assert "transmitting" in resp.json()["detail"]
    # Refused, not partially applied.
    assert radio.status().frequency is None


def test_apply_preset_stops_a_running_scan_first():
    radio = MockRadio(supports_cat=True)
    with _client(radio) as client:
        client.post("/scan", json={"frequencies": [145_000_000, 146_000_000]}, headers=AUTH)
        assert client.app.state.scan_runner.running is True
        resp = client.post("/presets/apply", json={"name": "2m Simplex"}, headers=AUTH)
        assert resp.status_code == 200
        # The scan was stopped before tuning, and the preset frequency won.
        assert client.app.state.scan_runner.running is False
    assert radio.status().frequency == 146_520_000


def test_apply_preset_422_on_backend_valueerror():
    class _BandLimitedRadio(MockRadio):
        def set_frequency(self, hz):
            raise ValueError("frequency out of band for this radio")

    radio = _BandLimitedRadio(supports_cat=True)
    resp = _client(radio).post("/presets/apply", json={"name": "2m Simplex"}, headers=AUTH)
    assert resp.status_code == 422
    assert "2m Simplex" in resp.json()["detail"]


# --- transmit power (ADR 0146) -----------------------------------------------------------------

def test_power_is_optional_and_absent_means_leave_the_station_alone():
    """The one field where absent is NOT "off". The split and the tone belong to the channel, so a
    preset omitting them means none; a power level belongs to the station, so omitting it means
    "however I am set" — and forcing a default would undo the operator's own choice on every tap."""
    (preset,) = resolve_presets([{"name": "Bench", "frequency": 445_800_000}])
    assert preset.power is None

    (quiet,) = resolve_presets([{"name": "Bench", "frequency": 445_800_000, "power": "LOW"}])
    assert quiet.power == "low"          # normalised, like mode


def test_a_bad_power_level_fails_loud_at_load():
    with pytest.raises(RuntimeError, match="power='turbo'"):
        resolve_presets([{"name": "x", "frequency": 445_800_000, "power": "turbo"}])


def test_the_preset_power_vocabulary_matches_the_backend_enum():
    """Two spellings of the same three levels, kept apart so presets do not import a backend
    (the `CTCSS_TONES` rule). A test is what stops them drifting."""
    from radio_server.backends.uvk5.vfo import PowerLevel

    assert POWER_LEVELS == {level.value for level in PowerLevel}


def test_a_preset_power_is_applied_and_reported():
    radio = MockRadio(supports_cat=True)
    applied, skipped = apply_preset(
        radio, Preset(name="Quiet", frequency=445_800_000, power="low")
    )
    assert str(Capability.SET_POWER) in applied
    assert radio.status().power == "low"
    assert not any(s["field"] == "power" for s in skipped)


def test_a_preset_without_power_does_not_touch_the_level():
    """Tapping a channel that says nothing about power must not put it back to a default."""
    radio = MockRadio(supports_cat=True)
    apply_preset(radio, Preset(name="Quiet", frequency=445_800_000, power="low"))
    apply_preset(radio, Preset(name="Other", frequency=446_000_000))
    assert radio.status().power == "low"


def test_power_is_reported_skipped_on_a_backend_that_cannot_set_it():
    """Guardrail 3: named, never silently dropped — an operator who set a channel to low power on
    a radio that cannot do it needs to know it is transmitting at whatever it was."""
    audio_only = MockRadio(supports_cat=False)
    _honoured, skipped = split_preset_fields(
        Preset(name="Quiet", frequency=445_800_000, power="low"), audio_only.capabilities()
    )
    assert {"field": "power", "capability": "set_power"} in skipped
