# API reference

> **For developers.** This describes the HTTP/WebSocket interface for writing software against
> radio-server. To operate a station you never need this — see **[Using your station](using-it.md)**.

The server exposes one HTTP surface: a token-gated REST API plus seven WebSocket streams. It is
a thin, honest layer over the injected `Radio` backend — see
[ADR 0011](adr/0011-api-layer.md) for the design, and [architecture.md](architecture.md) for how
it sits above the rest of the stack.

All examples assume the server is running against the mock backend on `http://127.0.0.1:8000`
with `RADIO_API_TOKEN=dev-lan-secret` (see [Try it first](getting-started.md)).

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

A full-CAT backend additionally lists `clear_broadcast_fm`, `scan`, `set_channel`,
`set_frequency`, `set_mode`, `set_modulation`, `set_power`, `set_split`, `set_tone` — the nine
members of `CAT_CAPS`. Real backends advertise a subset: the `uvk5` and `kv4p` backends have `scan`
but not `set_power`, the `baofeng` backend with a UV-K5 tuner has `set_power` but not `scan`, and
`set_modulation` needs both F7 firmware **and** a `setvfo`/`hybrid` tuner — a `baofeng` on the
stock-firmware `eeprom` tuner has every other tuning capability and not that one. Use this to decide
which controls to enable, and never assume the split from the backend name.

`clear_broadcast_fm` is the one capability that is **earned rather than configured**, and it has no
route of its own. Every other member is a claim derived from configuration; this one appears only
after a radio has actually answered `0x087A`, because the firmware that implements it is a fork
branch that is neither merged nor flashed — advertising it from a config key would have every
station claiming a firmware generation nobody is running. Its absence therefore means "this radio
has not shown it can do this", not "this server would refuse". The server clears broadcast FM once
at startup; there is deliberately no way to turn it **on**, so there is no endpoint to gate. Use its
presence to know whether `broadcast_fm` in `GET /status` can ever be non-null (ADR 0157).

#### `GET /status`

A point-in-time snapshot plus the controller block.

```json
{
  "backend": "mock",
  "transmitting": false,
  "busy": false,
  "frequency": 146520000,
  "tx_frequency": null,
  "channel": null,
  "tone": null,
  "mode": "FM",
  "rssi": null,
  "pa": null,
  "power": null,
  "tx_ready_in": null,
  "tune_persist": null,
  "modulation": null,
  "tx_ok": null,
  "broadcast_fm": null,
  "controller": null,
  "scan": { "running": false, "frequency": null },
  "link": null,
  "dstar": null,
  "dvap": null
}
```

The field is `transmitting`, not `ptt`. The CAT fields (`frequency`, `tx_frequency`, `channel`,
`tone`, `mode`) are `null` on an audio-only backend. `controller` is `null` when no controller loop
was wired; otherwise it is `{"running": <bool>, "session_open": <bool>}`. `scan` reflects the
background scan runner: `{"running": <bool>, "frequency": <hz or null>}` (running is always `false`
on an audio-only backend, which cannot scan). `link` is `null` when no `[[mumble.servers]]` entries
are configured; otherwise it carries `{active, entries: [...]}` (see `GET /link/status`).
`dstar` and `dvap` mirror `GET /dstar/status` and `GET /dvap/status` and are `null` when those
features are not configured — note that the `dvap` block here is served from cache and does no I/O,
unlike the dedicated route, which refreshes from the gateway.

`tx_frequency` is the transmit leg when a repeater split is armed, and `null` for simplex — never a
mirror of `frequency`. Every tuning call clears it (ADR 0133), so it is the field to read when a
repeater is not opening.

Two diagnostic fields carry numbers that used to require stopping the service and opening the serial
port by hand. Nothing decides anything on either.

- **`rssi`** — the raw received-signal reading behind `busy`, on whatever scale the backend's
  hardware uses, or `null` where there is none (or while keyed). `null` means *no reading*, never
  "no signal".
- **`pa`** — what the power amplifier was set to at the **last key-up**, or `null` before the first
  transmission and on any backend that cannot see one. Survives un-key on purpose: the question it
  answers is asked after the over.

  ```json
  { "bias": 12, "gain": 136, "band_matched": false, "tx_frequency": 147555000 }
  ```

  `band_matched: false` is the one to read. On the UV-K5 dock the firmware sets the PA up from the
  radio's **own front-panel VFO**, not from the frequency the host tuned, and the per-band bias
  calibration lives in flash the dock cannot read — so transmitting outside the radio's VFO band
  uses the other band's calibration and the radiated power is uncharacterised (ADR 0128/0132/0134).
  The server logs a warning naming it, and the fix is on the radio's front panel, not in software.

Five more fields are reported only where a backend can answer them, and are `null` everywhere
else. `tx_ready_in` and `tune_persist` are specific to a UV-K5 tuned over an AIOC; `power`,
`modulation` and `tx_ok` are also implemented by the mock, so the examples in this document
exercise them.

- **`power`** — how hard the radio transmits: `"low"`, `"mid"` or `"high"`. Reported from what the
  radio **confirmed**, not what was asked: `0x0873` is answered with the `OUTPUT_POWER` read back out
  of the radio's own VFO. `null` before the first tune (the radio is on whatever its front panel says
  and the host cannot see it), and `null` for a level this server did not choose — the front panel
  reaches `LOW2`..`LOW5` and `USER`, which have no name here.

- **`tx_ready_in`** — seconds until the radio will accept a key-up, or `null` when it will accept one
  now. The firmware mutes its transmitter for six seconds after **any** EEPROM conversation — the
  handshake and reads arm it as well as the writes — and it refuses a key-up *and* cuts an over
  already in progress. Always relative, never an absolute deadline, so a stale snapshot cannot become
  a lie. The server enforces the wait at key-up regardless; this exists so the UI stops offering a
  button that will do nothing (ADR 0144/0145).
- **`tune_persist`** — whether the channels the server tunes are also **stored on the radio**
  (`POST /tuning/persist`). `null` means the backend has no such choice, which is everything except a
  UV-K5 on the `hybrid` tuner — `null` and `false` are different answers, so a UI can hide the control
  rather than render one that does nothing.
- **`modulation`** — the **demodulator** the radio is on: `"FM"`, `"AM"` or `null` (ADR 0150). Not
  `mode`, which is wide/narrow **bandwidth** — a radio is narrow-FM *or* wide-FM, and either of those
  or AM. Reported from what the radio **confirmed**: `0x0878` carries the modulation read back out of
  its own VFO after the firmware applied it, not the value it was handed. `null` means *not known* —
  on a backend that cannot set one, and where this server's startup assertion did not land (radio
  switched off, or pre-F7 firmware with no set-modulation command). On the `baofeng` backend with a
  `setvfo` or `hybrid` tuner it is normally `"FM"` from the moment the server starts, because the
  server **states** FM at construction and the radio confirms it on `0x0878` (ADR 0155). That is
  still not the firmware's own seeded FM being adopted — it is a value this server chose and the
  radio agreed to, and the distinction matters because `tx_ok` is measured in the same round trip.
  It is never *defaulted* to `"FM"`: an unasserted demodulator reports `null`.
- **`tx_ok`** — whether the radio will key its **own** transmit path right now, or `null` where that
  is not knowable. `false` is a normal operating state, not a fault: this firmware is built without
  `ENABLE_TX_WHEN_AM`, so it sets `VFO_STATE_TX_DISABLE` in any non-FM modulation — **AM is
  receive-only**. That matters because the `baofeng` backend keys through the AIOC's DTR line into
  exactly that path, while the `uvk5` dock backend's register keying does not go through it and is
  unaffected. Same radio state, different consequence per backend, and no client can infer which; so
  it is reported on every backend and never gated on the one selected. With `tx_ok: false` a key-up
  is refused with a **503** naming the reason, rather than asserting the line and transmitting
  nothing. On a tuner that can set a modulation, `null` after startup now means the server's boot
  assertion did not land — which is itself worth acting on, because until it does the server cannot
  refuse a key-up the radio would swallow (ADR 0155).
- **`broadcast_fm`** — the radio's **second receiver**, or `null`. A UV-K5 carries a BK1080
  commercial-FM chip (64–108 MHz) beside the BK4819 that everything else drives; when it runs it
  holds the speaker line the AIOC listens on, so **the station hears nothing on its own channel** —
  and transmits normally throughout, because the firmware's transmit path has no broadcast-FM
  check. The automatic station ID therefore goes out into a channel nobody is monitoring. A block
  when known, `{"on": false, "hz": 103200000}`:
    - **`on`** — is the second receiver running? `true` means the station cannot hear itself.
    - **`hz`** — the BK1080's tuning, or `null` where the radio blanked it. **Only meaningful
      together with `on`**: the receiver remembers where it was, so a real frequency is reported
      even when it is switched off. That is the frequency it *would resume on*, not what anything
      is listening to; read alone it looks like a station happily monitoring 103.2 MHz.

  The whole block is `null` when the server does not know — no such receiver, firmware older than
  F8 (which drops the command in silence), or a radio that was switched off at startup. **`null`
  and `{"on": false}` are different answers** and must not be rendered the same: the first is "we
  never asked", the second is "we asked and the station can hear". Collapsing them is how a deaf
  station gets trusted.

  This is a record of what the server **asserted** at startup, not a live reading — the server
  never re-reads it. An operator pressing the radio's own FM key afterwards is therefore invisible
  here, and the block will still say `on: false` while the station is deaf. There is no route to
  change it: the server clears broadcast FM at startup and has no way to turn it on (ADR 0157).

#### `POST /ptt`

Body: `{"on": true}`. Keys or unkeys PTT. Returns the fresh status snapshot dict. Publishes a
`ptt` event and then a `status` event on `/events`.

#### `POST /transmit`

Body: **raw PCM bytes** (not JSON), canonical format (48 kHz, 16-bit signed LE, mono). Wraps the
body in one audio frame and transmits it. Returns `{"transmitted_bytes": <int>}`. Publishes a
`status` event.

> For continuous/live transmit from a browser mic, use the [`/audio/tx`](#audiotx) WebSocket
> instead — `POST /transmit` is a one-shot buffer.

#### `GET /services`

The DTMF voice services actually wired in this deployment, as `[{"digit", "name", "description"}]`,
for the web UI's reference panel. The catalog reflects config — an operator plugin
(`local_services/`, ADR 0051) appears only when it is installed and its `enabled(settings)` gate
passes. Returns `[]` when no controller is configured.

#### `POST /services/{digit}`

Fire a DTMF service or built-in (e.g. `02` time, `01` station ID, `99` logout) from the control
operator and transmit it over the air. The LAN token is the operator's credential — there is **no
over-RF auth check and no TOTP burn** here, exactly like `/ptt` and `/transmit`, which key TX
directly. Returns the dispatch result dict; **`503`** when no controller is configured (a loud
failure, not a silent no-op). Also emits `command`/`session` events on `/events`.

### CAT surface (capability-gated)

These exist on every deployment but return **`501`** when the backend lacks the capability (see
[capability gating](#capability-gating)). On a full-CAT backend they succeed and return the
status dict.

| Method | Path | Body | Capability |
| --- | --- | --- | --- |
| `POST` | `/frequency` | `{"hz": <int>}` | `set_frequency` |
| `POST` | `/channel` | `{"n": <int>}` | `set_channel` |
| `POST` | `/split` | `{"tx_hz": <int>` or `null}` | `set_split` |
| `POST` | `/tone` | `{"tone": <float>` or `null}` | `set_tone` |
| `POST` | `/mode` | `{"mode": "<str>"}` | `set_mode` |
| `POST` | `/scan` | `ScanBody` (below) | `scan` |
| `POST` | `/scan/stop` | — | `scan` |

**`POST /split`** (ADR 0133) — arm a repeater split: transmit on `tx_hz` while receiving on the tuned
frequency. `null` (or `0`) restores simplex. The transmit leg is applied inside the radio's own key
path — tuned before the transmitter is enabled, and returned to the receive leg after the PA drops —
so the carrier can never appear on the frequency you are listening to.

**`POST /frequency` clears any armed split**, and the returned status shows `"tx_frequency": null` so
that is visible rather than inferred. This is the fail-safe direction: a transmit leg that outlived a
retune would let the next unattended transmission (a station ID on a timer) key a repeater's uplink
from a frequency nobody chose.

`tx_hz` is validated harder than `/frequency`'s `hz`, because it is the number that radiates —
**`422`** when it is out of band, off the tuning raster, more than 10 MHz from the receive frequency
(every standard 2 m / 1.25 m / 70 cm offset is well inside that; a value further out is a typo), or
crossband. `set_split` is advertised by the uvk5 backend and the mock; **kv4p returns `501`** — the
device carries separate TX/RX fields but the capability has not been proven on real RF, and an
advertised transmit capability that has never keyed is not one this project ships.

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

### Channel presets (ADR 0115/0133)

Named host-side tuning entries from the `[[presets]]` block in `radio.toml` (see
[configuration](configuration.md#channel-presets)). A preset says where to listen, optionally where to
*transmit* (a repeater split), and the CTCSS tone to send. Applied through the same CAT surface as the
tuning routes above.

| Method | Path | Body | Capability |
| --- | --- | --- | --- |
| `GET` | `/presets` | — | — (always 200) |
| `POST` | `/presets/apply` | `{"name": "<str>"}` | `set_frequency` |

**`GET /presets`** — lists the configured presets and, per entry, which fields the **current** backend
can honour. Always **`200`** (an empty list when none are configured); no state change.

```json
{ "presets": [
  { "name": "W0CRA 145.46", "frequency": 145460000, "tx_frequency": 144860000,
    "offset": -600000, "tx_tone": 107.2, "rx_tone": 107.2, "mode": "FM",
    "honoured": ["set_frequency", "set_split", "set_mode", "set_tone"],
    "unsupported": [{"field": "rx_tone", "capability": "",
                     "reason": "rx tone squelch is not implemented (v1); RSSI squelch gates receive"}] }
] }
```

`frequency` is what the radio listens on (a repeater's *output*); `tx_frequency` is what it transmits
on, `null` for simplex. **`offset` is derived and read-only** (`tx_frequency - frequency`) — it is
reported because that is how a repeater is written down, but it is not an input: `radio.toml` stores
the absolute transmit frequency.

`rx_tone` is **stored but never honoured** — nothing implements receive tone squelch, so it appears in
`unsupported` on every backend, with a `reason` rather than a capability (no `Capability` member backs
it, and inventing a string would corrupt the vocabulary the UI parses).

That empty `capability` is the discriminator the web UI filters on (ADR 0145): a **capability gap** is
per-backend news worth an alert, while a field nothing implements anywhere is not, and repeating an
unactionable warning in the same box as the actionable ones is how the actionable ones stop being read.
The API is unchanged — every consumer still receives it.

On an audio-only backend `honoured` is empty and every field appears in `unsupported`. Note that
`rx_tone` is reported with an **empty** `capability` there too — it is unhonoured everywhere, not
missing from this particular backend.

**`POST /presets/apply`** — applies a preset by `name` (case-insensitive). Writes in a fixed order:
frequency, split, mode, tone, then power. Anything the backend can't honour is **reported, never
silently dropped**:

```json
{ "applied": ["set_frequency", "set_split", "set_mode"],
  "skipped": [{"field": "tx_tone", "capability": "set_tone"}],
  "status": { ...RadioStatus... } }
```

A repeater preset applied to a backend without `set_split` (kv4p today) still tunes the **receive**
leg and reports `{"field": "tx_frequency", "capability": "set_split"}` — you can monitor the repeater,
and you are told plainly that transmitting through it will not work.

Two details of `applied` worth knowing before you diff against it:

- **`set_split` and `set_tone` are called unconditionally** where the capability exists — that is how
  a preset with no tone *clears* a leftover one — but they are listed in `applied` only when the
  preset's value is non-`null`. So `applied` under-reports the calls actually made.
- **`power` is the one field applied conditionally.** A preset that does not name a level does not
  touch it, because a power level belongs to the station rather than the channel. Every other field
  is written on every apply.

On success it pushes a `status` event on `/events`, exactly like the tuning routes. Error cases:

- **`404`** — no preset with that name.
- **`501`** — the active backend can't tune (no `set_frequency`), naming the capability — the same
  response `POST /frequency` gives on that backend. `GET /presets` still lists the entries.
- **`409`** — the radio is transmitting; a preset is refused mid-TX (it is not queued).
- **`422`** — the preset's frequency is outside the active radio's band (a pre-validated preset can
  still be out of band for a particular backend).

A running scan is stopped first (the scan owns tuning), then the preset is applied.

**`POST /modulation`** — body `{"modulation": "FM" | "AM"}`. Sets the **demodulator** and returns
the full `RadioStatus`; pushes a `status` event.

**This is not `POST /mode`.** That one sets wide/narrow **bandwidth** and its values are `FM`/`NFM`;
this one chooses what kind of signal the radio expects to find, and its values are `FM`/`AM`. Both
spell one of their values `"FM"` and they are easy to read as synonyms — they are separate settings,
reached by separate frames, and a backend can have either without the other. AM is what makes
airband receivable at all.

- **`501`** naming `set_modulation` where the backend cannot do it — everything but the mock and a
  UV-K5 on **F7** firmware behind a `setvfo`/`hybrid` tuner. A stock-firmware `eeprom` tuner has no
  such command and does not advertise it.
- **`422`** on anything but `FM`/`AM` (case-insensitive). `USB` is refused deliberately: the wire
  reserves its number but no radio here has been proven on it.
- **`409`** mid-transmission. Not politeness — switching to AM disables the radio's own transmit
  path, so doing it under a live carrier would end the over from underneath the operator.
- **`503`** when the radio does not confirm the change: it is switched off, or running pre-F7
  firmware that drops the command in silence. The message names both, because from the host they are
  the same event.

**Setting `AM` stops this station transmitting**, and the server enforces that rather than letting it
surface as dead air: with `tx_ok: false` a key-up is refused (503) instead of asserting the PTT line
into a radio that will ignore it. Set `FM` to transmit again. Persisted channel storage is untouched
by this — the demodulator is not part of a stored channel record.

**`POST /power`** — body `{"level": "low" | "mid" | "high"}`. Sets how hard the radio transmits and
returns the full `RadioStatus`; pushes a `status` event. **422** on a level that is not one of the
three, and **501** naming `set_power` on any backend that cannot set it.

**Only two backends can: the mock, and `baofeng` with a UV-K5 tuner.** Not a plain UV-5R (it holds
its power on its own front panel), and — less obviously — **not the `uvk5` dock backend either**.
Over the dock, power is a raw PA-bias register write whose per-band calibration lives in flash the
host cannot read, which is a different mechanism with its own open questions (ADR 0128/0134). Read
`GET /capabilities`; do not infer this one from the radio model.

Three steps because three is what the radio's dock command accepts. **What a level is in watts is not
answered anywhere in this API**, and deliberately so: the firmware computes it per band from
calibration in its own flash that the host cannot read. That is what makes this the *calibrated* path
— and what makes any number here a made-up one.

The level is the **station's**, not a channel's: it persists across tuning to another channel. A
`[[presets]]` entry may carry `power` and moves it when applied; an entry that says nothing leaves it
alone (see `docs/configuration.md`).

**`POST /tuning/persist`** — body `{"on": <bool>}`. Chooses whether the channels the server tunes are
also **stored on the radio**, or held only in its RAM (ADR 0145). Returns
`{"persist": <bool>, "status": {...}}` and pushes a `status` event.

| | `on: false` — instant (default) | `on: true` — stored |
|---|---|---|
| channel change | one `0x0873` frame, milliseconds | plus an EEPROM write |
| transmit after a change | immediately | after ~6.5 s, reported as `tx_ready_in` |
| radio switched off | forgotten — it boots on the last **stored** channel | it boots on this one |
| flash wear | none | one write per changed channel |

Instant is safe to transmit in: the server re-sends the channel with one dock frame before the PTT
line goes high, so a radio that was switched off cannot go on the air on a stale frequency. What it
*will* do is **listen** on the stale one until a channel is tapped.

A live switch rather than a setting because the trade changes with what the operator is doing, and
`POST /settings` returns `apply: "restart"`. `baofeng.uvk5_tune_persist` sets the value at startup;
flipping it here does not write config back. Turning it **on** stores the channel the radio is
already on, so the switch means what it says.

- **`501`** — the backend has no such choice (everything but a UV-K5 on the `hybrid` tuner: `setvfo`
  never stores, `eeprom` always does). Read `tune_persist` in `GET /status` to know before asking.
- **`409`** — the radio is transmitting. Storing arms the serial lockout and the firmware cuts an
  over in progress when it does, so this switch must never be able to end a transmission.

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

When auth is off, returns `{"enforced": false}`. When a **fixed** login code is in use
(`auth.fixed_code`, [ADR 0083](adr/0083-fixed-login-code.md)), returns `{"enforced": true, "fixed":
true}` — the fixed code is write-only and is **never** echoed; **`503`** if the fixed mode is
selected but no code has been set.

### `POST /auth/session` (ADR 0046)

Open the over-the-air session from the web UI (clicking the OTA-code chip), with the same on-air
effect as a DTMF-keyed login — welcome announcement, station ID armed, session events. No body. Like
`/services/{digit}`, the LAN token is the credential: **no over-RF auth check and no TOTP burn**
(consuming a code here would lock an RF caller out of that window). **`503`** when no controller is
configured.

Returns `{"opened", "session_open", "announced", "announce_error"}`. Read `opened` and `announced`
**as a pair** — neither is unambiguous alone (ADR 0152):

| `opened` | `announced` | meaning |
| --- | --- | --- |
| `true` | `true` | session opened, the login announcement went out |
| `true` | `false` | session opened, the announcement was **refused** — see `announce_error` |
| `true` | `null` | session opened, no announcement configured |
| `false` | `null` | already open, nothing attempted |

**A refused announcement is `200`, not `503`** — the login succeeded, and the session really is
open (`GET /status` will agree). Only the side effect failed: the radio would not key, typically
because it is demodulating AM (ADR 0150). `announce_error` carries the backend's own sentence, and
the same failure is published on `/events` as a `session` event with phase `tx_failed` and written
to the ledger.

### `GET /link/status` and `POST /link` (ADR 0041/0042)

The Mumble/Murmur link (bridge RF audio to a Mumble channel). Present when `[[mumble.servers]]`
entries are configured. **One link is active at a time** — connecting an entry switches away from
the current one.

- **`GET /link/status`** → `{"link": {"active": slug|null, "entries": [...]}}` — every configured
  entry (`name`, `slug`, `host`, `port`, `channel`, `dtmf`, `tx_to_rf`, `autoconnect`) plus
  live state (`running`, and `connected`/`peers` on the active one). `name` is free text
  (ADR 0052); `slug` is derived from it (lowercased, punctuation/spaces collapsed to `_`) and is
  the stable key `active` refers to. The join password is never on this surface. The active entry
  also carries
  `users` — the channel roster, `[{"name": <str>, "talking": <bool>}, ...]` sorted by name,
  excluding this station; `null` on inactive entries or when the roster is unknown. `talking` is
  best-effort (true for a short window after the user's last voice frame), so poll for a live
  indicator. The same block also appears under `link` in `GET /status`. `{"link": null}` when no
  entries are configured. The station's Mumble nick is not per-entry: it is always
  `<callsign> (radio-server)` (from `station.callsign`).

  The active entry's `tx` block carries the bridge frame counters. Two of them say the relay could
  not put audio on the air, and they are deliberately **separate** (ADR 0153):

  - **`dropped_key_refused`** — the radio refused the key-up. It is demodulating AM and will not
    key its own PTT path (ADR 0150). A *standing* condition: it recurs on every frame until the
    operator changes the demodulator, so it climbs fast and is expected to. **Fix it at the radio.**
  - **`relay_errors`** — an unexpected fault on the key path: a dead audio device, an unplugged
    cable. A *fault*, not a condition — rare, and **any** nonzero value wants investigating. Each
    one is logged with a full traceback.

  They are two counters rather than one precisely so a refusal recurring at frame rate cannot bury
  a single I/O error. Neither kills the relay loop any more.
- **`POST /link`** — body `{"entry": "Club Net", "on": true}` to connect that entry (switch
  semantics), `{"on": false}` to disconnect. `entry` accepts the display name or the slug (either
  slugifies to the same key, ADR 0052) and may be omitted on connect only when exactly one entry is
  configured (`422` otherwise); an unknown name is a `404`. When no entries are configured,
  returns **`503`** — a loud failure, not a silent no-op. Returns `{"link": {...}}`.
- Every transition (browser, DTMF combo, autoconnect) is pushed on `/events` as a
  `{"type": "link", "data": {entry, state, active, entries}}` frame; a connect that fails
  (`state: "error"`) carries the reason in `detail`.
- A connect that fails synchronously — e.g. the `mumble` extra / system libopus is not installed —
  returns **`503`** with the actionable reason (including the install command) in `detail`,
  never a bare 500.

Settings-side, the entry list is edited via **`GET`/`PUT /settings/mumble-servers`** (whole-list
replace, validated atomically, restart-applied). Editor rows carry `slug` (computed, ignored on
input) and the plaintext `password` field (ADR 0052 — for *public* gate codes like the demo
server's; the editor round-trips it), plus `password_set` — whether a secrets-channel password
exists for that entry. A *private* server's password goes via the write-only
**`POST /settings/mumble-servers/{name}/password`** — the path param accepts the display name or
the slug — which lands it on the secrets channel as `mumble_password_<slug>` (never in
`radio.toml`, never read back); a secrets-channel password overrides the plaintext field.

Bridged transmissions onto RF are auto-identified (Part 97): the same streaming station-ID that
covers the `/audio/tx` talker prepends the callsign when due. Set an entry's `tx_to_rf = false` to
run it receive-only (RF → Mumble monitor, never keys the transmitter).

### `POST /server/restart` (ADR 0047)

Restart the whole server process from the settings screen — settings are restart-to-apply, so this
closes the loop after a save. No body. Hands `server.restart_command` to the deployment's supervisor
(systemd-user by default) after a brief delay so this response reaches the browser first; returns
`{"restarting": true}`. **`503`** when `server.restart_command` is unset (bare bench runs) — the web
UI hides the button in that case, keyed off `restart_available` in `GET /settings`.

### `GET /radio/backends` and `POST /radio/select` (ADR 0076)

Switch which configured backend is live, without editing a file or restarting.

`GET /radio/backends` returns every backend the config can build:

```json
{
  "active": "baofeng",
  "active_capabilities": ["ptt", "receive", "status", "transmit"],
  "backends": [{ "name": "baofeng", "active": true, "settings": { } }]
}
```

`POST /radio/select` with `{"backend": "kv4p"}` tears the current one down, builds the new one, and
persists `server.backend` to `radio.toml` so the choice survives a restart. On success it publishes a
`capabilities` event **and** a `status` event. **`409`** when the named backend has no configuration
block, **`400`** when the resulting settings are invalid, **`503`** when the switch failed and the
previous backend was rolled back.

> **⚠ One transition crashes the process.** `baofeng` → `uvk5` segfaults (SIGSEGV) during teardown.
> It is reachable from the web UI, it is recorded in
> [ADR 0140](adr/0140-the-first-key-is-always-lost.md)/0141/0142, and it is **not fixed**. Under a
> supervisor the server restarts, but the in-flight request never returns. Prefer editing
> `server.backend` and restarting for that particular change.

### Diagnostics

Two routes that read hardware truth rather than server bookkeeping. Both exist because server state
has been wrong about the radio before (ADR 0093: `POST /ptt {"on": false}` returned `200` and
`status.transmitting` read `false` while the carrier stayed up).

#### `GET /diagnostics/ptt-line`

```json
{ "backend": "baofeng", "asserted": true, "readable": true, "transmitting": true }
```

`asserted` reads the **kernel's** view of the serial control line, so it is ground truth for whether
the radio is actually keyed. It is deliberately **tri-valued**: `true`, `false`, or `null` when the
line cannot be read at all — `null` is "no answer", never "not keyed".

#### `POST /diagnostics/reboot-radio`

No body. Power-cycles or resets the radio where the backend can. Returns
`{"rebooted": true, "status": {...}}` and publishes a `status` event. **`409`** while transmitting.
**`501`** on a backend with no reboot path — note this one's `detail` is a **plain string**, not the
`{error, capability}` object the CAT surface returns.

### D-STAR (ADR 0086–0109)

> **Read [Setting up D-STAR and the DVAPs](dstar-setup.md) before using these.** Linking a reflector
> arms a path that keys your transmitter from the internet, and the crossband's bench re-proof has
> never passed. These routes all **`503`** unless `dstar.callsign` is set, which is the intended
> default.

#### `GET /dstar/status`

`{"dstar": null}` when unconfigured. Otherwise a block carrying `configured`, `active` (the linked
reflector, or `null`), `mode` (`idle`/`rx`/`tx`), a `gateway` sub-block, a `tx` counter set, and the
last 30 `activity` records.

**`active` is what was last *sent*, not what the gateway confirmed** — there is no read-back on this
path, so a dropped command or a gateway-side timeout makes it diverge from reality. The DVAP routes
below *are* confirmed; these are not.

Two counters in the `tx` set say the reflector→RF relay could not put audio on the air, and they are
deliberately **separate** (ADR 0153): **`rx_key_refusals`** is the radio refusing the key-up because
it is demodulating AM (ADR 0150) — a *standing* condition that recurs until the operator changes the
demodulator, so it climbs fast and is expected to; **`rx_relay_errors`** is an unexpected fault on
the key path (dead audio device, unplugged cable), where **any** nonzero value wants investigating
and each occurrence is logged with a full traceback. Sharing one counter would let a refusal
recurring at frame rate bury a single I/O error. Neither kills the drain loop any more — before
ADR 0153 either one took the whole crossband down over one frame.

#### `POST /dstar/link` and `POST /dstar/unlink`

`link` takes `{"reflector": "REF001 C"}` — the module letter is required. `unlink` takes no body.
Both return the fresh `dstar` block and publish a `dstar` event. **`422`** on a reflector name that
will not parse, **`409`** mid-over, **`503`** when unconfigured *or when the DV Dongle is held by
another process* (it is opened exclusively, ADR 0089).

### DVAP (ADR 0095/0096/0109)

Link and unlink the separate `dstarrepeater` endpoints that drive DV Access Point dongles, over the
ircDDBGateway remote-control interface. radio-server carries no audio and no PTT for these.

#### `GET /dvap/status`

`{"dvap": null}` when no `[[dvap.modules]]` are configured. Otherwise `{configured, remote, modules}`,
one entry per module with `module`, `label`, `frequency_hz`, `reachable`, `linked` and `reflector`.
**Unlike `/status`, this route refreshes from the gateway** on every call. An unreachable gateway
returns the cached block rather than an error.

#### `POST /dvap/link` and `POST /dvap/unlink`

`{"module": "B", "reflector": "REF001 C"}` and `{"module": "B"}`. **`404`** on an unknown module,
**`422`** on a bad reflector, **`503`** when unconfigured or the gateway is unreachable. Both publish a
`dvap` event carrying the **confirmed** post-refresh state — this link state is read back from the
gateway, so it is trustworthy in a way the module-A D-STAR link is not.

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

(FastAPI wraps `HTTPException.detail`, hence the outer `detail` key.) The `capability` value is one
of `set_frequency`, `set_channel`, `set_split`, `set_tone`, `set_mode`, `set_modulation`,
`set_power`, `scan`. Reachable from ten handlers: `/frequency`, `/split`, `/channel`, `/tone`,
`/mode`, `/modulation`, `/power`, `/scan`, `/scan/stop` and `/presets/apply`.

`clear_broadcast_fm` is the ninth member of `CAT_CAPS` and is deliberately **not** in that list: it
has no route, so it never appears in a 501 body. It is advertised on `GET /capabilities` to say what
a radio has proved it can do, not to gate a request (ADR 0157).

**Two routes return a 501 whose `detail` is a plain string, not this object**:
`POST /tuning/persist` and `POST /diagnostics/reboot-radio`. A client that reads
`detail.capability` gets `undefined` on those — check the type before indexing.

### Settings & secrets (ADR 0026)

A thin, schema-driven surface over the `radio.toml` config (ADR 0025) — what the settings UI reads
and writes. Changes are **restart-to-apply**: writes persist to file but do not hot-reload the
running server, so every write response carries `"restart_required"` / `"restart_required": true`.

**`GET /settings`** — the schema with current values. Returns
`{"settings": [...], "secrets": {...}, "apply": "restart"}`. Each settings entry is
`{key, group, type, default, value, required, description, advanced}` (plus `choices` for
`type: "enum"`; `advanced` is the basic/advanced tier the settings screen collapses on);
`type` is one of `string`, `integer`, `number`, `boolean`, `enum`. A required setting that is unset
serializes with `value: null`. The `secrets` block reports **presence only** —
`{"api_token": {"set": …}, "totp_secret": {"set": …}, "fixed_code": {"set": …}}`. **A secret value
is never returned** (secrets are not part of the settings schema). The response also carries
`restart_available`, which the UI keys the restart button off.

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

**`POST /settings/secrets/fixed-code`** ([ADR 0083](adr/0083-fixed-login-code.md)) — write-only. Body
`{"code": "<6 digits>"}`; sets the optional **fixed** over-the-air login code on the secrets channel.
**`400`** unless the code is exactly 6 digits. Returns `{"set": true, "restart_required": true}`; the
code is never read back (GET reports presence only).

There are also three Mumble-server routes on this router — `GET`/`PUT /settings/mumble-servers` and
`POST /settings/mumble-servers/{name}/password` — described under
[the link section](#get-linkstatus-and-post-link-adr-00410042). `PUT` returns
`{servers, restart_required: true, apply: "restart"}` and **`400`** on a validation failure.

All of them are token-gated like the rest of the API (`401` without a valid bearer token).

### REST status codes summary

| Code | When |
| --- | --- |
| `200` | Success. |
| `400` | `PATCH /settings` with an invalid value, unknown key, a secret key, or an empty `values` map (body names it); `PUT /settings/mumble-servers` and `POST /settings/mumble-servers/{name}/password` on a validation failure; `POST /radio/select` when the resulting settings are invalid. |
| `401` | Missing/invalid bearer token (`WWW-Authenticate: Bearer`). |
| `404` | `POST /link` or `POST /settings/mumble-servers/{name}/password` with an unknown entry (name or slug); `POST /presets/apply` with an unknown preset name; `POST /dvap/link` and `POST /dvap/unlink` with an unknown module. |
| `409` | `POST /scan` while a scan is already running (one scan at a time); `POST /presets/apply`, `POST /modulation`, `POST /tuning/persist` and `POST /diagnostics/reboot-radio` while transmitting (refused mid-TX); `POST /dstar/link` and `POST /dstar/unlink` mid-over; `POST /radio/select` on a backend with no configuration block. |
| `422` | `/scan` with a malformed addressing plan; `POST /link` connect with `entry` omitted when more than one entry is configured; `POST /presets/apply` with a frequency out of the active radio's band; `POST /split` with a transmit frequency out of band, off the tuning raster, further than a repeater offset, or crossband; `POST /frequency` and `POST /tone` on a backend `ValueError`; `POST /power` on a level that is not `low`/`mid`/`high`; `POST /modulation` on anything but `FM`/`AM`; `POST /dstar/link` and `POST /dvap/link` on a reflector name that will not parse. |
| `501` | CAT endpoint on a backend lacking that capability (body names it) — except `POST /tuning/persist` and `POST /diagnostics/reboot-radio`, whose `detail` is a plain string. |
| `503` | No controller configured (`POST /controller`, `/services/{digit}`, `/auth/session`); no Mumble link configured or the `mumble` extra missing (`POST /link`); `server.restart_command` unset (`POST /server/restart`); every `/dstar/*` and `/dvap/*` route when that feature is unconfigured, and `/dstar/link` when the DV Dongle is held by another process; `POST /radio/select` when the switch failed and rolled back; `POST /modulation` when the radio does not confirm it (switched off, or pre-F7 firmware); `POST /ptt` and `POST /transmit` when the radio is on AM and refuses its own PTT path. |

**A `503` can also come from *any* route.** A `RadioUnavailable` raised by the backend — a serial
port that vanished, a board that stopped answering — is caught by an app-wide handler and returned as
`503 {"detail": "<the hardware message>"}`. It is not confined to the rows above, because hardware can
fail under any call.

## WebSocket streams

Seven sockets, all authenticated the same way (`?token=`, bad token → close `1008` pre-accept).
**A reverse proxy must pass the upgrade on all of them** — an allow-list of the first three is how
the browser's Mumble and D-STAR audio ends up silently broken (see [deployment.md](deployment.md)).

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
| `rx` | `{"active": <bool>}` | squelch opens/closes (a signal-aware gate; inert under `squelch = "off"`) |
| `arbiter` | `{"mode": "idle"｜"receiving"｜"transmitting"}` | duplex arbiter mode transitions |
| `session` | `{"phase", ...}` | controller session lifecycle (open/close, forced ID); `tx_failed` carries `{"what", "reason"}` when a station-keying call **raised** — the radio refused its own PTT path, the audio device died (ADR 0151) |
| `auth` | `{"result": "accepted"｜"rejected"}` | an over-RF auth attempt — **the result only, never the code** |
| `command` | `{"service": <name>}` | a dispatched voice-service command |
| `link` | `{entry, state, active, entries}` | Mumble link state change (browser or autoconnect); a failed connect carries `detail` |
| `capabilities` | `{"capabilities": [...]}` | the live backend changed (`POST /radio/select`) — re-read it rather than assuming the set is fixed for the session |
| `dstar` | `{reflector, state, ...}` plus the `dstar` block | a D-STAR reflector was linked or unlinked. **The only push channel for that state** — `status` frames carry `RadioStatus` fields only |
| `activity` | `{mycall, ur, dir, reflector}` | a station was heard on, or sent to, the linked reflector |
| `dvap` | the confirmed `dvap` block | a DVAP module was linked or unlinked; published *after* the gateway read-back, so it is confirmed state |
| `alarm` | `{"kind": "tx_timeout", "tot": <float>}` | the transmitter time-out **force-unkeyed a stuck key**. Fired from a timer thread, so it arrives even when the keying path is wedged — this is the event to alert on |

A `link` event raised by a DTMF combo rather than the browser carries an extra `"via": "dtmf"` and
**omits** the `{active, entries}` block.

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

### `/audio/mumble/rx` and `/audio/mumble/tx` (ADR 0050)

The browser as a **Mumble client**: when a link is active (ADR 0041/0042), these stream the Mumble
channel instead of RF. **Neither keys the radio.** Present only when `[[mumble.servers]]` is
configured — otherwise both close **`1008`** (like a bad token).

- **`/audio/mumble/rx`** — the Mumble twin of `/audio/rx`: same `?token=` auth and JSON ready header,
  then binary canonical PCM of the **Mumble channel** (the received peer audio the bridge fans out).
  It is fed by the Mumble receive path, not the RF pump, so it takes no RX demand; with no link up the
  stream is simply idle until a bridge connects.
- **`/audio/mumble/tx`** — the Mumble twin of `/audio/tx`, but it **keys no radio**: the operator's mic
  frames go to the live bridge's single Mumble sender, which arms an operator-talk yield so the
  RF→Mumble relay steps aside (one voice on the shared channel user). Same auth, single-talker guard
  (**`1013`** busy — a **separate** slot from the RF `/audio/tx`, so the two never block each other),
  and canonical format handshake (**`1003`**) as `/audio/tx`. If no link is active when a frame
  arrives, the server sends `{"status": "no_link"}` and closes. No `TxSession`, no station ID.

### `/audio/dstar/rx` and `/audio/dstar/tx` (ADR 0088)

The same shape again, for a linked **D-STAR reflector**. Present only when `dstar.callsign` is set —
otherwise both close **`1008`**, like a bad token.

- **`/audio/dstar/rx`** — binary canonical PCM of the decoded reflector audio, published **before**
  the content gate, so a garbled decode is still audible and therefore diagnosable.
- **`/audio/dstar/tx`** — the operator's mic, AMBE-encoded and sent to the reflector. **This path
  never keys RF.** It holds a **third** talker slot, distinct from both the RF and Mumble ones
  (**`1013`** busy), takes the same canonical format handshake (**`1003`**), and sends
  `{"status": "no_link"}` then stops if no bridge is up.

> The *other* direction — reflector audio onto RF — is not a WebSocket at all. It is the crossband,
> it keys the real transmitter, and it is **disabled**. See [dstar-setup.md](dstar-setup.md).

### WebSocket close codes

| Code | Socket(s) | Meaning |
| --- | --- | --- |
| `1008` | all seven | invalid/missing `?token=` (closed pre-accept) — **and**, on the four Mumble/D-STAR sockets, that the feature is not configured |
| `1013` | `/audio/tx`, `/audio/mumble/tx`, `/audio/dstar/tx` | that talker slot is busy (after an accept + `{"status":"busy"}` message). The three slots are independent |
| `1003` | `/audio/tx`, `/audio/mumble/tx`, `/audio/dstar/tx` | unsupported/malformed PCM format (header or mid-stream frame) |

`1008` is deliberately overloaded on the feature sockets: a client cannot tell "bad token" from
"D-STAR isn't configured" from the close code alone. Check `GET /capabilities` and `GET /dstar/status`
first rather than inferring an auth failure.

## See also

- [operating.md](operating.md) — the two auth planes and Part-97 behavior in depth.
- [architecture.md](architecture.md) — where the API sits in the stack.
- ADRs: [0011 API layer](adr/0011-api-layer.md),
  [0014 RX streaming](adr/0014-rx-audio-streaming.md),
  [0016 TX ingest](adr/0016-tx-audio-ingest.md),
  [0023 RX playback](adr/0023-rx-playback.md),
  [0024 TX mic capture](adr/0024-tx-mic-capture.md).
