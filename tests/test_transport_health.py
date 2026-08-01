"""A station with a dead serial reader must not report healthy (ADR 0166).

`Uvk5Transport._read_loop` has one fatal path: any exception out of `serial.read()` calls `_fail`
and **returns**. The thread ends, nothing restarts it, and `_reader_error` had no public accessor —
while `AiocBaofeng.status()` is pure attribute reads by design, so `/status` went on answering with a
cached frequency and `tx_ok: true` from a radio that had not spoken since. ADR 0163 caught it once,
by luck, because `deafened_unknown` happened to exist on a cadence that only runs while a bridge
relays.

The cause to keep in mind is not the bench script. `A602RQT5` re-enumerates hourly on the bench box,
and a USB re-enumeration leaves exactly this wreckage with nobody behind it.

Three rules the tests hold to:

1. **Liveness is asked of the thread, never of the radio.** Polling the radio to discover that the
   radio has stopped answering is the failure mode, not the detector — so `alive` must be a local
   check that works with a serial handle that raises on every touch.
2. **`/status` keeps answering 200 with a full body.** It is where a broken station is diagnosed, and
   naive clients discard a 503 body. `/healthz` carries the verdict instead.
3. **Recovery says what it did.** `already_healthy`, `reopened` and `failed` are three different
   facts, and a 200 that means "I tried" is the fault class this repo keeps closing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.backends.base import TransportHealth
from radio_server.backends.uvk5.transport import Uvk5Transport

from .test_uvk5_transport import FakeSerial, FakeSerialError, make_transport, wait_until

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- the transport knows, and says so ---------------------------------------------------------


def test_a_fresh_transport_is_alive():
    transport = make_transport(FakeSerial())
    try:
        assert transport.alive is True
        assert transport.reader_error is None
    finally:
        transport.close()


def test_a_dead_reader_reports_itself_without_touching_the_radio():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        fake.feed(FakeSerialError("device reports readiness to read but returned no data"))
        assert wait_until(lambda: transport.alive is False)
        # The serial handle is now poison — `alive` must not have consulted it.
        fake.dead = True
        assert transport.alive is False
        assert "returned no data" in str(transport.reader_error)
    finally:
        transport.close()


def test_a_closed_transport_is_not_reported_as_a_fault():
    # Teardown is not a defect. A closed transport is deliberately stopped, and reporting it as a
    # dead reader would make every clean shutdown look like the thing this ADR exists to catch.
    transport = make_transport(FakeSerial())
    transport.close()
    assert transport.alive is False
    assert transport.reader_error is None


def test_the_kv4p_transport_answers_the_same_question():
    # The same `_fail`-and-return shape, in a second backend. A liveness surface on one of two
    # identical transports is the drip-feed failure this repo keeps closing.
    from radio_server.backends.kv4p.transport import Kv4pTransport

    assert hasattr(Kv4pTransport, "alive")
    assert hasattr(Kv4pTransport, "reader_error")


# --- reconnect: one shot, and it says what happened -------------------------------------------


def test_reconnect_on_a_live_reader_changes_nothing():
    transport = make_transport(FakeSerial())
    try:
        assert transport.reconnect() == "already_healthy"
        assert transport.alive is True
    finally:
        transport.close()


def _serial_sequence(*handles):
    """A factory handing out `handles` in order — one per open, so a reconnect gets a fresh one."""
    remaining = list(handles)

    def factory(_port=None, _baud=None):
        if not remaining:
            raise OSError(16, "Device or resource busy")
        return remaining.pop(0)

    return factory


def test_reconnect_reopens_a_dead_reader_and_the_new_one_runs():
    first, second = FakeSerial(), FakeSerial()
    transport = Uvk5Transport(_serial_factory=_serial_sequence(first, second))
    try:
        first.feed(FakeSerialError("device vanished"))
        assert wait_until(lambda: transport.alive is False)
        assert transport.reconnect() == "reopened"
        assert transport.alive is True
        assert transport.reader_error is None, "a successful reopen must clear the stored error"
    finally:
        transport.close()


def test_a_reopen_that_cannot_open_the_port_reports_the_reason():
    first = FakeSerial()
    transport = Uvk5Transport(_serial_factory=_serial_sequence(first))
    try:
        first.feed(FakeSerialError("device vanished"))
        assert wait_until(lambda: transport.alive is False)
        with pytest.raises(OSError, match="busy"):
            transport.reconnect()
        assert transport.alive is False, "a failed reopen must not look like a recovery"
    finally:
        transport.close()


def test_a_superseded_reader_cannot_clobber_the_new_one():
    """ADR 0099's reader-generation guard, brought to the dock transport.

    A straggler from the replaced reader can outlive the bounded join. Without a generation tag it
    would store its own death in `_reader_error` *after* the new reader started, so a recovered
    transport would report itself broken and nothing would ever look healthy again.
    """
    first = FakeSerial()
    second = FakeSerial()
    made: list[FakeSerial] = []

    def factory(_port, _baud):
        made.append(first if not made else second)
        return made[-1]

    transport = Uvk5Transport(_serial_factory=factory)
    try:
        first.feed(FakeSerialError("first reader dies"))
        assert wait_until(lambda: transport.alive is False)
        assert transport.reconnect() == "reopened"
        # The old handle raises again, as a straggler on the superseded generation would.
        first.feed(FakeSerialError("straggler dies too"))
        import time

        time.sleep(0.15)
        assert transport.alive is True, "a superseded reader marked the live transport dead"
        assert transport.reader_error is None
    finally:
        transport.close()


# --- the health surface -----------------------------------------------------------------------


def _client(radio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


def test_status_carries_the_transport_block():
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=True, error=None, port="/dev/ttyACM0")
    body = _client(radio).get("/status", headers=AUTH).json()
    assert body["transport"] == {"alive": True, "error": None, "port": "/dev/ttyACM0"}


def test_a_backend_with_no_serial_transport_reports_null_not_healthy():
    # `None` is "there is nothing here to report on", which is the honest answer for the mock and
    # for any audio-only backend. Defaulting it to `{"alive": true}` would invent a healthy reading
    # for a transport that does not exist — the same wrong answer `broadcast_fm` refuses to give.
    body = _client(MockRadio()).get("/status", headers=AUTH).json()
    assert body["transport"] is None


def test_status_still_answers_200_with_a_full_body_when_the_reader_is_dead():
    """The diagnosability rule. `/status` is where a broken station is read, and a 503 body is
    discarded by naive clients — so the fault goes *in* the body, and the body stays complete."""
    radio = MockRadio(supports_cat=True)
    radio.set_frequency(146_520_000)
    radio.transport_health = lambda: TransportHealth(alive=False, error="port went away", port="/dev/ttyACM0")
    response = _client(radio).get("/status", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["transport"]["alive"] is False
    assert body["transport"]["error"] == "port went away"
    # The whole point of not 503-ing: the diagnosis is still readable. These are the stale cached
    # fields, and knowing they are stale is what the transport block is for.
    assert body["frequency"] == 146_520_000
    assert body["backend"] == "mock"


def test_healthz_is_200_on_a_healthy_station():
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=True, error=None, port="/dev/ttyACM0")
    response = _client(radio).get("/healthz", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_healthz_is_503_when_the_reader_is_dead():
    """The brief's test. A dead reader is not a degraded state, it is a broken one."""
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=False, error="multiple access on port", port="/dev/ttyACM0")
    response = _client(radio).get("/healthz", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "multiple access on port" in str(response.json())


def test_healthz_is_200_on_a_backend_with_no_transport_to_report_on():
    # A mock station is not broken; it has nothing to be broken. `null` must not read as a fault
    # any more than it reads as healthy.
    assert _client(MockRadio()).get("/healthz", headers=AUTH).status_code == 200


def test_the_events_snapshot_carries_the_block_so_a_new_tab_is_not_told_healthy():
    """A tab opened *after* the reader died gets one on-connect snapshot and then silence until
    something else publishes. If the block is missing from that snapshot, the UI shows a healthy
    station indefinitely."""
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=False, error="gone", port="/dev/ttyACM0")
    with _client(radio) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            event = ws.receive_json()
    assert event["type"] == "status"
    assert event["data"]["transport"]["alive"] is False


# --- the reconnect route ----------------------------------------------------------------------


def test_reconnect_route_reports_already_healthy_without_pretending_to_act():
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=True, error=None, port="/dev/ttyACM0")
    radio.reconnect_transport = lambda: "already_healthy"
    response = _client(radio).post("/diagnostics/reconnect", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["outcome"] == "already_healthy"


def test_reconnect_route_reports_a_real_reopen():
    radio = MockRadio(supports_cat=True)
    radio.transport_health = lambda: TransportHealth(alive=True, error=None, port="/dev/ttyACM0")
    radio.reconnect_transport = lambda: "reopened"
    response = _client(radio).post("/diagnostics/reconnect", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["outcome"] == "reopened"


def test_a_failed_reopen_is_a_503_not_a_200():
    """The whole reason this route reports an outcome. A 200 saying `failed` would be read as
    success by everything that checks a status code and by most humans skimming a log."""
    radio = MockRadio(supports_cat=True)

    def boom():
        raise OSError(16, "Device or resource busy")

    radio.transport_health = lambda: TransportHealth(alive=False, error="dead", port="/dev/ttyACM0")
    radio.reconnect_transport = boom
    response = _client(radio).post("/diagnostics/reconnect", headers=AUTH)
    assert response.status_code == 503
    assert "busy" in response.json()["detail"]


def test_reconnect_is_501_on_a_backend_that_has_no_transport():
    response = _client(MockRadio()).post("/diagnostics/reconnect", headers=AUTH)
    assert response.status_code == 501


# --- the watcher ------------------------------------------------------------------------------


def test_the_watcher_alarms_once_on_the_transition_not_every_tick():
    """A dead reader should log once, not every 2 s for a week — and it must alarm at all, because
    nothing else publishes when a background thread dies."""
    from radio_server.api.app import TransportWatcher

    readings = [True, True, False, False, False]
    published: list = []
    watcher = TransportWatcher(lambda: readings.pop(0), published.append)
    for _ in range(5):
        watcher.tick()
    alarms = [e for e in published if getattr(e, "type", None) == "alarm"]
    assert len(alarms) == 1
    assert alarms[0].data["kind"] == "transport_dead"


def test_the_watcher_says_so_when_it_comes_back():
    from radio_server.api.app import TransportWatcher

    readings = [True, False, False, True, True]
    published: list = []
    watcher = TransportWatcher(lambda: readings.pop(0), published.append)
    for _ in range(5):
        watcher.tick()
    kinds = [e.data.get("kind") for e in published if getattr(e, "type", None) == "alarm"]
    assert kinds == ["transport_dead", "transport_back"]


def test_the_watcher_never_touches_the_radio():
    """It is handed a liveness callable, not a radio. A watcher that polled the radio to find out
    whether the radio answers would be the failure mode wearing a monitor's hat."""
    from radio_server.api.app import TransportWatcher

    watcher = TransportWatcher(lambda: True, lambda _e: None)
    assert not hasattr(watcher, "_radio")


def test_a_watcher_whose_probe_raises_does_not_die():
    from radio_server.api.app import TransportWatcher

    def angry():
        raise RuntimeError("the probe itself broke")

    published: list = []
    watcher = TransportWatcher(angry, published.append)
    watcher.tick()  # must not raise
    watcher.tick()


# --- TIOCEXCL ---------------------------------------------------------------------------------


def test_claiming_the_port_is_a_no_op_on_a_handle_with_no_real_fd():
    """Every transport test in this suite runs on a fake handle. The claim must degrade to nothing
    rather than making the fakes unusable."""
    from radio_server.backends.uvk5.transport import claim_port_exclusive

    assert claim_port_exclusive(FakeSerial()) is False


def test_claiming_the_port_sets_tiocexcl_on_a_real_tty():
    """Measured, not assumed: pyserial's own `exclusive=True` is `flock` and does not stop a naive
    `open()`. One `TIOCEXCL` ioctl does."""
    import errno
    import os
    import pty

    from radio_server.backends.uvk5.transport import claim_port_exclusive

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    handle = _RealFd(os.open(path, os.O_RDWR | os.O_NOCTTY))
    try:
        assert claim_port_exclusive(handle) is True
        with pytest.raises(OSError) as excinfo:
            os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))
        assert excinfo.value.errno == errno.EBUSY
    finally:
        os.close(handle.fd)
        os.close(master)


class _RealFd:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def fileno(self) -> int:
        return self.fd


def test_a_superseded_reader_is_shed_by_generation_not_by_hope():
    # The guard has to be a tag the reader carries, not a join that "should" have finished:
    # `close()`/`reconnect()` join with a bound, and a wedged read can outlive it.
    transport = make_transport(FakeSerial())
    try:
        assert isinstance(transport._reader_gen, int)
    finally:
        transport.close()


def test_a_second_opener_is_told_what_to_do_about_it():
    """EBUSY on its own sends someone to a search engine. The message has to name the remedy the
    docs already prescribe, because this cycle is what makes the refusal happen at all."""
    from radio_server.backends.uvk5.transport import port_busy_message

    text = port_busy_message("/dev/ttyACM0", OSError(16, "Device or resource busy"))
    assert "/dev/ttyACM0" in text
    assert "systemctl --user stop radio-server" in text


# --- doctor's side of the refusal ----------------------------------------------------------


def test_doctor_explains_an_ebusy_instead_of_crashing_on_it():
    """The branch that had no coverage, and duly broke.

    Every `report.fail("could not open the serial port", ...)` site got the new explanation by a
    blanket edit — and two of them live in `*_connect_probe(report, cfg, ...)`, which has no `port`
    variable. The result was a `NameError` *while reporting an error*, found only by running doctor
    against a live station. Both shapes are pinned here so the next edit cannot repeat it.
    """
    import errno as _errno

    from radio_server.doctor import _open_failure_detail

    busy = _open_failure_detail("/dev/ttyACM0", OSError(_errno.EBUSY, "Device or resource busy"))
    assert "systemctl --user stop radio-server" in busy
    # Anything that is not EBUSY passes through unchanged — a missing device is not a busy one, and
    # telling someone to stop the service when the cable is unplugged sends them the wrong way.
    other = _open_failure_detail("/dev/ttyACM0", OSError(_errno.ENOENT, "No such file or directory"))
    assert "systemctl" not in other


def test_every_doctor_open_failure_site_passes_a_real_port_expression():
    """Guards the specific slip: the call sites are in functions with different local names."""
    import inspect
    import re

    from radio_server import doctor

    source = inspect.getsource(doctor)
    calls = re.findall(r"(?<!def )_open_failure_detail\(([^,]+),", source)
    assert len(calls) >= 5, "the call sites moved; this guard is no longer guarding them"
    for call in calls:
        arg = call.strip()
        assert arg in {"port", 'cfg["serial_port"]'}, f"unexpected port expression: {arg!r}"
