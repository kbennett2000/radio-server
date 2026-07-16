"""The controller loop: the whole software tower running live on the mock (ADR 0013).

This is the first cycle where `DtmfInput` → `AuthGate` → `Dispatcher` → `StationId` and an
attached `ScanEngine` are pumped by one driver off real session transitions, and where
`StationId.begin_session`/`check`/`sign_off` finally connect to something other than a test
fixture. Everything runs against `MockRadio` with the `FakeClock` and scripted receive audio —
no hardware, no multimon/piper (a `FakeDtmfDecoder` + `StubTts` stand in), and no real sleeps.

The load-bearing proofs: a DTMF-TOTP login over `step()` opens a session and arms the ID; an
authed ``"1"`` lands a genuinely CW-ID'd time announcement in `tx_log`; the periodic-ID safety
net forces an ID when the clock passes the interval mid-session; an inactivity timeout closes
the session and signs off (the transition `AuthGate` only demotes lazily — surfaced here via
`expire_if_idle`); an attached scan ticks each step and holds on a scripted-busy channel;
lifecycle events reach the `EventHub` in order; and `POST /controller` flips the live state in
`/status`.
"""

import asyncio

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.audio import AudioFrame, synth_dtmf
from radio_server.auth import OutcomeKind
from radio_server.backends import MockRadio
from radio_server.controller import (
    ControllerRunner,
    DEFAULT_SESSION_TIMEOUT,
    build_controller,
)
from radio_server.scan import ResumeMode, ScanEngine, ScanPlan
from radio_server.services import (
    DEFAULT_ID_INTERVAL,
    CwId,
    StubTts,
)

from .conftest import TEST_SECRET, FakeClock, make_settings
from .test_dtmf import FakeDtmfDecoder

CALLSIGN = "AE9S"

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# A canonical DTMF frame stands in for a received "over"; the fake decoder ignores its content.
RX = synth_dtmf("1")

# Three 20 kHz-spaced 2 m channels for the scan; the middle one is the scripted-busy target.
FREQS = [146_500_000, 146_520_000, 146_540_000]
BUSY = FREQS[1]

ID_AUDIO = CwId().encode(CALLSIGN)  # what `build_id_encoder` (CW default) prepends


class SilentDecoder:
    """A decoder that hears no DTMF — every frame yields an empty digit string.

    For the live-loop tests, where `receive()` delivers a real (empty) frame every iteration and
    a scripted finite decoder would run dry; here the loop simply pumps silence.
    """

    def decode(self, frame: AudioFrame) -> str:
        return ""


def build_ctrl(
    clock,
    scripts,
    *,
    radio=None,
    settings_extra=None,
    decoder=None,
    dedup=False,
    bindings=None,
    mumble_entries=(),
):
    """A controller over the real stack, wired via `build_controller` with test doubles.

    ``scripts`` feeds a `FakeDtmfDecoder` (one entry per received over) unless ``decoder`` is
    given. The TOTP secret is passed to `build_controller` (a secret, not a schema setting).
    ``on_event`` is left unset so the caller attaches a recorder or hands the controller to
    `create_app` (which rebinds it to the hub adapter).

    By default a tiny buffer window (`dtmf.buffer_seconds=0.02`) makes each received `synth_dtmf`
    frame fill a window and decode on its own step (recovering the per-over cadence these tests
    assume), and `dedup=False` because the `FakeDtmfDecoder` returns whole pre-formed entries per
    call — held-tone collapsing would fold a code's legitimately-repeated digits. A test exercising
    the real per-window buffering/dedup path passes `dedup=True` with its own window + decoder.
    """
    radio = radio if radio is not None else MockRadio()
    # Announcements are silenced by default here (empty → the coerce_optional_str "say nothing" path)
    # so the ID/command/timeout tests below see only their own overs; the announcement tests re-enable
    # them explicitly via settings_extra.
    overrides = {
        "station.callsign": CALLSIGN,
        "dtmf.buffer_seconds": 0.02,
        "controller.login_announcement": "",
        "controller.timeout_announcement": "",
        "controller.logout_announcement": "",
    }
    if settings_extra:
        overrides.update(settings_extra)
    settings = make_settings(overrides)
    dec = decoder if decoder is not None else FakeDtmfDecoder(list(scripts))
    ctrl = build_controller(
        settings,
        radio=radio,
        totp_secret=TEST_SECRET,
        decoder=dec,
        tts=StubTts(),
        clock=clock,
        dedup=dedup,
        service_bindings=bindings,
        mumble_entries=mumble_entries,
    )
    return radio, ctrl


# --- login → session open, ID armed --------------------------------------------------------

def test_login_over_the_loop_opens_session_and_arms_id(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#"])
    events = []
    ctrl.on_event = events.append

    result = ctrl.step(clock.now, RX)

    assert result.outcomes[0].kind is OutcomeKind.ACCEPTED
    assert ctrl.session.authenticated
    assert result.session_open is True
    # A successful login emits the auth outcome and the session_open lifecycle event (ADR 0019).
    assert [e.phase for e in events] == ["auth_accepted", "session_open"]
    assert radio.tx_log == []  # authenticating never transmits


class _PerWindowDecoder:
    """Models the real decoder over buffered windows: emits one code key per *filled* window, held
    (doubled) as multimon would for a keyed tone, with a silent window between keys. It is only
    consulted when `BufferedDtmfInput` accumulates a full window, so it exercises accumulation +
    held-tone dedup exactly as production does. '' once the script is exhausted."""

    def __init__(self, entry: str) -> None:
        script: list[str] = []
        for key in entry:
            script.append(key * 2)  # a held tone within the window (multimon re-emits it)
            script.append("")  # the key released → a silent window resets the dedup run
        self._script = script
        self._i = 0

    def decode(self, frame: AudioFrame) -> str:
        v = self._script[self._i] if self._i < len(self._script) else ""
        self._i += 1
        return v


def test_login_accumulates_from_short_frames_over_the_buffered_loop(clock, code_for):
    # The real bug ADR 0030 fixes: a keyed digit arrives as many ~20 ms frames, each too short to
    # decode. With BufferedDtmfInput the controller accumulates them into ~windows and authenticates.
    code = code_for(clock.now)
    decoder = _PerWindowDecoder(code + "#")
    # window = 4800 bytes = 0.05 s; a 960-byte (~20 ms) frame is far too short alone → 5 fill a window.
    radio, ctrl = build_ctrl(
        clock, [], decoder=decoder, dedup=True, settings_extra={"dtmf.buffer_seconds": 0.05}
    )
    events: list = []
    ctrl.on_event = events.append
    short_frame = AudioFrame(b"\x00\x01" * 480)  # 960 bytes, ~20 ms — one decode needs five of these

    accepted = False
    # Each of the (2 × len(code+"#")) windows needs 5 short frames; loop with headroom.
    for _ in range(len(code + "#") * 2 * 5 + 10):
        result = ctrl.step(clock.now, short_frame)
        if any(o.kind is OutcomeKind.ACCEPTED for o in result.outcomes):
            accepted = True
            break

    assert accepted, "the buffered controller should authenticate a code arriving in short frames"
    assert ctrl.session.authenticated
    assert "auth_accepted" in [e.phase for e in events]


# --- deferred-event emissions: auth outcome, command, ID enrichment (ADR 0019) -------------

def test_rejected_auth_emits_auth_rejected_with_no_code(clock, code_for):
    good = code_for(clock.now)
    bad = "000000" if good != "000000" else "111111"
    radio, ctrl = build_ctrl(clock, [bad + "#"])
    events = []
    ctrl.on_event = events.append

    result = ctrl.step(clock.now, RX)

    assert result.outcomes[0].kind is OutcomeKind.REJECTED
    assert not ctrl.session.authenticated
    assert [e.phase for e in events] == ["auth_rejected"]
    # SECURITY (guardrail 4 / ADR 0018-0019): the failed-auth signal carries no code material.
    rejected = events[0]
    assert rejected.data is None
    assert bad not in repr(rejected)


def test_command_dispatch_emits_command_with_service(clock, code_for):
    good = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [good + "#", "1#"])
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)              # login
    ctrl.step(clock.now, RX)              # authed "1" -> the time service dispatches

    commands = [e for e in events if e.phase == "command"]
    assert [e.data for e in commands] == [{"service": "time"}]


def test_registry_miss_emits_no_command(clock, code_for):
    good = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [good + "#", "9#"])  # "9" is not a registered service
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)              # login
    result = ctrl.step(clock.now, RX)     # authed "9" -> graceful miss

    assert result.outcomes[0].kind is OutcomeKind.COMMAND
    assert result.outcomes[0].detail.transmitted is False  # nothing transmitted
    assert [e for e in events if e.phase == "command"] == []  # ...so no dispatch record


def test_forced_id_event_carries_callsign_and_mode(clock, code_for):
    good = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock, [good + "#", "1#"], settings_extra={"controller.session_timeout": 700}
    )
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)              # login
    ctrl.step(clock.now, RX)              # a command transmits (arms the periodic ID)
    clock.advance(DEFAULT_ID_INTERVAL)    # +600: overdue, still within the 700s timeout
    ctrl.step(clock.now)                  # periodic ID fires

    id_events = [e for e in events if e.phase == "id"]
    assert [e.data for e in id_events] == [{"callsign": CALLSIGN, "mode": "cw"}]


# --- authed "1" → a CW-ID'd time announcement in tx_log ------------------------------------

def test_authed_one_lands_cw_id_time_announcement(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "1#"])

    ctrl.step(clock.now, RX)              # over 1: login
    result = ctrl.step(clock.now, RX)     # over 2: authed "1"

    assert result.outcomes[0].kind is OutcomeKind.COMMAND
    assert result.outcomes[0].detail.service == "time"
    assert result.outcomes[0].detail.transmitted is True
    # One over, carrying the CW station ID prepended to the time announcement.
    assert len(radio.tx_log) == 1
    assert radio.tx_log[0].samples.startswith(ID_AUDIO.samples)
    assert len(radio.tx_log[0].samples) > len(ID_AUDIO.samples)  # ... then the announcement


# --- forced periodic ID mid-session (Part 97 safety net) -----------------------------------

def test_forced_periodic_id_fires_when_interval_passes_mid_session(clock, code_for):
    code = code_for(clock.now)
    # Session timeout well past the ID interval so the session stays open across it.
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#"], settings_extra={"controller.session_timeout": 100000}
    )
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)  # login
    ctrl.step(clock.now, RX)  # command -> transmits (ID + time), last_id = now
    assert len(radio.tx_log) == 1

    clock.advance(DEFAULT_ID_INTERVAL)      # now overdue for a re-ID
    result = ctrl.step(clock.now)           # no audio this iteration

    assert result.id_sent is True
    assert len(radio.tx_log) == 2           # an ID-only over was forced
    assert radio.tx_log[1] == ID_AUDIO
    assert events[-1].phase == "id"


# --- inactivity timeout closes the session and signs off -----------------------------------

def test_inactivity_timeout_closes_session_and_signs_off(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "1#"])  # default 300s session timeout
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)  # login
    ctrl.step(clock.now, RX)  # command -> station has transmitted, so sign-off will ID
    assert len(radio.tx_log) == 1

    clock.advance(DEFAULT_SESSION_TIMEOUT + 1.0)  # idle past the timeout, no further digits
    result = ctrl.step(clock.now)

    assert result.signed_off is True
    assert ctrl.session.authenticated is False
    assert result.session_open is False
    assert len(radio.tx_log) == 2                 # closing ID
    assert radio.tx_log[1] == ID_AUDIO
    assert events[-1].phase == "session_close"


# --- an attached scan ticks each step and holds on scripted busy ---------------------------

def test_attached_scan_ticks_each_step_and_holds_on_busy(clock):
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    engine = ScanEngine(
        radio, ScanPlan.from_frequencies(FREQS), mode=ResumeMode.CARRIER, settle=0.0, clock=clock
    )
    ctrl.scan = engine

    last = None
    for _ in range(8):
        last = ctrl.step(clock.now)  # no DTMF; the scan advances on the injected clock
        clock.advance(0.01)

    assert last.scanning is True
    # Carrier-operated scan dwells on the busy channel — it holds there.
    assert engine.current_frequency == BUSY


# --- lifecycle events reach the hub in order -----------------------------------------------

def test_lifecycle_events_are_emitted_in_order(clock, code_for):
    code = code_for(clock.now)
    # Timeout between the ID interval (600) and the moment we force a close, so the order is
    # session_open (login) -> id (periodic) -> session_close (timeout).
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#"], settings_extra={"controller.session_timeout": 700}
    )
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)              # login   -> auth_accepted, session_open
    ctrl.step(clock.now, RX)              # command -> command (transmits)
    clock.advance(DEFAULT_ID_INTERVAL)    # +600: overdue, still within the 700s timeout
    ctrl.step(clock.now)                  # periodic -> id
    clock.advance(101.0)                  # now 701s idle since last activity
    ctrl.step(clock.now)                  # idle    -> session_close

    assert [e.phase for e in events] == [
        "auth_accepted",
        "session_open",
        "command",
        "id",
        "session_close",
    ]


# --- session voice UX: announcements, 4# play-ID, 99# force logout (cycle 37) ---------------

def test_login_speaks_welcome_prepended_with_id(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock, [code + "#"], settings_extra={"controller.login_announcement": "Welcome."}
    )
    ctrl.step(clock.now, RX)  # login
    # One over: the CW station ID prepended to the spoken login confirmation (session's first over).
    assert radio.tx_log == [ID_AUDIO + StubTts().render("Welcome.")]


def test_play_id_command_transmits_an_id_and_keeps_the_session(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "4#"])  # announcements disabled by build_ctrl
    events: list = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)             # login (silent)
    assert radio.tx_log == []
    result = ctrl.step(clock.now, RX)    # 4# -> play station ID

    assert radio.tx_log == [ID_AUDIO]    # an ID-only over
    assert events[-1].phase == "id"
    assert ctrl.session.authenticated    # 4# does not end the session
    assert result.signed_off is False


def test_force_logout_speaks_confirmation_then_closing_id(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#", "99#"], settings_extra={"controller.logout_announcement": "Goodbye."}
    )
    events: list = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)          # login (silent)
    ctrl.step(clock.now, RX)          # 1# time -> station transmits (ID + time), arms sign-off
    n = len(radio.tx_log)
    result = ctrl.step(clock.now, RX)  # 99# force logout

    assert ctrl.session.authenticated is False
    assert result.signed_off is True
    # The spoken confirmation, then the Part-97 closing ID.
    assert radio.tx_log[n:] == [StubTts().render("Goodbye."), ID_AUDIO]
    assert events[-1].phase == "session_close"


def test_idle_timeout_speaks_before_the_closing_id(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#"], settings_extra={"controller.timeout_announcement": "Session timed out."}
    )
    ctrl.step(clock.now, RX)  # login (silent)
    ctrl.step(clock.now, RX)  # 1# -> transmits (ID + time)
    n = len(radio.tx_log)
    clock.advance(DEFAULT_SESSION_TIMEOUT + 1.0)
    result = ctrl.step(clock.now)

    assert result.signed_off is True
    assert radio.tx_log[n:] == [StubTts().render("Session timed out."), ID_AUDIO]


def test_announcement_defaults_are_friendly():
    from radio_server.controller.engine import (
        load_login_announcement,
        load_logout_announcement,
        load_timeout_announcement,
    )

    s = make_settings({})
    assert load_login_announcement(s) == "Welcome."
    assert load_timeout_announcement(s) == "Session timed out."
    assert load_logout_announcement(s) == "Goodbye."


def test_service_catalog_lists_builtins_sorted_by_digit(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder())  # no weather -> just time + builtins
    by_digit = {e["digit"]: e["name"] for e in ctrl.service_catalog}
    assert [e["digit"] for e in ctrl.service_catalog] == ["1", "4", "99"]
    assert by_digit["4"] == "station-id" and by_digit["99"] == "logout"


def test_remapped_builtin_digits_drive_the_commands_over_the_air(clock, code_for):
    # Move station-id to 5# and logout to 0# (ADR 0034); the engine keys them off the operator's map,
    # not the historical 4/99.
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock,
        [code + "#", "5#", "0#"],
        bindings={"1": "time", "5": "station-id", "0": "logout"},
    )
    ctrl.step(clock.now, RX)             # login (silent)
    result = ctrl.step(clock.now, RX)    # 5# -> play station ID, session stays open
    assert radio.tx_log == [ID_AUDIO]
    assert ctrl.session.authenticated and result.signed_off is False

    result = ctrl.step(clock.now, RX)    # 0# -> force logout, closing ID
    assert ctrl.session.authenticated is False and result.signed_off is True


def test_the_old_builtin_digits_are_inert_after_a_remap(clock, code_for):
    # With logout moved to 0#, a 99# over is just an unmapped digit — a graceful miss, session intact.
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock, [code + "#", "99#"], bindings={"1": "time", "5": "station-id", "0": "logout"}
    )
    ctrl.step(clock.now, RX)             # login (silent)
    result = ctrl.step(clock.now, RX)    # 99# -> no longer logout; nothing happens
    assert ctrl.session.authenticated is True and result.signed_off is False
    assert radio.tx_log == []


# --- Mumble link combos: connect/disconnect built-ins from the entry list (ADR 0042) --------

def _link_entries():
    from radio_server.link import resolve_mumble_entries

    return resolve_mumble_entries(
        [
            {"name": "home", "host": "h1", "dtmf": "13"},
            {"name": "club_net", "host": "h2", "dtmf": "1234"},
        ]
    )


def test_link_combo_fires_on_link_and_speaks_confirmation(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "13#"], mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    events: list = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)   # login (silent)
    result = ctrl.step(clock.now, RX)  # 13# -> connect "home"

    assert linked == ["home"]
    # The session's first over: the CW ID prepended to the spoken confirmation.
    assert radio.tx_log == [ID_AUDIO + StubTts().render("Linked to home.")]
    assert events[-1].phase == "link" and events[-1].data == {"entry": "home"}
    assert ctrl.session.authenticated and result.signed_off is False


def test_link_announcement_speaks_the_slug_with_spaces(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "1234#"], mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    assert linked == ["club_net"]
    assert radio.tx_log == [ID_AUDIO + StubTts().render("Linked to club net.")]


def test_disconnect_combo_fires_on_link_none(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "13#", "73#"], mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    events: list = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)  # login
    ctrl.step(clock.now, RX)  # 13# connect
    n = len(radio.tx_log)
    ctrl.step(clock.now, RX)  # 73# disconnect

    assert linked == ["home", None]
    assert radio.tx_log[n:] == [StubTts().render("Link off.")]
    assert events[-1].phase == "link" and events[-1].data == {"entry": None}


def test_disconnect_combo_works_without_a_session(clock):
    # ADR 0043: dropping the link is the one un-gated RF command — it must work after the
    # session times out mid-net (the "I'm done listening" case), so it bypasses the TOTP gate.
    radio, ctrl = build_ctrl(clock, ["73#"], mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    events: list = []
    ctrl.on_event = events.append

    result = ctrl.step(clock.now, RX)

    assert linked == [None]
    # The station's first over ever, so the CW ID is prepended into it (Part 97 intact).
    assert radio.tx_log == [ID_AUDIO + StubTts().render("Link off.")]
    assert result.outcomes[0].kind is OutcomeKind.COMMAND
    assert events[-1].phase == "link" and events[-1].data == {"entry": None}
    # Never treated as a login attempt, and no session opens for it.
    assert not any(e.phase == "auth_rejected" for e in events)
    assert not ctrl.session.authenticated


def test_disconnect_combo_does_not_extend_a_session(clock, code_for):
    # The un-gated path skips the activity stamp: keying 73# mid-session must not push the
    # inactivity timeout out (a disconnect is a de-escalation, not session activity).
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "73#"], mumble_entries=_link_entries())
    ctrl.on_link = lambda name: None

    ctrl.step(clock.now, RX)  # login at t0
    clock.advance(DEFAULT_SESSION_TIMEOUT - 1.0)
    ctrl.step(clock.now, RX)  # 73# just inside the timeout — must not refresh it
    clock.advance(2.0)        # t0 + timeout + 1: idle counted from login, not from 73#
    result = ctrl.step(clock.now)

    assert result.signed_off is True and not ctrl.session.authenticated


def test_link_combo_requires_an_authenticated_session(clock):
    # Unauthenticated, "13#" is a TOTP attempt (rejected) — never a link command (guardrail 4:
    # *connecting* enables Mumble voice to key TX, so it sits behind the login like every
    # capability-granting command; only the de-escalating disconnect is un-gated, ADR 0043).
    radio, ctrl = build_ctrl(clock, ["13#"], mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    result = ctrl.step(clock.now, RX)
    assert result.outcomes[0].kind is OutcomeKind.REJECTED
    assert linked == [] and radio.tx_log == []


def test_link_combos_are_inert_with_no_entries(clock, code_for):
    # No [[mumble.servers]]: 13#/73# are unmapped digits — a graceful miss, nothing transmitted.
    # The unauthenticated leading 73# is a plain rejected login attempt: the ADR 0043 carve-out
    # exists only when entries configure a link-off combo.
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, ["73#", code + "#", "13#", "73#"])
    linked: list = []
    ctrl.on_link = linked.append
    first = ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    assert first.outcomes[0].kind is OutcomeKind.REJECTED
    assert linked == [] and radio.tx_log == []


def test_link_combo_colliding_with_a_service_digit_fails_loud_at_build(clock):
    from radio_server.link import resolve_mumble_entries

    entries = resolve_mumble_entries([{"name": "home", "host": "h1", "dtmf": "1"}])
    try:
        build_ctrl(clock, [], decoder=SilentDecoder(), mumble_entries=entries)
    except RuntimeError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected the 1/time collision to fail loud")


def test_link_announcements_are_configurable(clock, code_for):
    # The confirmations are settings (mumble.link_announcement, a {name} template, and
    # mumble.link_off_announcement) — the operator's phrasing, like the session announcements.
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock,
        [code + "#", "13#", "73#"],
        mumble_entries=_link_entries(),
        settings_extra={
            "mumble.link_announcement": "Now chatting on {name}, enjoy!",
            "mumble.link_off_announcement": "Chat over.",
        },
    )
    ctrl.on_link = lambda name: None
    ctrl.step(clock.now, RX)  # login (silent)
    ctrl.step(clock.now, RX)  # 13# connect
    ctrl.step(clock.now, RX)  # 73# disconnect
    assert radio.tx_log == [
        ID_AUDIO + StubTts().render("Now chatting on home, enjoy!"),
        StubTts().render("Chat over."),
    ]


def test_blank_link_announcements_connect_silently(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(
        clock,
        [code + "#", "13#", "73#"],
        mumble_entries=_link_entries(),
        settings_extra={
            "mumble.link_announcement": "",
            "mumble.link_off_announcement": "",
        },
    )
    linked: list = []
    ctrl.on_link = linked.append
    ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    ctrl.step(clock.now, RX)
    assert linked == ["home", None]  # the commands still fire
    assert radio.tx_log == []  # nothing spoken


def test_service_catalog_lists_link_combos_with_the_keypad(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder(), mumble_entries=_link_entries())
    by_digit = {e["digit"]: e for e in ctrl.service_catalog}
    assert by_digit["13"]["name"] == "link:home"
    assert "home" in by_digit["13"]["description"]
    assert by_digit["1234"]["name"] == "link:club_net"
    assert "club net" in by_digit["1234"]["description"]  # spoken form in the description
    assert by_digit["73"]["name"] == "link-off"
    # Sorted with the rest of the keypad (string order, like the existing catalog).
    digits = [e["digit"] for e in ctrl.service_catalog]
    assert digits == sorted(digits)


def test_trigger_runs_a_link_combo_from_the_lan(clock):
    # The API trigger seam (LAN token authority) reaches the link built-ins like any command.
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder(), mumble_entries=_link_entries())
    linked: list = []
    ctrl.on_link = linked.append
    result = ctrl.trigger("13")
    assert result == {"digit": "13", "builtin": True, "transmitted": True}
    assert linked == ["home"]


# --- the API trigger seam: run a service/command by digit without an RF login ---------------

def test_trigger_runs_a_service_over_the_air_without_rf_login(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder())
    events: list = []
    ctrl.on_event = events.append

    result = ctrl.trigger("1", clock.now)  # time service, no DTMF auth

    assert result["service"] == "time" and result["transmitted"] is True
    assert len(radio.tx_log) == 1                     # ID + time, the fresh station's first over
    assert radio.tx_log[0].samples.startswith(ID_AUDIO.samples)
    assert [e.phase for e in events] == ["command"]


def test_trigger_plays_station_id(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder())
    result = ctrl.trigger("4", clock.now)
    assert result["builtin"] is True
    assert radio.tx_log == [ID_AUDIO]


def test_trigger_logout_with_no_active_session_is_a_graceful_noop(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder())
    result = ctrl.trigger("99", clock.now)
    assert result["builtin"] is True
    assert radio.tx_log == []            # nothing to close, nothing keyed
    assert ctrl.session.authenticated is False


def test_trigger_unknown_digit_transmits_nothing(clock):
    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder())
    result = ctrl.trigger("7", clock.now)
    assert result["transmitted"] is False
    assert radio.tx_log == []


# --- the thin async driver pumps step() each iteration -------------------------------------

def test_runner_pumps_step_each_iteration(clock):
    box: dict = {}

    class CountingRadio(MockRadio):
        def __init__(self) -> None:
            super().__init__()
            self.receives = 0

        def receive(self) -> AudioFrame:
            self.receives += 1
            if self.receives >= 3:
                box["runner"].stop()
            return super().receive()

    radio = CountingRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.0)
    box["runner"] = runner

    asyncio.run(runner.run())

    assert radio.receives == 3       # looped receive() -> step() until stop()
    assert runner.running is False


# --- the API: start/stop the controller, live state in /status -----------------------------

def _api(radio, clock, **kwargs):
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder(), **kwargs)
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.01)
    app = create_app(radio, api_token=TOKEN, controller=ctrl, runner=runner)
    return ctrl, runner, app


def test_controller_endpoint_flips_running_in_status(clock):
    radio = MockRadio()
    _, _, app = _api(radio, clock)
    with TestClient(app) as client:
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False

        r = client.post("/controller", json={"on": True}, headers=AUTH)
        assert r.status_code == 200
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is True

        client.post("/controller", json={"on": False}, headers=AUTH)
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False


def test_controller_endpoint_requires_token(clock):
    radio = MockRadio()
    _, _, app = _api(radio, clock)
    assert TestClient(app).post("/controller", json={"on": True}).status_code == 401


def test_controller_autostarts_on_boot_when_setting_on(clock):
    # ADR 0037: with the manual Start/Stop button gone, a configured controller comes up on boot when
    # `controller.autostart` is on. Passing an explicit `settings` is the real `build_app` path.
    radio = MockRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    app = create_app(
        radio, api_token=TOKEN, controller=ctrl, settings=make_settings({"controller.autostart": True})
    )
    with TestClient(app) as client:  # the context manager runs the lifespan (startup autostart)
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is True


def test_controller_does_not_autostart_when_setting_off(clock):
    radio = MockRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    app = create_app(
        radio, api_token=TOKEN, controller=ctrl, settings=make_settings({"controller.autostart": False})
    )
    with TestClient(app) as client:
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False


def test_no_autostart_without_explicit_settings(clock):
    # The bare DI seam (settings=None, the default used across these tests) must never autostart, even
    # though the resolved default is on — otherwise every controller test would boot active.
    radio = MockRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    app = create_app(radio, api_token=TOKEN, controller=ctrl)
    with TestClient(app) as client:
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False


def test_status_controller_block_is_null_when_unconfigured():
    radio = MockRadio()
    client = TestClient(create_app(radio, api_token=TOKEN))
    assert client.get("/status", headers=AUTH).json()["controller"] is None


def test_controller_endpoint_503_when_unconfigured():
    radio = MockRadio()
    client = TestClient(create_app(radio, api_token=TOKEN))
    resp = client.post("/controller", json={"on": True}, headers=AUTH)
    assert resp.status_code == 503


# --- session lifecycle streamed over the WebSocket -----------------------------------------

def test_session_events_reach_the_ws_in_order(clock, code_for):
    code = code_for(clock.now)
    radio = MockRadio()
    # A controller scripted for one login; handed to the app so its on_event publishes to the hub.
    _, ctrl = build_ctrl(clock, [code + "#"], radio=radio)
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.01)
    app = create_app(radio, api_token=TOKEN, controller=ctrl, runner=runner)

    with TestClient(app) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            ws.receive_json()  # initial status snapshot
            ctrl.step(clock.now, RX)                       # login -> auth_accepted, session_open
            clock.advance(DEFAULT_SESSION_TIMEOUT + 1.0)
            ctrl.step(clock.now)                           # idle  -> session_close
            seen = [ws.receive_json() for _ in range(3)]

    # The login now surfaces its auth outcome as its own `auth` event ahead of the session
    # lifecycle (ADR 0019); the `auth` payload is the result only — never a code.
    assert [(e["type"], e["data"].get("phase") or e["data"].get("result")) for e in seen] == [
        ("auth", "accepted"),
        ("session", "session_open"),
        ("session", "session_close"),
    ]
