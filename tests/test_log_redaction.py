"""The API token stops appearing in the journal (ADR 0165).

The leak is not ours: uvicorn's `get_path_with_query_string` appends the query string verbatim, and
`websockets_sansio_impl.py` logs it at INFO on every accept — so `?token=<secret>` lands in journald
once per socket connect. Measured on the live station: **1072 lines in 72 hours.** `journalctl`
output gets pasted into PRs and ADRs here, and both repos are public.

Three instruments, weakest to strongest:

- **Unit** — `redact()` against every shape a credential arrives in, including the two the design
  deliberately does not cover (a `fixed_code` short enough that redacting its value would blank
  unrelated numbers, and any secret under `MIN_VALUE_LENGTH`).
- **Wiring** — `__main__.main()` with `uvicorn.run` stubbed, proving the factory is installed by the
  real entrypoint rather than by a test.
- **Integration** — a real `uvicorn.Server` on a real socket with uvicorn's own `LOGGING_CONFIG`,
  connected by a real WebSocket client, asserting on what the handlers actually emitted. This is the
  only one that proves the fix is in the right place; the other two would stay green if the filter
  were installed somewhere uvicorn's loggers never reach.

A hand-rolled handshake was deliberately not used for the integration leg: uvicorn logs *nothing*
when `ServerProtocol.accept()` rejects a malformed upgrade, so "the token is not in the output"
would pass because nothing was written at all. A negative assertion that holds because the event
never happened is the worst kind of green.
"""

from __future__ import annotations

import logging
import logging.config
import threading
import time
from io import StringIO

import pytest
import uvicorn
import uvicorn.config
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from radio_server import logsafe
from radio_server.api import create_app
from radio_server.backends import MockRadio

from .conftest import make_secrets

#: Long enough to clear `MIN_VALUE_LENGTH`, and shaped like the real thing (`token_urlsafe`).
TOKEN = "b3nch-Sekr1t-Value-For-Tests-0123456789ab"
#: Under the floor, and a substring of ordinary log text — the reason the floor exists.
SHORT_TOKEN = "radio-serv"


@pytest.fixture(autouse=True)
def _restore_logging():
    """Every test here mutates process-global logging state. Put all of it back.

    The record factory, the root handlers (`basicConfig` is not idempotent-safe for us) and
    uvicorn's own loggers are all global; leaking any of them into the other 2100 tests would be a
    debugging nightmare for whoever hit it next.
    """
    factory = logging.getLogRecordFactory()
    root = logging.getLogger()
    root_handlers, root_level = list(root.handlers), root.level
    uvicorn_handlers = {
        name: list(logging.getLogger(name).handlers)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    try:
        yield
    finally:
        logsafe.uninstall()
        logging.setLogRecordFactory(factory)
        root.handlers[:] = root_handlers
        root.setLevel(root_level)
        for name, handlers in uvicorn_handlers.items():
            logging.getLogger(name).handlers[:] = handlers


# --- unit: the transformation ---------------------------------------------------------------


def test_a_websocket_url_loses_its_token():
    logsafe.install()
    assert logsafe.redact("/audio/rx?token=" + TOKEN) == "/audio/rx?token=<redacted>"


def test_the_query_string_survives_around_the_redaction():
    # `&`-terminated, so a path with other parameters stays diagnosable — the point of redacting
    # rather than dropping the line.
    logsafe.install()
    got = logsafe.redact(f"/audio/rx?token={TOKEN}&fmt=pcm")
    assert got == "/audio/rx?token=<redacted>&fmt=pcm"


def test_a_bearer_header_loses_its_credential():
    logsafe.install()
    assert logsafe.redact(f"Authorization: Bearer {TOKEN}") == "Authorization: Bearer <redacted>"


@pytest.mark.parametrize("name", ["api_token", "totp_secret", "password", "mumble_password_home"])
def test_every_credential_name_in_the_registry_is_covered(name):
    # The name list is derived from `KNOWN_SECRETS` + `MUMBLE_PASSWORD_PREFIX`, so adding a secret
    # to `config/secrets.py` extends the redaction without anyone remembering to come here.
    logsafe.install()
    assert logsafe.redact(f"{name}={TOKEN}") == f"{name}=<redacted>"


def test_a_registered_value_is_redacted_even_with_no_name_in_front_of_it():
    # The backstop under the name layer: `websockets` logs a header name and its value as two
    # separate args, which no `name=value` pattern can span.
    logsafe.install()
    logsafe.register_secret_values(make_secrets(api_token=TOKEN))
    assert TOKEN not in logsafe.redact(f"connected with {TOKEN} ok")


def test_a_short_secret_is_not_registered_by_value():
    # `radio-serv` is 10 characters and a substring of `/etc/radio-server/radio.toml`. Value-
    # redacting it would corrupt ordinary log text everywhere. The floor is why the name layer,
    # not the value layer, is what actually protects the WebSocket line.
    logsafe.install()
    logsafe.register_secret_values(make_secrets(api_token=SHORT_TOKEN))
    assert logsafe.redact("/etc/radio-server/radio.toml") == "/etc/radio-server/radio.toml"


def test_a_short_secret_is_still_redacted_by_name():
    logsafe.install()
    logsafe.register_secret_values(make_secrets(api_token=SHORT_TOKEN))
    assert logsafe.redact(f"?token={SHORT_TOKEN}") == "?token=<redacted>"


def test_the_fixed_code_is_excluded_from_value_redaction_by_name():
    # A static 6-digit code is low-security by nature (`config/secrets.py`), and registering it
    # would blank every matching frequency, byte count and duration in the journal.
    logsafe.install()
    armed = logsafe.register_secret_values(make_secrets(api_token=TOKEN, fixed_code="147555"))
    assert "fixed_code" not in armed
    assert logsafe.redact("tuned to 147555 kHz") == "tuned to 147555 kHz"


def test_install_reports_which_secrets_it_armed_and_never_their_values(caplog):
    logsafe.install()
    with caplog.at_level(logging.INFO):
        logsafe.register_secret_values(make_secrets(api_token=TOKEN, fixed_code="147555"))
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "api_token" in said and TOKEN not in said


# --- unit: the record factory, and the arity trap -------------------------------------------


def _emit(logger_name: str, msg, *args) -> str:
    """Log through a real handler and return exactly what it wrote.

    Every mutation is restored, `propagate` included: leaving `uvicorn.error` non-propagating would
    silently mute the integration tests below, since it owns no handler of its own and reaches the
    output only through the `uvicorn` logger's.
    """
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(logger_name)
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(msg, *args)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
    return stream.getvalue()


def test_the_uvicorn_websocket_accept_line_comes_out_redacted():
    logsafe.install()
    got = _emit("uvicorn.error", '%s - "WebSocket %s" [accepted]', "10.0.0.9:5555",
                f"/audio/rx?token={TOKEN}")
    assert TOKEN not in got
    assert "token=<redacted>" in got


def test_redacting_a_bare_message_never_dumps_the_arguments(capsys):
    """The trap this design is built around.

    Rewriting `record.msg` while `record.args` is non-empty can delete a `%s`. `logging` then calls
    `Handler.handleError`, which prints `Message:` *and* `Arguments: (<the raw secret>,)` straight
    to stderr — turning a redaction into a guaranteed cleartext dump of the very thing it was
    protecting. So `msg` is only rewritten when there are no args.
    """
    logsafe.install()
    got = _emit("radio_server.test", "api_token=%s", TOKEN)
    err = capsys.readouterr().err
    assert "--- Logging error ---" not in err and TOKEN not in err
    assert TOKEN not in got


def test_argument_arity_and_types_survive():
    # `uvicorn.logging.AccessFormatter` unpacks `record.args` as a 5-tuple and calls
    # `int(status_code)`. Rebuilding args as anything but a same-length tuple breaks every
    # access-log line in the process.
    logsafe.install()
    seen = {}

    class Capture(logging.Handler):
        def emit(self, record):
            seen["args"] = record.args

    logger = logging.getLogger("radio_server.arity")
    logger.addHandler(Capture())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:1", "GET", f"/x?token={TOKEN}", "1.1", 403)
    assert len(seen["args"]) == 5
    assert seen["args"][4] == 403 and isinstance(seen["args"][4], int)
    assert TOKEN not in seen["args"][2]


def test_a_dict_argument_is_rebuilt_rather_than_mutated():
    # `LogRecord.__init__` unwraps a single Mapping arg — and that mapping is the caller's live
    # object, not a copy. Redacting it in place would corrupt caller state, not just the log line.
    logsafe.install()
    original = {"url": f"/audio/rx?token={TOKEN}"}
    got = _emit("radio_server.dicts", "%(url)s", original)
    assert TOKEN not in got
    assert original["url"].endswith(TOKEN), "the caller's dict was mutated"


def test_non_string_arguments_pass_through_untouched():
    logsafe.install()
    got = _emit("radio_server.types", "%s %s %s", 42, None, b"\x00\x01")
    assert "42" in got


def test_installing_twice_does_not_wrap_twice():
    logsafe.install()
    first = logging.getLogRecordFactory()
    assert logsafe.install() is False
    assert logging.getLogRecordFactory() is first


# --- wiring: the real entrypoint installs it ------------------------------------------------


def test_the_entrypoint_installs_redaction_before_it_serves(tmp_path, monkeypatch):
    """`python -m radio_server` is the only way this server is ever started (every documented and
    deployed `ExecStart`), so `main()` is where the guarantee has to be made."""
    from radio_server import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RADIO_API_TOKEN", TOKEN)
    served = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: served.update(kw, app=app))

    entry.main(["--config", str(tmp_path / "absent.toml"),
                "--secrets", str(tmp_path / "absent-secrets.toml")])

    assert served, "uvicorn.run was never reached"
    assert logsafe.installed() is True
    # Found by running the real entrypoint rather than by a test: `basicConfig` used to sit just
    # above `uvicorn.run`, so everything logged during startup — the arming report included — was
    # emitted before root had a handler and went nowhere. A diagnostic nobody can read is not a
    # diagnostic.
    assert logging.getLogger().handlers, "root has no handler by the time startup logs"
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    got = _emit("uvicorn.error", '%s - "WebSocket %s" [accepted]', "10.0.0.9:1",
                f"/events?token={TOKEN}")
    assert TOKEN not in got


# --- integration: a real server, a real socket, uvicorn's real logging config ----------------


class _Bench:
    """A real uvicorn on a real ephemeral port, with its own log handlers writing to a buffer."""

    def __init__(self, app):
        self._config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="info")
        self._config.load()  # runs uvicorn's dictConfig — the real LOGGING_CONFIG
        self.log = StringIO()
        for name in ("uvicorn", "uvicorn.access"):
            for handler in logging.getLogger(name).handlers:
                handler.setStream(self.log)
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self._server.started, "uvicorn did not start"
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10.0)

    def url(self, path: str, token: str) -> str:
        return f"ws://127.0.0.1:{self.port}{path}?token={token}"


def test_a_real_authenticated_socket_leaves_no_token_in_the_log():
    logsafe.install()
    with _Bench(create_app(MockRadio(), api_token=TOKEN)) as bench:
        with connect(bench.url("/audio/rx", TOKEN), open_timeout=10) as ws:
            ws.recv()  # the JSON format header, so the accept has definitely been logged
        written = bench.log.getvalue()
    assert "WebSocket /audio/rx" in written, "the accept line was never logged — vacuous assertion"
    assert TOKEN not in written
    assert "token=<redacted>" in written


def test_a_rejected_socket_leaves_no_token_either():
    """A *wrong* token is journaled too — uvicorn logs the 403 it writes for a pre-accept close.
    That is somebody's credential (a typo of the real one, or another station's), so it is the
    same defect wearing a different status code."""
    wrong = "wr0ng-" + TOKEN
    logsafe.install()
    with _Bench(create_app(MockRadio(), api_token=TOKEN)) as bench:
        with pytest.raises(InvalidStatus):
            connect(bench.url("/audio/rx", wrong), open_timeout=10)
        written = bench.log.getvalue()
    assert "WebSocket /audio/rx" in written, "the rejection line was never logged"
    assert wrong not in written
    assert "token=<redacted>" in written


def test_a_plain_http_request_loses_its_query_string_token_too():
    """REST authenticates by header, so this path does not leak today. It is one pasted URL away
    from doing so — a `wss://` address typed into a browser bar arrives here — and it runs through
    a different logger (`uvicorn.access`) and a different formatter (`AccessFormatter`) than the
    WebSocket line, so covering one is not evidence for the other."""
    import urllib.error
    import urllib.request

    logsafe.install()
    with _Bench(create_app(MockRadio(), api_token=TOKEN)) as bench:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{bench.port}/status?token={TOKEN}", timeout=10)
        except urllib.error.HTTPError:
            pass  # 401: the header is what counts. The access line is logged either way.
        written = bench.log.getvalue()
    assert '"GET /status' in written, "the access line was never logged"
    assert TOKEN not in written
    assert "token=<redacted>" in written
