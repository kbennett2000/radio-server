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
from radio_server.backends import CAT_CAPS, FULL_CAPS, SHARED_CAPS, MockRadio
from radio_server.backends.base import Capability, RadioStatus
from radio_server.presets import (
    Preset,
    apply_preset,
    resolve_presets,
    split_preset_fields,
)

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PRESETS = (
    Preset("2m Simplex", 146_520_000),
    Preset("Club Output", 146_940_000, tone=100.0, mode="NFM"),
)


# --- resolve_presets: happy path + fail-loud ---------------------------------------------

def test_resolve_presets_happy_path_and_defaults():
    got = resolve_presets(
        [
            {"name": "2m Simplex", "frequency": 146_520_000},
            {"name": "Rptr", "frequency": 146_940_000, "tone": 100.0, "mode": "nfm"},
        ]
    )
    # mode defaults to FM and is upper-cased; tone omitted → None.
    assert got == (
        Preset("2m Simplex", 146_520_000, tone=None, mode="FM"),
        Preset("Rptr", 146_940_000, tone=100.0, mode="NFM"),
    )


def test_resolve_presets_empty_is_dormant():
    assert resolve_presets(None) == ()
    assert resolve_presets([]) == ()


def test_resolve_presets_rejects_non_ctcss_tone():
    with pytest.raises(RuntimeError, match="not a standard CTCSS tone"):
        resolve_presets([{"name": "x", "frequency": 146_520_000, "tone": 100.5}])


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
    assert honoured == ["set_frequency", "set_mode", "set_tone"]
    assert skipped == []


def test_split_omits_tone_when_preset_has_none():
    # A tone-less preset never reports a tone gap, even on a backend without SET_TONE.
    honoured, skipped = split_preset_fields(PRESETS[0], SHARED_CAPS)
    assert honoured == []
    assert {s["field"] for s in skipped} == {"frequency", "mode"}


def test_split_partial_caps_reports_tone_skipped():
    partial = frozenset({Capability.SET_FREQUENCY, Capability.SET_MODE})
    honoured, skipped = split_preset_fields(PRESETS[1], partial)
    assert honoured == ["set_frequency", "set_mode"]
    assert skipped == [{"field": "tone", "capability": "set_tone"}]


def test_split_audio_only_skips_all_present_fields():
    honoured, skipped = split_preset_fields(PRESETS[1], SHARED_CAPS)
    assert honoured == []
    assert {s["capability"] for s in skipped} == {"set_frequency", "set_mode", "set_tone"}


# --- apply_preset: the seam over the existing radio surface ------------------------------

def test_apply_preset_tunes_full_backend():
    radio = MockRadio(supports_cat=True)
    applied, skipped = apply_preset(radio, PRESETS[1])
    assert applied == ["set_frequency", "set_mode", "set_tone"]
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
    assert applied == ["set_frequency", "set_mode"]
    assert skipped == [{"field": "tone", "capability": "set_tone"}]
    # Frequency + mode DID land; the tone was skipped, not silently attempted.
    assert radio.status().frequency == 146_940_000
    assert radio.status().tone is None


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
    assert club["tone"] == 100.0
    assert club["honoured"] == ["set_frequency", "set_mode", "set_tone"]
    assert club["unsupported"] == []


def test_get_presets_reports_all_unsupported_on_audio_only():
    body = _client(MockRadio(supports_cat=False)).get("/presets", headers=AUTH).json()
    club = body["presets"][1]
    assert club["honoured"] == []
    assert {u["capability"] for u in club["unsupported"]} == {
        "set_frequency",
        "set_mode",
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
    assert body["applied"] == ["set_frequency", "set_mode", "set_tone"]
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
