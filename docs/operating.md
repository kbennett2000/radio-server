# Operating guide (Part 97)

> **New here?** This is the detailed, technical reference. If you just want to get on the air, start
> with **[Using your station](using-it.md)** — it covers logging in and the spoken services in plain
> language.

This is the doc for the licensed operator: how the station authenticates callers, how it
identifies itself, what it logs, and — importantly — what "secure" does and does not mean here.
Rationale for each decision is in the linked ADRs.

Every transmission the server makes is *your* station under *your* license. The controller
enforces the required behavior (identification, single-use auth) rather than leaving it to you to
remember.

## Two auth planes

The system has **two entirely separate** authentication mechanisms. They guard different things,
face different threats, and share no code. Do not conflate them.

### 1. Over-RF DTMF / TOTP — gating the transmitter

This is what stops any random person with an HT from keying your station's services. A caller
sends a TOTP code as DTMF digits; the controller verifies it before opening a session. See
[ADR 0003 (session/auth state machine)](adr/0003-session-auth-state-machine.md). Implemented in
[`radio_server/auth/`](../radio_server/auth/).

- **Secret:** `RADIO_TOTP_SECRET` (base32), fail-loud if unset. Like the LAN token, this is a
  secret and lives in `radio-secrets.toml` (chmod 600) or the environment — never in `radio.toml`.
- **Window:** the verifier accepts ±1 time step (`valid_window = 1`), i.e. the previous, current,
  and next 30-second TOTP step — roughly a **90-second total acceptance span**, to tolerate
  over-the-air and human latency.
- **Single-use (burn):** a code that verifies is *burned* — the `(code, step)` pair is recorded
  and a replay of the same code within its window is rejected. Consumed codes are pruned as their
  window passes. This is the key property: a code sniffed off the air cannot be replayed.
- **Session inactivity timeout:** an authenticated session is demoted back to unauthenticated
  after ~300 s of inactivity (checked on each inbound DTMF event and on a timeout sweep), so a
  session can't be left open indefinitely.
- **The one un-gated combo:** the Mumble link-off combo (`98#` by default) works **without** a
  session ([ADR 0043](adr/0043-ungated-link-off.md)). It only ever removes capability — it drops
  the link and stops Mumble→RF audio — so it's deliberately allowed for a logged-out operator
  whose session timed out while listening to a net. Anyone on frequency can key it; the accepted
  worst case is a dropped link and a short ID'd "Link off." over. Connect combos, and everything
  else, still require the login.

### 2. LAN API token — gating the HTTP surface

This gates the REST/WebSocket API on your network — a completely different surface with a
different threat model (a wired LAN, not a broadcast channel). See
[`radio_server/api/auth.py`](../radio_server/api/auth.py) and [api.md](api.md#authentication).

- **Secret:** `RADIO_API_TOKEN`, fail-loud if unset (the API is closed by default). As a secret it
  lives in `radio-secrets.toml` (chmod 600) or the environment — never in `radio.toml`.
- **Mechanism:** a plain static bearer secret, constant-time compared (`hmac.compare_digest`).
  No window, no burn, no per-caller state machine — it deliberately reuses none of the TOTP
  machinery. REST sends it as `Authorization: Bearer …`; WebSockets pass `?token=…`.

The two planes are independent: holding the LAN token lets you drive the API; keying a service
*over the air* still requires a valid TOTP code, and vice versa.

One deliberate bridge between the planes exists for the operator's convenience: the token-gated
`GET /auth/totp` (the web UI's "Over-the-air login code" card) shows the **current** TOTP code so
the operator can key a DTMF login at the radio without their phone. This grants the token holder
no capability they lack — the LAN token already transmits directly (`/ptt`, the service trigger
endpoints) — and the *secret* is never exposed (ADR 0025); the displayed code stays single-use
when keyed (`verify_and_burn`).

## Station identification

Automatic station ID is required controller behavior, not an optional feature (Part 97). See
[ADR 0005 (station-ID scheduling)](adr/0005-station-id-scheduling.md),
[ADR 0007 (CW encoder)](adr/0007-cw-id-encoder.md), and
[ADR 0010 (voice ID)](adr/0010-voice-id.md). Implemented in
[`radio_server/services/station_id.py`](../radio_server/services/station_id.py).

- **Callsign:** `station.callsign`, fail-loud if unset — a station cannot legally transmit without
  one, so the loader refuses rather than transmitting unidentified.
- **Interval:** `station.id_interval`, default **600 s**. Values **above 600 are rejected** (not
  clamped) — the Part-97 10-minute ceiling is a hard limit here. "Due" is measured from the last
  ID, and when an ID comes due mid-session it is prepended into the same over.
- **Forced ID:** if the station has transmitted this session and an ID is due, the controller
  forces an ID-only transmission.
- **Sign-off:** at session end the station sends a closing ID — but only if it actually
  transmitted during the session (no transmission, no ID needed).
- **Mode:** `station.id_mode` = `cw` (default) or `voice`. `voice` requires a configured Piper
  voice and does **not** silently fall back to CW. CW speed and sidetone are `station.cw_wpm`
  (default 20 wpm) and `station.cw_tone_hz` (default 600 Hz).

## The operating log

A passive, append-only JSONL ledger of station activity — the QSO/operating record. See
[ADR 0018 (event log)](adr/0018-event-log.md). Implemented in
[`radio_server/eventlog/`](../radio_server/eventlog/).

- **Path:** `logging.path`, default `radio-server.jsonl`, opened fail-loud at startup if
  unwritable. Flushed per write.
- **What it records:** PTT key-up/key-down (with keyed duration), scan hits/phases, session
  open/close (with reason and whether the station signed off), station-ID transmissions (with
  callsign and mode), and auth/command outcomes.
- **The no-secrets rule:** the log **whitelists** specific fields per record type and *never*
  copies event payloads wholesale. An auth-rejected record says only *that* auth failed and
  when — never the code, the digits, or any secret. TOTP codes, DTMF digits, and the API token
  never reach the ledger.
- **Failure isolation:** a logging fault is caught and the record dropped — a disk problem can
  never break a transmission or the event pump.

## Recording

Opt-in audio capture to WAV. See [ADR 0020 (recording)](adr/0020-audio-recording.md) and
[ADR 0021 (recording safety & TX)](adr/0021-recording-safety-and-tx.md). Controlled by
`recording.enabled` / `recording.tx` (independent toggles), `recording.path`,
`recording.mode` (`gated`; `full` is unimplemented), and `recording.max_seconds` (a
per-segment cap, always on).

**Squelch-off warning:** with `recording.enabled` on *and* `audio.squelch = "off"` (the default)
there is no gate-close edge, so RX is not segmented per-transmission — it accumulates into
time-capped segments bounded only by `recording.max_seconds`. This is bounded and safe but
surprising, so the server logs a one-time warning at startup advising `audio.squelch = "audio"`
or `"cat"` for one WAV per
received transmission. It warns; it does not fail.

## Received audio sounds distorted / clipping

If the browser Monitor (or a recording) sounds harsh, crunchy, or clipped, it is almost always an
**input-level** problem, not the app: the server relays received PCM byte-for-byte — there is no
software RX gain — so whatever the sound-card ADC captured is exactly what you hear. Audio driven in
too hot squares off (clips) at the ADC before the app ever sees it.

Fix it at the capture stage:

1. Measure it: `python -m radio_server.doctor --rx-level` reports the received peak in dBFS and warns
   when you are near clipping.
2. Turn the level **down** — the radio's volume knob (the AIOC/SignaLink taps the speaker line) and
   the card's capture level in `alsamixer` (see
   [hardware-bringup.md](hardware-bringup.md#audio-levels--squelch-the-i-hear-nothing-step) for the
   AIOC capture control). Aim for peaks comfortably below full scale.

For a Mumble link, clipping on the received channel comes from the **far-end** sender's level — same
fix, on their station.

The Monitor card's **Vol** slider is a *playback* control (per-browser, with a little default
headroom so a hot channel can't overshoot into clipping in the browser's output stage). It's handy
for taming a loud channel, but it lowers volume *after* the fact — it cannot restore audio already
clipped at the capture ADC. That's still a level fix on the sending side.

## Security reality

Read this before trusting the station with anything consequential.

- **Auth is gated access, not confidentiality.** Everything on RF is in the clear — there is no
  encryption and (under Part 97) can't be. TOTP does not hide anything; it only makes a *replay*
  fail, because each code is single-use within a ~90-second window.
- **Match auth strength to consequence.** A service that just announces the time is low-stakes;
  anything that keys the transmitter in a more consequential way should be gated harder than
  "announce the time." Treat the ability to transmit as the privileged operation.
- **The LAN token is only as private as your LAN.** It is a static secret sent on the wire (TLS
  is a deployment concern, pending). Generate a strong random token; don't reuse it across
  stations.

## Configuration guardrails

The loaders fail loud rather than transmit in an unsafe or illegal state:

- `station.callsign` unset → refuses to run the transmitting path (no unidentified transmission).
- `RADIO_TOTP_SECRET` unset → the live controller loop is not wired (`/controller` reports 503).
- `station.id_interval` > 600 → rejected (Part-97 ceiling).
- Any set-but-malformed numeric config (VAD levels, timeouts, WPM, …) → raises at load, rather
  than silently falling back to a default.

## See also

- [configuration.md](configuration.md) — the full configuration reference (with the annotated
  [`radio.toml.example`](../radio.toml.example)).
- [api.md](api.md) — the HTTP surface and its token auth.
- [architecture.md](architecture.md) — where auth, station ID, and the log sit in the stack.
