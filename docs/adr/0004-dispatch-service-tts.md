# 0004 — Command dispatch, service interface, and TTS shape

Status: Accepted

## Context

Cycle 2 left `AuthGate`'s command hook as a self-announcing stub (`_unwired_dispatch`).
An authenticated session's digits need to route to real services, and this cycle produces
the server's **first transmission**: digit `"1"` announces the time. It stays fully mock
and hardware-free — real TTS (piper) and real audio I/O are later cycles — so the whole
path must be deterministic and assertable via `MockRadio.tx_log` with a fake clock.

This ADR fixes the shape of three things introduced together: the service interface, the
dispatch registry, and the TTS interface.

## Decision

- **Services produce audio; the dispatcher performs I/O.** `Service = Callable[[Session,
  ServiceContext], AudioFrame]` — a handler takes the calling session plus a context and
  *returns* the audio to transmit. It does no radio I/O itself. `Dispatcher` owns the
  `Radio` and calls `radio.transmit(...)` with whatever the handler returns. This keeps
  handlers pure and unit-testable, and makes "unknown digit → no transmit" correct by
  construction: with no handler there is simply nothing to send.

- **`ServiceContext` is minimal and radio-free.** A frozen dataclass carrying only what a
  handler needs to *build* audio: `clock: Clock` and `tts: TtsEngine`. It deliberately
  excludes a `Radio` reference so a handler cannot key the transmitter or do arbitrary
  I/O — that authority stays with the dispatcher. Extend the context by ADR when a service
  genuinely needs more (e.g. radio `status()` for a service that reports frequency).

- **Registry maps digit → named service; the dispatcher is a `Dispatch`.** `ServiceRegistry`
  holds `digit -> (name, Service)`; services self-register into an instance at wiring time
  (`time_service`'s `register(registry, tz)`), so adding a service does not touch dispatch.
  `Dispatcher(radio, ctx, registry)` is *callable* with the exact signature the auth layer
  already defined — `Dispatch = Callable[[str, Session], object]` — so it drops straight
  into `AuthGate(verifier, ..., dispatch=dispatcher)` with no change to the auth layer. The
  cycle-2 stub remains the default; we inject the real dispatcher.

- **`DispatchResult` is the hook's return, surfaced as `Outcome.detail`.** Frozen dataclass
  `{digits, service: str | None, transmitted: bool}`. A known digit → `service=name,
  transmitted=True` after transmit; an unknown digit → `service=None, transmitted=False`
  and nothing sent. An unknown digit is a graceful no-op, **not** an error: the session was
  authenticated and did route to dispatch, so `Outcome.kind` stays `COMMAND`. Turning a
  no-service result into a spoken "unknown command" reply is a later audio-feedback cycle's
  job; this cycle only reports it.

- **`TtsEngine` is a `Protocol`; `StubTts` is deterministic.** `render(text) -> AudioFrame`.
  `StubTts.render` returns `b"<audio:" + text + b">"` — a pure function of the text, so a
  test asserts exactly what was "spoken" by reading `tx_log`. The real piper engine (later
  cycle) implements the same one-method contract, so nothing above the TTS layer changes
  when it lands. Audio stays opaque `bytes` (`AudioFrame`); the rate/width/channel format is
  pinned by its own ADR before real audio I/O.

- **Time formatting is isolated and config-driven.** `format_spoken_time(now, tz) -> str`
  is a pure function (`"The time is %H:%M %Z"`, **24-hour local**), kept out of dispatch so
  24h↔12h and wording tweaks never ripple. The timezone is configuration: `RADIO_TZ`
  (an IANA name) via `load_timezone(env=os.environ)`, bound into the service at registration
  so tests pass an explicit `ZoneInfo` and stay host-independent. Unlike the TOTP secret,
  timezone is not security-relevant, so it has a **marked default** (`UTC`) rather than
  failing loud; a *set-but-invalid* zone still raises (fail loud on misconfiguration).

- **The time service reads the same injected clock as the session timeout.** The context's
  `clock` is the very clock passed to `AuthGate`, so the announced time and the inactivity
  timeout share one source of truth — and a `FakeClock` drives both deterministically.

## Consequences

- The first transmission exists and is asserted end-to-end (enroll → authenticate → `"1"`
  → exact `tx_log` bytes) with no hardware and no real time.
- Adding a service is: write a `Service`, `register` it under a digit. Dispatch, auth, and
  the API surface are untouched.
- Because handlers can't reach the radio, no service can accidentally key TX or bypass the
  dispatcher's single transmit path — the natural seam where automatic station ID will be
  enforced next cycle.
- **Transmissions this cycle are un-ID'd on purpose.** Automatic station ID (Part 97,
  guardrail 5 — CW/voice on a ≤10-min interval and at session end) is a separate scheduler
  concern and lands in cycle 4. Nothing goes to real hardware until it exists; recorded in
  HANDOFF.
- Single-process, in-memory registry and context; no persistence needed (services are pure
  functions of their inputs).
