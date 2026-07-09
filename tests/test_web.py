"""Same-origin web-UI serving and the mock-CAT toggle (ADR 0022).

The SPA itself is browser-verified (see `web/README.md`); these tests cover the Python seams the
UI depends on, driven through Starlette's `TestClient` against `create_app`/`build_app` — no
server binds, no node build required (a tmp directory stands in for `web/dist`):

- `create_app(web_dir=...)` serves a built bundle at `/`, serves a "build me" placeholder when it
  is unbuilt, and — the load-bearing invariant — never shadows the token-gated API routes.
- `web_dir=None` (the default every prior test uses) leaves the surface unchanged (no `/` route).
- `build_app` honours `RADIO_WEB_DIR`, and `RADIO_MOCK_CAT=off` yields an audio-only mock so the
  capability-greying can be demonstrated without hardware.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from radio_server.api import build_app, create_app
from radio_server.backends import MockRadio

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _built_dir(tmp_path: Path) -> Path:
    """A stand-in for `web/dist`: an index plus one asset."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "index.html").write_text("<div id='root'></div>")
    (tmp_path / "app.js").write_text("console.log('radio');")
    return tmp_path


# --- create_app(web_dir=...) serving ------------------------------------------------------


def test_built_web_dir_serves_spa_and_assets(tmp_path):
    client = TestClient(create_app(MockRadio(), api_token=TOKEN, web_dir=_built_dir(tmp_path)))
    root = client.get("/")
    assert root.status_code == 200
    assert "id='root'" in root.text
    asset = client.get("/app.js")
    assert asset.status_code == 200
    assert "radio" in asset.text


def test_unbuilt_web_dir_serves_placeholder_not_a_crash(tmp_path):
    # Directory exists but has no index.html — the API must still run and tell the operator what to do.
    client = TestClient(create_app(MockRadio(), api_token=TOKEN, web_dir=tmp_path))
    root = client.get("/")
    assert root.status_code == 200
    assert "npm run build" in root.text


def test_static_mount_never_shadows_gated_api(tmp_path):
    # With the SPA mounted at "/", the token-gated API routes must still win and stay gated.
    client = TestClient(create_app(MockRadio(), api_token=TOKEN, web_dir=_built_dir(tmp_path)))
    assert client.get("/status").status_code == 401  # gated, not swallowed by the static mount
    assert client.get("/status", headers=AUTH).status_code == 200
    assert client.get("/capabilities", headers=AUTH).status_code == 200


def test_no_web_dir_leaves_surface_unchanged():
    # The DI-seam default: no "/" route at all, API still gated — the pre-ADR-0022 surface.
    client = TestClient(create_app(MockRadio(), api_token=TOKEN))
    assert client.get("/").status_code == 404
    assert client.get("/status").status_code == 401


# --- build_app env wiring -----------------------------------------------------------------


def _env(tmp_path: Path, **overrides) -> dict[str, str]:
    env = {
        "RADIO_API_TOKEN": TOKEN,
        "RADIO_LOG_PATH": str(tmp_path / "log.jsonl"),
    }
    env.update(overrides)
    return env


def test_build_app_honours_radio_web_dir(tmp_path):
    web = _built_dir(tmp_path / "dist")
    client = TestClient(build_app(_env(tmp_path, RADIO_WEB_DIR=str(web))))
    assert client.get("/").status_code == 200
    assert "id='root'" in client.get("/").text


def test_build_app_web_dir_defaults_gracefully_when_unbuilt(tmp_path):
    # Point RADIO_WEB_DIR at a directory with no bundle: still runnable, placeholder at "/".
    empty = tmp_path / "nothing"
    empty.mkdir()
    client = TestClient(build_app(_env(tmp_path, RADIO_WEB_DIR=str(empty))))
    assert client.get("/").status_code == 200
    assert "npm run build" in client.get("/").text


def test_radio_mock_cat_off_yields_audio_only_mock(tmp_path):
    client = TestClient(build_app(_env(tmp_path, RADIO_MOCK_CAT="off")))
    caps = client.get("/capabilities", headers=AUTH).json()
    assert "set_frequency" not in caps
    # And a CAT call is honestly refused with the named capability (guardrail 3), not a no-op.
    resp = client.post("/frequency", json={"hz": 146_520_000}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_frequency"


def test_radio_mock_cat_on_by_default_is_full_cat(tmp_path):
    client = TestClient(build_app(_env(tmp_path)))
    caps = client.get("/capabilities", headers=AUTH).json()
    assert "set_frequency" in caps
    assert client.post("/frequency", json={"hz": 146_520_000}, headers=AUTH).status_code == 200
