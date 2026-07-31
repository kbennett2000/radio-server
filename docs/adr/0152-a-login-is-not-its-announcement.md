# 0152 — A login is not its announcement

Status: Accepted

Corrects a defect shipped by ADR
[0151](0151-a-failed-key-up-must-give-the-radio-back.md), in the web-session seam of ADR
[0046](0046-web-session-open.md) and the controller loop of ADR
[0013](0013-controller-loop.md). Host only — no firmware change, no UI, no bridges.

## Context

ADR 0151 gave `Controller._keying` a `strict` flag that re-raises after emitting, and applied it to
**both** API entry points — `trigger` and `open_session` — on the argument that a caller who *can*
be told about a hardware fault should be. That argument is right for `trigger`. For `open_session`
it was wrong, and it shipped a defect.

**Verified on `origin/master` before anything was changed**, not inferred from the diff:

```python
opened = self._gate.open(self._session, now)     # commits: state = AUTHENTICATED
if opened:
    self._station.begin_session(now)             # arms the station ID
    if self._login_audio is not None:
        self._keying(..., strict=True)           # raises here
    self._emit("auth_accepted")                  # never reached
    self._emit("session_open")                   # never reached
return {...}                                     # never reached
```

A radio demodulating AM refuses its own PTT path (ADR 0150), so the announcement raises. The gate
is **already open** and the ID **already armed**, but neither lifecycle event fires and the caller
gets a 503. The result is an authenticated session that exists in memory and **does not exist in the
ledger**: `GET /status` reports `session_open: true` while the audit trail has no `auth_accepted`
and no `session_open` record for it. The 503 tells the operator their session did not open; the very
next status call contradicts it.

ADR 0151 also shipped `test_open_session_still_raises_so_the_api_can_report_503`, which asserts the
raise. **That test pinned the defect** rather than catching it — recorded here rather than quietly
rewritten, because a green suite that encodes the bug is worth knowing about.

### `step()` was checked, not assumed

The obvious question is whether the over-the-air login has the same hole. It does not, and the
reason is worth stating because it is an accident rather than a design: `step()`'s ACCEPTED branch
calls `self._keying("announcement", ...)` with **no `strict=` argument**, so it takes the default
`False` — the guard emits `tx_failed`, returns, and both events still fire. The DTMF login survives
a refused announcement intact. Only the API path lost the session, which is exactly the kind of
divergence a shared helper with a per-call-site flag invites.

## Decision

**`open_session` stops re-raising. `trigger` keeps re-raising, unchanged.**

The line between them is not "API versus over-the-air" — that was ADR 0151's mistake, and it is a
line drawn on the wrong axis. It is: **does the caller's request name a transmission?**

- **`trigger`** *is* "transmit this" — the "play ID" button, `POST /services/{digit}`. If nothing
  went out, the request failed and a 200 would be a lie. ADR 0151 is right here and this cycle does
  not touch it.
- **`open_session`** is "open my session". The announcement is a side effect *of* the open, not the
  open itself. `gate.open()` has flipped the state and stamped `last_activity` before the
  announcement is ever attempted.

Three reasons, in order of weight:

1. **It restores an invariant the code already claims.** `open_session`'s own docstring says
   "On-air behavior is identical to a DTMF-accepted auth (the `step` ACCEPTED branch)". `step()`
   neither rolls back nor loses its emits. The two paths disagreed; they agree again.
2. **A 503 that contradicts observable state is the fault class this repo keeps closing.** The
   caller is told the request failed while `/status` shows the session open — "no signal and no
   measurement look identical" (ADR [0140](0140-the-first-key-is-always-lost.md)/[0143](0143-a-tune-must-know-it-has-a-session.md)),
   one layer up.
3. **The ledger must not contain unrecorded authenticated sessions.** `auth_accepted` and
   `session_open` are the audit trail for who held control of the station (ADR
   [0019](0019-deferred-event-instrumentation.md)).

### The rollback alternative, and why not

Rolling the gate back (`gate.logout()`) on a failed announcement is defensible — it is the
all-or-nothing shape ADR 0151 argued for on key-up, applied consistently.

**A retrying caller would be fine**, which is worth saying because it is the obvious objection and
it does not land: `gate.open()` would return `True` again on the now-unauthenticated session,
`begin_session()` would re-reset the ID timer, the announcement would fail again, and it would roll
back again — a clean loop, no half-state. The one thing rollback cannot undo is `begin_session()`,
which has already reset `_last_id`/`_transmitted_this_session`; that leaves the ID armed for a
session that no longer exists, which can only cause an *extra* ID later. Legal direction, so not
disqualifying either.

It is rejected for a different reason: **it would replace one asymmetry with another.** `step()`
does not roll back, so an RF operator logging in over DTMF while the radio is in AM would keep their
session while a web operator doing the same thing would lose theirs — the equivalence the docstring
promises, broken in the opposite direction. And a key-up and a login are not the same kind of thing.
ADR 0151's all-or-nothing protects a *hardware* resource — the PTT line, the arbiter latch — where a
half-state strands a transmitter. A session is control-plane state with no such hazard.

### `announced` is tri-state, and always present

The response gains `announced` and `announce_error`. `announced` follows `tx_ok` (ADR 0150):
`None` means **not attempted** — the session was already open, or nothing is configured to say —
which is a different answer from `False`, **attempted and refused**. Collapsing them would report a
station with no configured announcement as having failed to speak, and send an operator looking for
a radio fault that is not there.

`opened` and `announced` are documented as **pairs** in `api.md`, because neither is unambiguous
alone: `announced: null` means "already open" next to `opened: false` and "nothing configured" next
to `opened: true`.

**The smaller diff — add the keys only when the announcement fails — was rejected.** A key that
appears only on failure is the silent-failure class ADRs
[0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md)/[0150](0150-the-host-learns-to-listen-to-am.md)
and 0151 exist to close: a client that never learns to look for it reads a 200 with no announcement
as a clean success. That is how a station ends up believing it spoke when it did not. Uniform shape,
every call — the four exact-equality assertions it costs are a one-time price.

`_keying` gains an `on_error` callback rather than `open_session` inlining its own `try/except`,
so the log line and the `tx_failed` emit stay in one place instead of two that must be kept in step.

## Acceptance

**Fail-first, run against `origin/master` and recorded red.** The test asserts the *invariant*
rather than the chosen remedy — either the gate is closed, or both lifecycle events were recorded,
never open-and-unrecorded — so it survives a later change of mind about how to fix it:

```
Session(state=<SessionState.AUTHENTICATED>, last_activity=1000000.0) = Controller.session
Extra items in the left set: 'auth_accepted', 'session_open'
```

That is the defect stated in one line: the session is authenticated and the ledger saw neither
event.

**`test_trigger_still_raises_so_the_api_can_report_503` stays green and untouched.** It is the proof
this cycle did not overcorrect — the diff contains no `+`/`-` line touching it.

**`step()`'s ACCEPTED path is now pinned explicitly**, since the whole defect was a divergence
between two paths one docstring calls identical.

**`uv run pytest`: 1965 passed, 5 skipped** (1961/5 before — 4 new tests, plus one replaced
one-for-one).

## Consequences

- `POST /auth/session` returns **200 with the reason** where a refusing radio previously produced a
  503. The session is open, and `/status` now agrees with the response instead of contradicting it.
- The ledger records every authenticated session again, including ones whose announcement failed.
- The response body gains two keys on **every** call. Additive for the web UI, which reads `opened`
  and `session_open` and ignores the rest; four exact-equality test assertions were updated.
- `strict` now has exactly one caller (`trigger`), and its justification narrows to the sentence
  that actually holds: the request names a transmission.
- The `tx_failed` event and its ledger record are unchanged — the response body is additional
  reporting, not a substitute for either.

## Out of scope

- **`trigger`.** Unchanged, deliberately.
- **The two bridge relay loops.** Still the recorded dependency from ADR 0151: they must be hardened
  before AM is selectable by an operator, i.e. before the UI cycle. A raising key-up still kills the
  Mumble→RF task and the reflector→RF drain loop.
- **The web UI.** No renderer for `announced`/`announce_error` yet; the operator learns of a refused
  announcement through the response, `/events` and the ledger.
- **Carving `/auth/session` out of `api.md`'s "a 503 can come from any route" note.** It remains
  true: the route still calls `radio.status()` for its status publish, which can raise on a dead
  backend.
- **Any hardware claim.** The radio is not flashed with F7; the refusal is modelled by fakes and has
  not been measured on the air.
