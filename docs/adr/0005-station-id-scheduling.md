# 0005 — Station ID scheduling model

Status: Accepted

## Context

CLAUDE.md guardrail 5 (Part 97): every transmission the server makes is the licensee's
station, so the controller must identify automatically — at least every 10 minutes while
transmitting and at the end of a session. Cycle 3 produced the first transmission (authed
`"1"` announces the time) but left it **un-ID'd on purpose**; HANDOFF marks automatic ID as
**the gate on hardware**. This ADR fixes the *scheduling* model. Real CW/voice synthesis is
deliberately out of scope — it needs the audio-format ADR (rate/width/channels) first — so
the ID audio is a deterministic stub, exactly as `StubTts` stubs speech, and the whole
scheduler is unit-tested on a fake clock with no real sleeps.

## Decision

- **`StationId` is the single transmit seam; it owns the radio.** The `Dispatcher` no longer
  holds a `Radio` — it holds a `StationId` and transmits through it. `StationId` is the only
  thing that calls `radio.transmit`. All three transmit paths (service content, forced
  periodic ID, sign-off ID) funnel through it, so "no transmission goes out un-ID'd" is true
  **by construction**: there is no other way to reach the radio from the services layer.

- **Callsign is fail-loud config with no default.** `RADIO_CALLSIGN` via
  `load_callsign(env)`; unset/empty raises `RuntimeError`. A station cannot legally transmit
  without a real callsign, so — unlike `RADIO_TZ`, which has a marked `UTC` default because
  timezone is not safety-relevant — a missing callsign must fail loudly rather than key up
  with a placeholder. This mirrors `load_totp_secret`.

- **Interval is config with a marked default and a hard ceiling.** `RADIO_ID_INTERVAL` via
  `load_id_interval(env)`; unset → `DEFAULT_ID_INTERVAL` (600 s). A value **above**
  `MAX_ID_INTERVAL` (600 s) is **rejected** (raises), not clamped — a too-long interval is a
  regulatory misconfiguration to fix, so fail loud rather than silently identify too rarely.
  Non-numeric and non-positive values also raise.

- **`IdEncoder` is a `Protocol`; `StubId` is deterministic.** `encode(callsign) -> AudioFrame`,
  one method mirroring `TtsEngine`. `StubId.encode` returns `b"<id:" + callsign + b">"`, a
  pure function of the callsign, so `tx_log` is asserted with exact bytes. Real `CwId` /
  `VoiceId` implement the same contract and land after the audio-format ADR; nothing above
  the encoder changes when they do.

- **Inclusion rule — ID rides in the same over, when due.** `StationId.transmit(audio)`
  prepends the ID into the same frame (`encode(callsign) + audio`, one keyup) when an ID is
  *due*, else sends the content alone. Due ≡ `last_id is None` (nothing identified yet this
  session — this is "the first transmission since the session opened") **OR**
  `now - last_id >= interval`. Every transmit marks the session as having transmitted.
  Within-interval transmissions do not repeat the ID.

- **The timer is measured from the last ID, not the last transmission.** The Part 97
  invariant is "≤10 minutes since the last **identification**." Measuring from `last_id`
  keeps a continuously-transmitting session legal (it re-IDs every interval even while
  traffic flows); measuring from the last transmission would let a chatty session slip past
  10 minutes without ID. Prepending the ID counts as identifying, so it advances `last_id`.

- **Forced periodic ID — `check(now)`.** A clock-driven safety net for a session that goes
  quiet past the interval: emits an **ID-only** over and advances `last_id` iff the session
  has transmitted and is due; otherwise a no-op. Returns whether it fired. A real scheduler
  task calls it periodically in prod; tests drive it with the fake clock. It never sleeps.

- **Sign-off ID — `sign_off(now)`.** At session end, emit a closing **ID-only** over iff the
  station transmitted during the session (a session that never keyed up needs no ID), then
  reset per-session state. Returns whether an ID was sent.

- **Per-session reset — `begin_session(now)`.** Resets `transmitted_this_session` and
  `last_id`. `sign_off` resets too; `begin_session` covers the path where a session ends by
  inactivity timeout with no explicit sign-off, so a stale `last_id` cannot suppress the
  next session's first ID.

- **ID rides in one frame with the content.** Chosen over a separate ID transmission (two
  keyups) so the transmission literally *carries* its ID in a single over. This relies on
  audio frames concatenating — valid for same-format PCM, which the audio-format ADR will
  pin; the stub bytes concatenate trivially for assertion.

## Consequences

- After this cycle the transmit path is **legality-clean**: no service transmission is
  un-ID'd, and periodic + sign-off ID exist and are asserted end-to-end on a fake clock.
- Because the `Dispatcher` holds a `StationId` (not a `Radio`), a service transmission
  cannot structurally bypass ID — the guardrail is enforced by the type wiring, not by
  convention.
- **Still blocked on hardware.** Real audio/tone synthesis (`CwId`/`VoiceId`, real TTS) and
  the audio-format ADR must land before anything reaches RF. The ID audio is opaque stub
  `bytes`.
- **Lifecycle wiring is deferred.** `begin_session`/`check`/`sign_off` are exposed and
  unit-tested, but calling them from real session-open (`ACCEPTED`), a periodic scheduler
  task, and session close/inactivity is the controller/API cycle's job. This keeps the
  cycle to the scheduling logic.
- Single active session per `StationId`, in-memory state. A multi-caller controller that
  needs concurrent sessions would key ID state per session; out of scope now.
