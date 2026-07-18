"""The live backend switch endpoints (ADR 0076): `POST /radio/select` + `GET /radio/backends`.

Driven over `create_app` with an injected fake `radio_factory` (keyed on `server.backend`) so no real
hardware is constructed, a config that declares two backend blocks (so `configured_backends` offers a
choice), and a temp `config_path` so the persisted write lands in a throwaway file. The proofs: a
select swaps the active backend and re-emits the new capability set over `/events`; an unconfigured
name is refused (409) without touching the running radio; a backend that fails to open rolls back
(503) and leaves the previous one live and the config unwritten; and a successful select persists
`server.backend` while preserving the rest of `radio.toml`.
"""

from __future__ import annotations

import tomllib

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio, Radio
from radio_server.config import Settings, load_settings
from radio_server.controller import build_controller
from radio_server.services import PLUGINS, StubFetcher, StubTts

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEST_SECRET = "JBSWY3DPEHPK3PXP"

# A config that declares BOTH a baofeng (audio+PTT only) and a kv4p (CAT) block, so both are
# "configured" (presence-based, ADR 0074) and either may be selected. The kv4p block gives the target
# CAT capabilities; a leading comment proves the persisted write preserves the rest of the file.
_CONFIG = """\
# radio-server config — test fixture (this comment must survive a live switch write-back)
[server]
backend = "baofeng"

[baofeng]
serial_port = "/dev/ttyACM0"

[kv4p]
serial_port = "/dev/ttyUSB0"
module_type = "vhf"
"""


def _write_config(tmp_path) -> str:
    path = tmp_path / "radio.toml"
    path.write_text(_CONFIG, encoding="utf-8")
    return str(path)


def _radio_factory(settings: Settings) -> Radio:
    # The fake swap: baofeng is audio+PTT only, kv4p advertises CAT — so a switch visibly changes the
    # capability set. "boom" raises to drive the rollback path.
    name = settings.get("server.backend")
    if name == "boom":
        raise RuntimeError("device did not come up")
    return MockRadio(supports_cat=(name == "kv4p"))


def _client(tmp_path, *, radio_factory=_radio_factory) -> TestClient:
    config_path = _write_config(tmp_path)
    settings = load_settings(config_path)
    radio = radio_factory(settings)  # the initial (baofeng) radio, matching server.backend
    app = create_app(
        radio,
        api_token=TOKEN,
        settings=settings,
        config_path=config_path,
        radio_factory=radio_factory,
    )
    return TestClient(app)


def test_backends_lists_current_and_configured(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/radio/backends", headers=AUTH).json()
    assert body["active"] == "baofeng"
    names = {b["name"] for b in body["backends"]}
    assert names == {"baofeng", "kv4p"}
    # The active backend is baofeng (audio+PTT only) — no CAT tuning caps advertised.
    assert "set_frequency" not in body["active_capabilities"]
    assert body["backends"][0]["active"] is True  # active first (ADR 0074 ordering)


def test_select_swaps_the_active_backend(tmp_path):
    with _client(tmp_path) as client:
        before = set(client.get("/capabilities", headers=AUTH).json())
        assert "set_frequency" not in before

        resp = client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"})
        assert resp.status_code == 200
        assert resp.json()["active"] == "kv4p"

        # The routes now read the newly-selected radio (the nonlocal rebind): /capabilities reflects
        # the kv4p CAT set, and /radio/backends reports kv4p active.
        after = set(client.get("/capabilities", headers=AUTH).json())
        assert "set_frequency" in after and "scan" in after
        assert client.get("/radio/backends", headers=AUTH).json()["active"] == "kv4p"


def test_select_unconfigured_backend_is_refused(tmp_path):
    with _client(tmp_path) as client:
        before = client.get("/capabilities", headers=AUTH).json()
        resp = client.post("/radio/select", headers=AUTH, json={"backend": "v71"})
        assert resp.status_code == 409
        assert "not configured" in resp.json()["detail"]
        # The running radio is untouched.
        assert client.get("/capabilities", headers=AUTH).json() == before
        assert client.get("/radio/backends", headers=AUTH).json()["active"] == "baofeng"


def test_select_failure_rolls_back_and_leaves_previous_backend(tmp_path):
    # A radio_factory that opens baofeng fine but raises for the target: the switch must fail 503 and
    # leave the holder on the previous working radio, with nothing persisted.
    def factory(settings: Settings) -> Radio:
        name = settings.get("server.backend")
        if name == "kv4p":
            raise RuntimeError("kv4p failed to boot")
        return MockRadio(supports_cat=False)

    config_path = _write_config(tmp_path)
    settings = load_settings(config_path)
    app = create_app(
        MockRadio(supports_cat=False),
        api_token=TOKEN,
        settings=settings,
        config_path=config_path,
        radio_factory=factory,
    )
    with TestClient(app) as client:
        before = client.get("/capabilities", headers=AUTH).json()
        resp = client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"})
        assert resp.status_code == 503
        assert "kv4p failed to boot" in resp.json()["detail"]
        # Still live on the previous backend, and the API reflects it.
        assert client.get("/capabilities", headers=AUTH).json() == before
        assert client.get("/radio/backends", headers=AUTH).json()["active"] == "baofeng"
    # Nothing was persisted — the file still names baofeng.
    with open(config_path, "rb") as fh:
        assert tomllib.load(fh)["server"]["backend"] == "baofeng"


def test_select_re_emits_capabilities_over_the_events_socket(tmp_path):
    with _client(tmp_path) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            ws.receive_json()  # the initial status snapshot on connect
            client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"})
            # Drain frames until the capabilities re-emit arrives; its set must carry the CAT caps.
            caps = None
            for _ in range(10):
                frame = ws.receive_json()
                if frame["type"] == "capabilities":
                    caps = set(frame["data"]["capabilities"])
                    break
            assert caps is not None, "no capabilities event was pushed after the switch"
            assert "set_frequency" in caps and "scan" in caps


# A config that ALSO carries a [plugins.*] extra channel (ADR 0051) and a callsign (so a controller
# builds). A local-plugin service gates its `enabled()` on `settings.extra(...)`, so this block is what
# a switch must preserve or the service drops out of the catalog (ADR 0078).
_CONFIG_WITH_PLUGIN = """\
[station]
callsign = "AE9S"

[server]
backend = "baofeng"

[baofeng]
serial_port = "/dev/ttyACM0"

[kv4p]
serial_port = "/dev/ttyUSB0"
module_type = "vhf"

[services]
5 = "testsvc"

[plugins.testsvc]
base_url = "http://svc.local/api"
"""


class _ExtraGatedPlugin:
    """A local-plugin-style service (ADR 0051): it appears in the catalog only when its
    ``[plugins.testsvc]`` config is present — exactly the gate the weather/quote/etc. plugins use."""

    id = "testsvc"
    description = "A local test service"

    def enabled(self, settings) -> bool:
        return bool(settings.extra("testsvc.base_url"))

    def build(self, ctx):
        def service(session, sctx):
            return sctx.tts.render("testsvc speaking")

        return service


def _plugin_client(tmp_path) -> TestClient:
    # Wire a real controller (with the extra-gated local plugin) through the same factory seam the
    # composition root uses, so a switch rebuilds it against the new radio (ADR 0076).
    plugins = PLUGINS + (_ExtraGatedPlugin(),)
    config_path = str(tmp_path / "radio.toml")
    (tmp_path / "radio.toml").write_text(_CONFIG_WITH_PLUGIN, encoding="utf-8")
    settings = load_settings(config_path)

    def controller_factory(cf_settings: Settings, cf_radio: Radio):
        return build_controller(
            cf_settings,
            radio=cf_radio,
            totp_secret=TEST_SECRET,
            tts=StubTts(),
            fetcher=StubFetcher(default={}),
            service_bindings={"5": "testsvc", "01": "station-id", "99": "logout"},
            plugins=plugins,
        )

    radio = _radio_factory(settings)
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=controller_factory(settings, radio),
        controller_factory=controller_factory,
        radio_factory=_radio_factory,
        settings=settings,
        config_path=config_path,
        service_plugins=plugins,
    )
    return TestClient(app)


def test_select_preserves_the_extra_channel_on_app_state(tmp_path):
    # The extra channel (ADR 0051) must survive the switch's patch path (ADR 0078) — asserted
    # directly on the live app.state.settings the whole app reads.
    with _plugin_client(tmp_path) as client:
        assert client.app.state.settings.extra("testsvc.base_url") == "http://svc.local/api"
        assert client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"}).status_code == 200
        # Before the fix this returned None — the switch stripped the plugins channel live.
        assert client.app.state.settings.extra("testsvc.base_url") == "http://svc.local/api"


def test_select_keeps_the_local_plugin_service_in_the_catalog(tmp_path):
    # The exact operator-visible failure (ADR 0078): a switch must NOT shrink the service catalog. The
    # local-plugin service is bound to digit 5; it must survive AIOC->kv4p and back.
    with _plugin_client(tmp_path) as client:
        before = {s["digit"] for s in client.get("/services", headers=AUTH).json()}
        assert "5" in before  # the local plugin is live before the switch

        assert client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"}).status_code == 200
        after = {s["digit"] for s in client.get("/services", headers=AUTH).json()}
        assert "5" in after, "the local-plugin service vanished from the catalog after the switch"

        # And it survives the switch back — the persisted+live settings stay whole across both hops.
        assert client.post("/radio/select", headers=AUTH, json={"backend": "baofeng"}).status_code == 200
        back = {s["digit"] for s in client.get("/services", headers=AUTH).json()}
        assert "5" in back


def test_select_persists_the_selection_preserving_the_rest_of_the_config(tmp_path):
    config_path = _write_config(tmp_path)
    settings = load_settings(config_path)
    app = create_app(
        MockRadio(supports_cat=False),
        api_token=TOKEN,
        settings=settings,
        config_path=config_path,
        radio_factory=_radio_factory,
    )
    with TestClient(app) as client:
        assert client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"}).status_code == 200

    text = open(config_path, encoding="utf-8").read()
    parsed = tomllib.loads(text)
    # The selection was written back through the schema (ADR 0051)...
    assert parsed["server"]["backend"] == "kv4p"
    # ...and the rest of radio.toml is preserved — the other blocks' values and the file's comment.
    assert parsed["baofeng"]["serial_port"] == "/dev/ttyACM0"
    assert parsed["kv4p"]["module_type"] == "vhf"
    assert "this comment must survive a live switch write-back" in text
