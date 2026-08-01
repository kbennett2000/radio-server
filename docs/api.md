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

**The token is redacted from the server's logs, and only from those** (ADR 0165). uvicorn prints the
full request path on every accept *and* on the 403 it writes for a rejected socket, so until that ADR
the token — right or wrong — went into journald once per connect. It now reads
`?token=<redacted>` there. Nothing else changed: the value is still in the URL, so it is still in
browser history, still in DevTools' Network panel, and still readable by any script on the page via
`ws.url`. Treat a `?token=` URL as a credential wherever it is *not* a server log.

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

A full-CAT backend additionally lists `clear_broadcast_fm`, `scan`, `set_broadcast_fm`,
`set_channel`, `set_frequency`, `set_mode`, `set_modulation`, `set_power`, `set_split`, `set_tone` —
the ten members of `CAT_CAPS`. Real backends advertise a subset: the `uvk5` and `kv4p` backends have `scan`
but not `set_power`, the `baofeng` backend with a UV-K5 tuner has `set_power` but not `scan`, and
`set_modulation` needs both F7 firmware **and** a `setvfo`/`hybrid` tuner — a `baofeng` on the
stock-firmware `eeprom` tuner has every other tuning capability and not that one. Use this to decide
which controls to enable, and never assume the split from the backend name.

`clear_broadcast_fm` and `set_broadcast_fm` are the two capabilities that are **earned rather than
configured**. Every other member is a claim derived from configuration; these appear only after a
radio has actually answered `0x087A`, because the firmware that implements it is a fork branch —
advertising them from a config key would have every station claiming a firmware generation nobody is
running. Their absence means "this radio has not shown it can do this", not "this server would
refuse". Use `clear_broadcast_fm` to know whether `broadcast_fm` in `GET /status` can ever be
non-null (ADR 0157).

`clear_broadcast_fm` is also the gate on **cost**, which is what it is really for since ADR 0161:
the server clears broadcast FM at startup and again before every key-up, and a radio that has not
earned this pays a set-membership test rather than a 3.0 s timeout before each over. Since ADR 0164
both also gate the two directions of `POST /broadcast-fm`, and the 501 body names which one is
missing so a UI can grey exactly the right control.

**Reading the pair.** They are earned by the same reply with one exception, and that exception is
reachable:

| you see | conclude |
|---|---|
| both | the radio has a second receiver and this server can switch it either way |
| neither | nothing has answered `0x0879` — pre-F8 firmware, a radio that was off at startup, or a backend with no dock tuner |
| **`clear_broadcast_fm` only** | the radio answered and said `ERR_NO_HAL`: its image was built without `ENABLE_FMRADIO`, so there is **no broadcast receiver at all**. Nothing can deafen this station and there is nothing to switch on |
| `set_broadcast_fm` only | cannot happen. If you see it, this server has a bug |

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
  "transport": null,
  "controller": null,
  "scan": { "running": false, "frequency": null },
  "link": null,
  "dstar": null,
  "dvap": null,
  "slots": {
    "tx": {
      "held": false,
      "holder": null,
      "since": null,
      "held_s": null,
      "stale_after_s": 180.0,
      "refused": {}
    },
    "mumble": null,
    "dstar": null
  },
  "rx_demand": { "requested": 0, "reader_running": false }
}
```

`slots` is talk-slot occupancy (ADR 0170), one entry per single-talker guard: `tx` is the RF
transmitter, `mumble` and `dstar` the link talk paths, each `null` when that subsystem is not
configured. **It exists because the two fields above that look like they answer the question do
not:** `transmitting` is PTT state and `busy` is squelch state, and both read `false` while a slot is
stranded — so before this block a leaked slot and an idle station were the same reading.

`holder` is which claimant holds it (`browser`, `mumble-relay`, `dstar-relay` — the RF slot is shared
by all three). `held_s` is the age of the claim, measured on a **monotonic** clock; `since` is the
wall-clock time derived from it, so an NTP step or a suspend moves the displayed time of day and
never the age. Both are `null` when free, so a stale timestamp never sits beside `held: false`
looking like a measurement. `refused` counts refused claims **per claimant**, deliberately not as one
total: the Mumble relay refuses at frame rate while a browser talker holds the slot, and a single
number would let that bury the one browser refusal an operator is trying to explain (ADR 0153's rule,
one layer down). It is a ledger and is **not** cleared on release.

`stale_after_s` is the transmitter time-out — how long one over may legally last — handed out so a
client can say "this has been held longer than a transmission" without hard-coding the number. **The
server publishes no `stale` verdict of its own:** it cannot distinguish a long legitimate over from a
stuck slot, only the timeouts can, and they already reap.

`rx_demand` is **not** a fourth entry in `slots`, and its field is `requested` rather than
`listeners` or `active`, for a measured reason. `/audio/rx` parks on an unbounded queue read and only
ever *sends*, so it learns its client is gone from the next published frame — and with a VAD squelch
(`squelch = "audio"`) a quiet channel publishes nothing. Measured against real uvicorn: a cleanly
closed listener was still counted 3 s later on a quiet channel, and an RST'd one was still counted
after 50 s, past uvicorn's 20 s keepalive, and stayed counted through a signal that woke every other
reader. **So this counts requests for received audio, not proven-live listeners.** The talk slots
above are the opposite — their receives are bounded by `tx.idle_timeout`, so a dropped talker is
freed in 43 ms (clean close) to 1.85 s (RST). The two kinds of number are kept in separate blocks so
one does not lend the other its trustworthiness.

`transport` is the serial link's liveness (ADR 0166) — `{"alive": <bool>, "error": <str or null>,
"port": <str or null>}`, or `null` on a backend with no serial link to report on. **It is the only
field here that is not a cached read.** Every other field is served from state the backend last
wrote, which is exactly why a dead reader was invisible: the answers keep arriving, they just stop
meaning anything. `alive: false` means the radio has stopped answering and everything else in this
object is the last thing the server knew.

`null` is "not applicable", never "fine" — the same tri-state rule `broadcast_fm` keeps.

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
  nothing — one of **two** causes that refuse a key-up; see `broadcast_fm` below, and expect the two
  to name different faults and different remedies. On a tuner that can set a modulation, `null`
  after startup now means the server's boot
  assertion did not land — which is itself worth acting on, because until it does the server cannot
  refuse a key-up the radio would swallow (ADR 0155).
- **`broadcast_fm`** — the radio's **second receiver**, or `null`. A UV-K5 carries a BK1080
  commercial-FM chip (64–108 MHz) beside the BK4819 that everything else drives; when it runs it
  holds the speaker line the AIOC listens on, so **the station hears nothing on its own channel**.
  Whether it also transmits into that channel depends on the firmware — see `blocks_tx`. A block
  when known, `{"on": false, "hz": 103200000, "band": 0, "blocks_tx": false, "rescues": 0}`:
    - **`on`** — is the second receiver running? `true` means the station cannot hear itself.
    - **`hz`** — the BK1080's tuning, or `null` where the radio blanked it. **Only meaningful
      together with `on`**: the receiver remembers where it was, so a real frequency is reported
      even when it is switched off. That is the frequency it *would resume on*, not what anything
      is listening to; read alone it looks like a station happily monitoring 103.2 MHz.
    - **`band`** — which BK1080 band the receiver is on: `0` 87.5–108, `1` 76–108, `2` 76–90,
      `3` 64–76 MHz, or `null` where nothing reported it (`0` is a real reading, so the blank is
      `null` and never `0`). Carried since ADR 0164 because `POST /broadcast-fm` takes the band byte
      explicitly and 76.5 MHz is one station under band 1 and a different one under band 2 — `hz`
      alone is half an answer.
    - **`blocks_tx`** — is the **radio itself** refusing to transmit because of this? `0x087A` flags
      bit 1, the F9 firmware interlock (ADR 0159), or `null` where nothing has reported it (any
      backend with no such firmware to ask, including `mock`). `false` on a deaf station is **not** a
      missing measurement: it is an older or non-interlock image saying, correctly, that it *will*
      key while deaf — the more dangerous state, and the reason the bit reports blocking rather than
      readiness. Reported here rather than folded into `tx_ok`, which is the BK4819 demodulator and
      belongs to `POST /modulation`; the two causes stay separate all the way to the operator.
    - **`rescues`** — how many key-ups this server has **rescued**: found the second receiver running
      immediately before switching it off, so the over went out on a station that could hear its own
      channel (ADR 0162). A climbing count means somebody keeps leaving broadcast FM on at the radio,
      and every over in between went out fine. A plain integer rather than a third tri-state, because
      *how many* has no unknown — a backend that cannot ask has rescued nobody.

  The whole block is `null` when the server does not know — no such receiver, firmware older than
  F8 (which drops the command in silence), a radio that was switched off at startup, or a re-read
  that was attempted and got no answer. **`null` and `{"on": false}` are different answers** and
  must not be rendered the same: the first is "we do not know", the second is "we asked and the
  station can hear". Collapsing them is how a deaf station gets trusted.

  **`on: true` refuses a key-up** with a **503**, on every path — `POST /ptt`, `POST /transmit`,
  the browser Talk button, both bridges, and the controller's station ID and announcements (ADR
  0158). `null` does **not** refuse, and that asymmetry is deliberate and load-bearing: an
  unmeasured field must never lock a transmitter, which is the same rule `tx_ok` follows.

  **This is a reading taken before the last key-up, not a live one and no longer a boot-time record**
  (ADR 0161). On a backend whose tuner has earned `clear_broadcast_fm`, the server sends one
  `0x0879` immediately before every key-up and records what came back. Three consequences, and the
  first is the one that decides how to read this field:

  > **Correction (ADR 0162).** The paragraph below said the wire offers no read-only action. It
  > does: `0x0879` action=TUNE at an out-of-band frequency is refused *before* the firmware touches
  > anything, and which refusal comes back (`ERR_OFF` = hearing, `ERR_BAND` = deaf) is a complete,
  > non-mutating answer. The server now sends that probe immediately before the clear, which is where
  > `rescues` comes from. The clear still follows it, so the rest of this section stands.

  - **The frame that CHANGES this state is the frame that switches the receiver off.** A key-up on a
    station an operator left in broadcast
    FM therefore **clears it and proceeds** rather than being refused — and because the reply
    describes the state *after* the clear, the server cannot report that it just did so. It is
    silent, deliberately, rather than guessing.
  - **`on: true` now means the radio was asked to stop and did not.** That is a malfunction, not an
    operating state, and it is the only case where deafness refuses a key-up at all.
  - **Between key-ups this block is stale, and the server does not poll.** A `GET /status` that
    reached into the radio and switched its receiver off would not be a status read; `0x0879` is
    also refused (`ERR_TX`) whenever the radio is transmitting **or monitoring**, so a polled block
    would go unknown exactly when the station is in use. An operator pressing the radio's own FM key
    is therefore still invisible *here* until the next key-up — which is when it matters, and where
    it is now caught.
  - **A key-up while the radio is busy does not refuse and does not lose the reading.** That same
    `ERR_TX` arrives routinely before a key-up — from this station's **own** key, via `dock.c`'s
    `ctx->tx_on`. (An earlier version of this line said "because an open squelch is most of an active
    QSO". ADR 0163 corrected that from firmware source: an ordinary open squelch is
    `FUNCTION_INCOMING`/`FUNCTION_RECEIVE` and trips nothing, and `FUNCTION_MONITOR` is the
    *forced*-open monitor key alone.) It
    is a courtesy refusal rather than a fault: the radio answered and named a condition of its *other*
    receiver, so the block keeps what it last measured and the key-up proceeds on it — including
    refusing, if what it last measured was `on: true`. There is therefore a window, while the station
    is transmitting or monitoring, in which this field cannot be refreshed at all.

  Since ADR 0164 there **is** a route in both directions — `POST /broadcast-fm`, below. Before it,
  the only host-side way out of broadcast FM was to press Talk and let the key-up's rescue do it as a
  side effect, which on an unattended LAN station meant the remedy was a hand on a keypad in another
  room (ADR 0158 R4 / 0160 finding 3 / 0161 finding 2). The refusal that used to require a **server
  restart** to clear no longer does either: turning the receiver off — from the route, or by pressing
  EXIT on the radio — is the whole remedy, because the next key-up re-reads.

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

**The demodulator is not the only thing that refuses a key-up.** Two other causes reach the same
503, each with its own message and its own remedy — see `broadcast_fm` under `GET /status`. A station
whose second receiver was asked to stop and did not (`broadcast_fm.on: true`) is refused; so is one
whose receiver could not be asked at all, because a server that does not know whether the station can
hear itself will not key it. Setting `FM` here clears neither, and an operator who tries it gets a
station that now *does* transmit and still cannot hear. Read the 503 body — the three messages share
no clause, no identifier and no remedy, precisely so "cannot transmit" is never half a diagnosis.

Every one of them now reaches the **browser** too. The `/audio/tx` socket sends
`{"status": "refused", "reason": "<the 503 body>"}` and then closes, because a browser cannot read a
close code; before ADR 0161 a refused key-up simply killed the socket and the Talk card reported
"Transmit connection dropped."

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

### `POST /broadcast-fm`

Drive the radio's **second receiver** — the BK1080 commercial-FM chip described under
`GET /status`'s `broadcast_fm` block. Body:

```json
{"action": "off" | "on" | "tune", "hz": 104300000, "band": 0}
```

Returns `{"broadcast_fm": {...}, "status": {...}}` and publishes a `status` event. The
`broadcast_fm` block is the **read-back** — what the radio is actually doing, out of the firmware's
own state after it acted, never an echo of the request.

`hz` and `band` are **ignored on `off`**: `Dock_SetFm` branches to `Dock_FmOff()` before it reads
either field, and refusing on them would be this server inventing a rule the firmware does not have.
`band` defaults to `0` (87.5–108 MHz) and is still sent explicitly, because the firmware's own
`FM_Band` is a two-bit field that would clamp a bad value in silence.

**`tune` is not a cheaper `on`.** It moves a receiver that is already running; asked of one that is
off it is refused, so a host stepping across the band cannot switch the station deaf by accident.
This is the web UI's **Retune** button: the frequency and band controls stay on screen while the
second receiver runs, so changing station is one request and never a stop/start (ADR 0168).

**What turning it on costs**, all measured, and none of it obvious from a switch labelled "FM radio":

1. Both bridges go silent. Nothing this radio hears reaches Mumble or a D-STAR reflector while the
   second receiver is selected (ADR 0162/0163).
2. **Real overs on the station's own channel are withheld from the links too, even though the radio
   hears them.** The firmware tears the BK1080 down for the duration of a real signal without
   clearing `gFmRadioMode`, so the probe the mute runs on reads "FM is selected" throughout — ADR
   0163's M3 recovered a witness's 1000 Hz tone at power 0.995 during an over while the probe still
   said FM. The mute is deliberately coarser than the radio.
3. **Any transmission turns it back off.** The server clears broadcast FM before every key-up, so
   Talk, a voice service and the automatic station ID all take the receiver back — and on a station
   with an open controller session the periodic ID will do it without anybody asking. It is not that
   the station cannot transmit; it is that transmitting ends this.
4. **The radio's keypad is not locked, it is repurposed** (ADR 0160, photographed): digits type a
   frequency into the broadcast receiver rather than moving the station, and `M` opens a
   save-to-channel prompt. Somebody at the radio who does not know it is in broadcast FM will believe
   they are tuning the station. *(Whether confirming that prompt overwrites a stored channel was
   never measured, and is not claimed here.)*

**The web UI no longer states any of these** (ADR 0168). ADR 0164's card put 1–3 in a notice above a
two-step arm/confirm button and 4 in a red banner while the receiver ran; all three came out at the
operator's request, and the card is now an ordinary control — one click on, one click off, editable
frequency throughout. The four consequences are unchanged and are written down twice: here, and in
[troubleshooting.md](troubleshooting.md) for the operator who is looking at the symptom rather than
at the API. None of them was ever the control that enforces them — that is the relay mute (ADR 0162)
and the pre-key-up clear (ADR 0161), neither of which lives in the UI.

Status codes, and the rule behind them is **a refusal that arrives is an answer; only silence is
unavailability**:

- **`501`** — naming *which* capability is missing, `clear_broadcast_fm` for `off` and
  `set_broadcast_fm` for `on`/`tune`. They are separately earned; see the table under
  `GET /capabilities`.
- **`409`** — the radio answered and said not now. `ERR_TX` (it is keyed, or somebody is holding the
  monitor key), `ERR_OFF` (a `tune` on a receiver that is off), and this server's own refusal to
  start the receiver mid-transmission, which would take the speaker out from under an over in
  progress. All three are worth retrying; none of them is a fault.
- **`422`** — the number was wrong. A frequency off the **100 kHz raster** (refused, never rounded:
  the next step is a whole adjacent station), a `band` outside 0–3, an unknown `action`, and the
  radio's own `ERR_BAND` for a frequency outside the named band's limits. The host checks the raster
  and the band *number* before anything reaches the wire; the band's frequency *limits* live in the
  BK1080 driver and are the radio's verdict, because a second copy of a hardware table here is a
  drift hazard worth more than it would buy.
- **`503`** — no reply at all. The radio is switched off, unplugged, or running firmware older than
  F8 that drops the frame in silence.

Turning it **off** is never refused mid-transmission: it gives the station its ears back, it is what
the key-up path does on its own, and blocking the one safe action behind the unsafe one's guard is
how ADR 0161 finding 2 stayed open for four cycles.

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

  A third pair says the relay is **withholding audio on purpose** (ADR 0162), and it is a counter
  plus a tri-state because the counter alone cannot be read safely:

  - **`rx_deafened`** — RF frames not relayed because the radio's second receiver is playing
    broadcast FM, so what this station hears is a commercial broadcast and not its own channel.
    Relaying it would put a broadcast station onto a link whose far end may be somebody else's RF
    repeater. **Fix it at the radio** (press EXIT); the relay resumes on its own.
  - **`deafened`** — `true` / `false` / **`null`**. `rx_deafened: 0` beside `deafened: false` means
    *this station was measured and can hear its own channel*; beside `deafened: null` it means
    *nobody has ever asked this radio* — every backend without a dock tuner, and every radio on
    firmware with no such command. Those are different facts and the counter renders them
    identically, which is the same trap `broadcast_fm` itself is a tri-state to avoid.
  - **`deafened_reason`** — the sentence to show an operator, or `null` when nothing is withheld.
  - **`deafened_age_s`** — how many seconds ago the reading the mute is acting on was measured, or
    **`null`** when nothing has ever measured it. The same discipline one level out: `deafened: true`
    renders a two-second-old reading and a ten-minute-old one identically, and `0` would claim
    "measured just now" on a station nothing is polling.
  - **`deafened_unknown`** — probes that answered nothing (the radio was keying, the link was busy,
    the reply timed out) and were **held through** rather than acted on. A non-answer is not a state
    transition: the last definite reading stands. A steadily climbing `deafened_unknown` with a
    rising `deafened_age_s` means the dock link has stopped answering — see
    [ADR 0163](adr/0163-a-cadence-for-the-probe.md) finding 1.

  **The browser is not muted, deliberately.** `/audio/rx` subscribes to the same audio hub with no
  policy at all: listening to broadcast FM in the browser is a feature, and a browser tab does not
  retransmit. The suppression lives in each *relay's* own loop rather than in the hub, so browser
  Listen and the recorder are untouched by construction (ADR 0085).

  `GET /dstar/status` carries the same fields on its own `tx` block, named **`tx_deafened`**,
  `deafened`, `deafened_reason`, `deafened_age_s` and `deafened_unknown` — `tx_*` there because
  RF→reflector is the outbound direction from that bridge's point of view.

  **The front-panel window is covered since [ADR 0163](adr/0163-a-cadence-for-the-probe.md)**, which
  polls the radio every ~2 s **while a bridge is relaying** (no bridge, no hazard, no serial traffic).
  ADR 0162 stated the opposite bound and it is superseded: an operator switching broadcast FM on at
  the front panel is now seen within about one poll interval, which is also the **leak window** — up
  to ~2 s of broadcast programming reaches the far end before the mute arms.

  **What it means, precisely.** The underlying read answers *broadcast FM is selected*, **not** *this
  station is deaf right now*. When a real signal opens the squelch the firmware drops the second
  receiver and passes channel audio without clearing the flag (measured: a witness's 1000 Hz tone
  recovered at power 0.995 while the probe still reported broadcast FM). **So the mute also withholds
  genuine overs for as long as an operator is listening to broadcast FM.** That is deliberate: the
  hazard is a commercial station relayed onto somebody else's repeater, and it lands on third
  parties, while the cost lands on a channel the operator has already left — and it is visible here
  rather than silent.

  **`GET /status`'s `broadcast_fm` block can disagree with this, and both are right.** That block is
  what the *key-up* path measured (`clear_broadcast_fm` remains its only writer, so no poll can ever
  reach the decision to refuse a transmission), so during the front-panel window it reports
  `on: false` while `deafened` here is `true`. Read `deafened` for what the relay is doing and
  `broadcast_fm` for what the last key-up found.
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

#### `GET /healthz`

Is this station **usable**, as opposed to merely answering? (ADR 0166)

```json
{ "ok": true, "backend": "baofeng", "transport": { "alive": true, "error": null, "port": "/dev/ttyACM0" } }
```

**`503`** with `"ok": false` when the serial reader is dead. **`200`** otherwise, including when
`transport` is `null` — a backend with no serial link is not broken, it has nothing to break.

**Why this is a separate endpoint from `/status`.** `/status` deliberately still answers `200` with
a full body when the reader is dead, because it is where a broken station gets *diagnosed* and a
naive client discards a `503` body — a `/status` that 503'd would say something is wrong and then
refuse to say what. This endpoint is the machine-readable verdict for anything that only reads a
status code. That was every readiness check in this repo until ADR 0166: `acceptance.py`'s
`wait_healthy` treated `200` on `/status` as the whole signal, so a station whose dock link had been
dead for an hour reported "restarted healthy".

Use `/healthz` for "should I trust this station", `/status` for "what is wrong with it".

### Diagnostics

Three routes that read hardware truth rather than server bookkeeping. They exist because server state
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

#### `POST /diagnostics/reconnect`

No body. **One bounded reopen** of the serial link (ADR 0166), and it reports which of three things
happened, because they are three different facts:

| body / status | meaning |
|---|---|
| `{"outcome": "already_healthy", "transport": {...}}` · **200** | the reader was running; nothing was done |
| `{"outcome": "reopened", "transport": {...}}` · **200** | the port was reopened and a new reader is running |
| `{"detail": "<the reason>"}` · **503** | the reopen failed — port held by another process, device gone, permission |

A failure is a **503, never a 200 saying "failed"**: an outcome reported in a body that everything
reads as success is the fault class this repo keeps closing.

**One shot, never a loop, and never automatic.** The common cause is a USB re-enumeration, which this
fixes without a human walking to the machine. The other cause is a second process holding the tty —
and a retry loop would spend the station's life fighting it for the port, which is a louder version
of the race that caused the fault. Publishes a `status` event on success.

**`501`** on a backend with no serial transport.

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
| `409` | `POST /scan` while a scan is already running (one scan at a time); `POST /presets/apply`, `POST /modulation`, `POST /tuning/persist` and `POST /diagnostics/reboot-radio` while transmitting (refused mid-TX); `POST /broadcast-fm` `on`/`tune` while transmitting, **and** whenever the radio itself answers `ERR_TX` or `ERR_OFF` — a courtesy refusal from a radio that replied is a conflict, not an unavailability, and would otherwise be a 503 purely by inheritance (ADR 0164); `POST /dstar/link` and `POST /dstar/unlink` mid-over; `POST /radio/select` on a backend with no configuration block. |
| `422` | `/scan` with a malformed addressing plan; `POST /link` connect with `entry` omitted when more than one entry is configured; `POST /presets/apply` with a frequency out of the active radio's band; `POST /split` with a transmit frequency out of band, off the tuning raster, further than a repeater offset, or crossband; `POST /frequency` and `POST /tone` on a backend `ValueError`; `POST /power` on a level that is not `low`/`mid`/`high`; `POST /modulation` on anything but `FM`/`AM`; `POST /broadcast-fm` on a frequency off the 100 kHz raster, a band outside 0–3, an unknown action, or the radio's own `ERR_BAND`; `POST /dstar/link` and `POST /dvap/link` on a reflector name that will not parse. |
| `501` | CAT endpoint on a backend lacking that capability (body names it) — except `POST /tuning/persist` and `POST /diagnostics/reboot-radio`, whose `detail` is a plain string. |
| `503` | No controller configured (`POST /controller`, `/services/{digit}`, `/auth/session`); no Mumble link configured or the `mumble` extra missing (`POST /link`); `server.restart_command` unset (`POST /server/restart`); every `/dstar/*` and `/dvap/*` route when that feature is unconfigured, and `/dstar/link` when the DV Dongle is held by another process; `POST /radio/select` when the switch failed and rolled back; `POST /modulation` when the radio does not confirm it (switched off, or pre-F7 firmware); `POST /ptt` and `POST /transmit` when the radio is on AM and refuses its own PTT path, **and** when the radio's second receiver is running so the station cannot hear its own channel (ADR 0158). Those last two are different faults with different remedies and deliberately different messages — the AM one names the demodulator and sends you to `POST /modulation`, the broadcast-FM one names the second receiver and sends you to `POST /broadcast-fm` or the radio's EXIT key. *(That sentence used to end "plus a restart". ADR 0161 dropped the latch and ADR 0164 added the route; neither is required any more.)* Also `POST /broadcast-fm` when the radio never answers at all. |

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
| `busy` | `{"slot": "tx"\|"mumble"\|"dstar", "holder": <str or null>, "held_s": <float or null>}` | a websocket talker was **refused** a talk slot, and who held it at the time (ADR 0170). **Websocket refusals only** — the Mumble and D-STAR relays are refused per frame while a browser talker holds the RF slot, and publishing those would bury this stream; they are counted instead, as `dropped_slot_busy` / `rx_dropped_busy` and in the slot's own `refused` ledger |

A `link` event raised by a DTMF combo rather than the browser carries an extra `"via": "dtmf"` and
**omits** the `{active, entries}` block.

`busy` was reserved in `EVENT_TYPES` from ADR 0011 and went unpublished until ADR 0170; a client
written against an older server will simply never have seen one. The normal path closes on client
disconnect with no application close code.

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

   Since ADR 0170 that message carries the diagnosis rather than just the verdict —
   `{"status": "busy", "slot": "tx", "holder": "browser", "held_s": 12.4, "stale_after_s": 180.0}`
   — because this message is the client's **only** channel here and it cannot go and read `/status`
   for a socket that is about to close. `holder` and `held_s` are what let a client tell "somebody is
   talking" from "this slot has been held for four hours and nobody is on the air"; past
   `stale_after_s` the web UI changes its wording rather than just showing a larger number. A client
   that receives no `holder` (an older server) must fall back to a generic sentence rather than
   inventing one.
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
