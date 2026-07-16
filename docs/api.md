# API reference

> **For developers.** This describes the HTTP/WebSocket interface for writing software against
> radio-server. To operate a station you never need this — see **[Using your station](using-it.md)**.

The server exposes one HTTP surface: a token-gated REST API plus three WebSocket streams. It is
a thin, honest layer over the injected `Radio` backend — see
[ADR 0011](adr/0011-api-layer.md) for the design, and [architecture.md](architecture.md) for how
it sits above the rest of the stack.

All examples assume the server is running against the mock backend on `http://127.0.0.1:8000`
with `RADIO_API_TOKEN=dev-lan-secret` (see the [README](../README.md#quickstart)).

## Authentication

There is **one** shared secret for this HTTP surface — the LAN API token (`RADIO_API_TOKEN`).
It is entirely separate from the over-RF TOTP auth that gates transmitting; see
[operating.md](operating.md#two-auth-planes).

- **REST** — send `Authorization: Bearer <token>`. Missing/invalid → **`401`** with
  `WWW-Authenticate: Bearer`. The token is compared in constant time (`hmac.compare_digest`).
- **WebSocket** — browsers cannot set handshake headers, so the token rides a query parameter:
  `?token=<token>`. Missing/invalid → the handshake is closed with code **`1008`** *before*
  `accept()` (closed by default, like REST).

The API is closed by default: `RADIO_API_TOKEN` has no default and the server fails loud if it
is unset, rather than serving open.

## REST endpoints

Every REST route requires the bearer token. Two groups: the **shared surface** (always present)
and the **CAT surface** (present in the API but capability-gated per backend — see
[capability gating](#capability-gating) below).

### Shared surface

#### `GET /capabilities`

Returns the capabilities the current backend advertises, as a sorted JSON string array.

```json
["ptt", "receive", "status", "transmit"]
```

A full-CAT backend additionally lists `scan`, `set_channel`, `set_frequency`, `set_mode`,
`set_tone`. Use this to decide which controls to enable.

#### `GET /status`

A point-in-time snapshot plus the controller block.

```json
{
  "backend": "mock",
  "transmitting": false,
  "busy": false,
  "frequency": 146520000,
  "channel": null,
  "tone": null,
  "mode": "FM",
  "controller": null,
  "scan": { "running": false, "frequency": null },
  "link": null
}
```

The field is `transmitting`, not `ptt`. The four CAT fields (`frequency`, `channel`, `tone`,
`mode`) are `null` on an audio-only backend. `controller` is `null` when no controller loop was
wired; otherwise it is `{"running": <bool>, "session_open": <bool>}`. `scan` reflects the background
scan runner: `{"running": <bool>, "frequency": <hz or null>}` (running is always `false` on an
audio-only backend, which cannot scan). `link` is `null` when no `[[mumble.servers]]` entries are
configured; otherwise it carries `{active, entries: [...]}` (see `GET /link/status`).

#### `POST /ptt`

Body: `{"on": true}`. Keys or unkeys PTT. Returns the fresh status snapshot dict. Publishes a
`ptt` event and then a `status` event on `/events`.

#### `POST /transmit`

Body: **raw PCM bytes** (not JSON), canonical format (48 kHz, 16-bit signed LE, mono). Wraps the
body in one audio frame and transmits it. Returns `{"transmitted_bytes": <int>}`. Publishes a
`status` event.

> For continuous/live transmit from a browser mic, use the [`/audio/tx`](#audiotx) WebSocket
> instead — `POST /transmit` is a one-shot buffer.

### CAT surface (capability-gated)

These exist on every deployment but return **`501`** when the backend lacks the capability (see
[capability gating](#capability-gating)). On a full-CAT backend they succeed and return the
status dict.

| Method | Path | Body | Capability |
| --- | --- | --- | --- |
| `POST` | `/frequency` | `{"hz": <int>}` | `set_frequency` |
| `POST` | `/channel` | `{"n": <int>}` | `set_channel` |
| `POST` | `/tone` | `{"tone": <float>` or `null}` | `set_tone` |
| `POST` | `/mode` | `{"mode": "<str>"}` | `set_mode` |
| `POST` | `/scan` | `ScanBody` (below) | `scan` |
| `POST` | `/scan/stop` | — | `scan` |

**`/scan` body** — provide *exactly one* addressing form, or get **`422`**:

```json
{ "frequencies": [146520000, 146940000], "lockout": [], "priority": null }
```

or a range:

```json
{ "start_hz": 146000000, "stop_hz": 147000000, "step_hz": 25000,
  "lockout": [], "priority": null }
```

`lockout` frequencies are skipped; `priority` (if set) is re-checked between steps. A malformed plan
(neither or both addressing forms, or an invalid range) → **`422`**.

`/scan` is **non-blocking** (ADR 0028): it starts a background scan and returns
`{"scanning": true, "status": {...}}` immediately. The scan is a continuous carrier/timed/hold
resume-mode loop that streams `scan` events (`scanning` → `active` → `dwelling`, `resumed`) on
`/events` and pauses while TX holds the radio. Only **one scan runs at a time** — a `/scan` while one
is already running returns **`409`**.

**`POST /scan/stop`** — no body. Signals the running scan to stop; it ends cleanly at the next tick
boundary (no mid-tune kill), drops to idle, and emits a `scan` event with phase `stopped`. Returns
`{"scanning": false, "stopped": <bool>}` where `stopped` is whether a scan was actually running.
**Idempotent** — a stop when nothing is scanning is a clean no-op ack. Capability-gated like `/scan`
(**`501`** naming `"scan"` on an audio-only backend).

### `POST /controller`

Body: `{"on": true}` to start the live controller loop, `{"on": false}` to stop it. Returns
`{"controller": {...}}`. When no controller was wired into the deployment (e.g. `RADIO_TOTP_SECRET`
unset), returns **`503`** with detail `"controller not configured in this deployment"` — a loud
failure, not a silent no-op.

### `GET /auth/totp`

The **current** over-the-air login code, for the web UI's code card — so the operator can key a
DTMF login at the radio without an authenticator app. Returns
`{"code": "123456", "seconds_remaining": n, "interval": 30}`; **`503`** when no TOTP secret is
enrolled. The response never contains the secret (ADR 0025), and reading the code burns nothing —
keying it over RF still passes the single-use check. Posture: the LAN token already transmits
directly, so this grants the token holder no new capability (see
[operating.md](operating.md)).

### `GET /link/status` and `POST /link` (ADR 0041/0042)

The Mumble/Murmur link (bridge RF audio to a Mumble channel). Present when `[[mumble.servers]]`
entries are configured. **One link is active at a time** — connecting an entry switches away from
the current one.

- **`GET /link/status`** → `{"link": {"active": name|null, "entries": [...]}}` — every configured
  entry (`name`, `host`, `port`, `channel`, `dtmf`, `tx_to_rf`, `autoconnect`) plus
  live state (`running`, and `connected`/`peers` on the active one). The same block also appears
  under `link` in `GET /status`. `{"link": null}` when no entries are configured. The station's
  Mumble nick is not per-entry: it is always `<callsign> (radio-server)` (from
  `station.callsign`).
- **`POST /link`** — body `{"entry": "home", "on": true}` to connect that entry (switch semantics),
  `{"on": false}` to disconnect. `entry` may be omitted on connect only when exactly one entry is
  configured (`422` otherwise); an unknown name is a `404`. When no entries are configured,
  returns **`503`** — a loud failure, not a silent no-op. Returns `{"link": {...}}`.
- Every transition (browser, DTMF combo, autoconnect) is pushed on `/events` as a
  `{"type": "link", "data": {entry, state, active, entries}}` frame; a connect that fails
  (`state: "error"`) carries the reason in `detail`.
- A connect that fails synchronously — e.g. the `mumble` extra / system libopus is not installed —
  returns **`503`** with the actionable reason (including the install command) in `detail`,
  never a bare 500.

Settings-side, the entry list is edited via **`GET`/`PUT /settings/mumble-servers`** (whole-list
replace, validated atomically, restart-applied) and each entry's Murmur password via the
write-only **`POST /settings/mumble-servers/{name}/password`** (it lands on the secrets channel as
`mumble_password_<name>`, never in `radio.toml`, and is never read back).

Bridged transmissions onto RF are auto-identified (Part 97): the same streaming station-ID that
covers the `/audio/tx` talker prepends the callsign when due. Set an entry's `tx_to_rf = false` to
run it receive-only (RF → Mumble monitor, never keys the transmitter).

### Capability gating

The load-bearing behavior of the CAT surface (guardrail 3): rather than silently no-op'ing an
unsupported operation, the API returns **`501 Not Implemented`** and *names the missing
capability* in the body, so a client can grey out exactly the right control.

```http
POST /frequency        (on an audio-only backend)
→ 501 Not Implemented
```
```json
{
  "detail": {
    "error": "capability not supported in this mode",
    "capability": "set_frequency"
  }
}
```

(FastAPI wraps `HTTPException.detail`, hence the outer `detail` key.) The `capability` value is
one of `set_frequency`, `set_channel`, `set_tone`, `set_mode`, `scan`. Reachable from all five
CAT endpoints.

### Settings & secrets (ADR 0026)

A thin, schema-driven surface over the `radio.toml` config (ADR 0025) — what the settings UI reads
and writes. Changes are **restart-to-apply**: writes persist to file but do not hot-reload the
running server, so every write response carries `"restart_required"` / `"restart_required": true`.

**`GET /settings`** — the schema with current values. Returns
`{"settings": [...], "secrets": {...}, "apply": "restart"}`. Each settings entry is
`{key, group, type, default, value, required, description}` (plus `choices` for `type: "enum"`);
`type` is one of `string`, `integer`, `number`, `boolean`, `enum`. A required setting that is unset
serializes with `value: null`. The `secrets` block reports **presence only** —
`{"api_token": {"set": true|false}, "totp_secret": {"set": true|false}}`. **A secret value is never
returned** (secrets are not part of the settings schema).

**`PATCH /settings`** — body `{"values": {"<key>": <value>, ...}}`. Validates the **whole** patch
against the schema and rejects it atomically: an invalid value, an unknown key, or a secret key
(`api_token`/`totp_secret`) returns **`400`** naming the problem, and **nothing is written**. On
success it round-trips `radio.toml` (preserving comments) and returns
`{"updated": [...], "restart_required": [...], "apply": "restart"}`.

**`POST /settings/secrets/api-token/rotate`** — write-only. Optional body `{"token": "<new>"}` to set
an explicit token; omitted → the server generates one. Returns `{"api_token": "<new>",
"restart_required": true, "note": ...}` — the token is shown **once**; re-authenticate with it after
a restart.

**`POST /settings/secrets/totp/enroll`** — write-only. Optional body `{"account": "<label>"}`.
Generates a **fresh** TOTP secret and returns `{"provisioning_uri": "otpauth://...", "secret": "...",
"restart_required": true, "note": ...}` — shown **once** for re-enrollment. It never returns an
existing secret.

All four are token-gated like the rest of the API (`401` without a valid bearer token).

### REST status codes summary

| Code | When |
| --- | --- |
| `200` | Success. |
| `400` | `PATCH /settings` with an invalid value, unknown key, or a secret key (body names it). |
| `401` | Missing/invalid bearer token (`WWW-Authenticate: Bearer`). |
| `409` | `POST /scan` while a scan is already running (one scan at a time). |
| `422` | `/scan` with a malformed addressing plan. |
| `501` | CAT endpoint on a backend lacking that capability (body names it). |
| `503` | `POST /controller` when no controller is configured; `POST /link` when no Mumble link is configured. |

## WebSocket streams

Three sockets, all authenticated the same way (`?token=`, bad token → close `1008` pre-accept).

### `/events`

A JSON event stream. On connect (after `accept()`) the server immediately sends a `status`
snapshot, then pushes events as state changes. Each frame is:

```json
{ "type": "<type>", "data": { ... } }
```

Event taxonomy:

| `type` | `data` | Emitted when |
| --- | --- | --- |
| `status` | full `RadioStatus` fields | any state change, and once on connect |
| `ptt` | `{"on": <bool>}` | PTT keys/unkeys (REST `/ptt` or streaming TX) |
| `scan` | `{"phase", "frequency", "channel"}` | scan progress: `scanning`/`active`/`dwelling`/`resumed` from the engine, `stopped` when the background runner tears the scan down (ADR 0028) |
| `arbiter` | `{"mode": "idle"｜"receiving"｜"transmitting"}` | duplex arbiter mode transitions |
| `session` | `{"phase", ...}` | controller session lifecycle (open/close, forced ID) |
| `auth` | `{"result": "accepted"｜"rejected"}` | an over-RF auth attempt — **the result only, never the code** |
| `command` | `{"service": <name>}` | a dispatched voice-service command |

(The `"busy"` name is reserved in the code but not currently emitted.) The normal path closes on
client disconnect with no application close code.

### `/audio/rx`

Binary **canonical PCM out** — what the radio is hearing. After `accept()`, the **first message
is a JSON ready handshake declaring the format**, then every subsequent message is a raw binary
PCM frame:

```json
{ "status": "ready", "format": { "rate": 48000, "width": 2, "channels": 1 } }
```

That is 48 kHz, 16-bit signed little-endian, mono. Read the header to configure playback (or
assume canonical — older clients that ignore it still work). The RX pump is demand-driven:
started on the first listener, stopped on the last. Introduced in
[ADR 0014](adr/0014-rx-audio-streaming.md); the format handshake in
[ADR 0023](adr/0023-rx-playback.md). Bad token → close `1008`; otherwise closes on disconnect.

### `/audio/tx`

Binary **canonical PCM in** — stream audio to transmit; the server keys PTT for the stream's
duration and drops it on close or idle. PTT is keyed via the audio/serial path, **never** over
CAT (guardrail 2). See [ADR 0016](adr/0016-tx-audio-ingest.md) and
[ADR 0024](adr/0024-tx-mic-capture.md).

Handshake sequence:

1. **Auth** — bad `?token=` → close `1008` (pre-accept).
2. **Single-talker guard** — one transmitter, one talker. A second concurrent client is
   **accepted**, sent `{"status": "busy"}`, then closed with **`1013`**. The accept-then-inform
   ordering is deliberate: a browser cannot observe a *pre-accept* close code (it surfaces as a
   generic `1006`), so the app accepts, sends a `busy` message the client can read, then closes
   `1013`. This path never enters the session teardown, so it never releases the *other* talker's
   slot.
3. **Format handshake** — the first message must be a JSON format declaration equal to canonical:
   `{"rate": 48000, "width": 2, "channels": 1}`. Malformed / non-canonical → close **`1003`**
   before any audio is accepted or the transmitter keys. No header within the idle timeout → the
   socket just returns (no explicit code).
4. **Ready ack** — on success the server replies
   `{"status": "ready", "format": {"rate": 48000, "width": 2, "channels": 1}}`.
5. **Binary loop** — send whole-sample PCM frames. PTT keys on the first real frame. A stall
   longer than `tx.idle_timeout` (default 2 s) drops PTT. A mid-stream non-canonical frame
   → close **`1003`**. On any exit (clean close, idle, format error, disconnect) the server drops
   PTT and frees the talker slot.

### WebSocket close codes

| Code | Socket(s) | Meaning |
| --- | --- | --- |
| `1008` | all three | invalid/missing `?token=` (closed pre-accept) |
| `1013` | `/audio/tx` | transmitter busy — a second talker (after an accept + `{"status":"busy"}` message) |
| `1003` | `/audio/tx` | unsupported/malformed PCM format (header or mid-stream frame) |

## See also

- [operating.md](operating.md) — the two auth planes and Part-97 behavior in depth.
- [architecture.md](architecture.md) — where the API sits in the stack.
- ADRs: [0011 API layer](adr/0011-api-layer.md),
  [0014 RX streaming](adr/0014-rx-audio-streaming.md),
  [0016 TX ingest](adr/0016-tx-audio-ingest.md),
  [0023 RX playback](adr/0023-rx-playback.md),
  [0024 TX mic capture](adr/0024-tx-mic-capture.md).
