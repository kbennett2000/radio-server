# ADR 0164 — The ON path: broadcast FM becomes a thing an operator can use

**Status:** Accepted · 2026-08-01 · closes [ADR 0158](0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md) **R4**, [ADR 0160](0160-the-bench-answers-back.md) **finding 3** and [ADR 0161](0161-the-host-asks-the-radio.md) **finding 2**; widens [ADR 0157](0157-a-station-that-cannot-hear-itself.md)'s surface without renaming its capability

## Context

The 0156→0163 arc built everything around the radio's second receiver except the ability to use it.
The server can switch broadcast FM **off** (0157), refuse to key while it is on (0158/0159), rescue
it before every over (0161), keep it off the bridges (0162) and poll for it (0163). It could not
switch it **on**, and it had no operator-facing way to switch it **off** either. Three findings say
the same thing from three sides and have been open for four cycles:

- **ADR 0158 R4** — `AiocBaofeng` advertises `CLEAR_BROADCAST_FM` with no `Radio`-level method
  behind it. *"The route cycle should fix it rather than discover it."*
- **ADR 0160 finding 3** — the documented operator remedy is "press EXIT, or power-cycle it", and it
  is the *only* remedy.
- **ADR 0161 finding 2** — *"pressing Talk is the operator's only way out of broadcast FM from the
  host side. If the pre-key-up clear misbehaves there is no second path, and the remedy is a hand on
  the radio's EXIT key — which on an unattended LAN station means nobody."*

The firmware half already existed and was already measured: `Dock_SetFm` implements OFF/ON/TUNE and
is merged to fork `main` (`d086a23`, F8); the bench radio runs F9 (`0161` B1, `flags=0x03` on the
wire); ADR 0160 measured an ON frame applying at 104.3 MHz with a 0.401 s time-to-first-byte. What
was missing was a route, a capability, and a control an operator can look at and understand.

### The correction this cycle had to carry, and the bench settled it

The brief listed four consequences of turning broadcast FM on and called stating them the design
work. Three survived contact with the code. **The fourth was wrong, and it was the one that matters
most to an operator.**

The brief said *"the transmitter is refused by the firmware (F9)"*. It is not, on any host path.
`AiocBaofeng._key_on` calls `_clear_if_deafened` **first** (`aioc_baofeng.py:816`), which probes and
then clears — so by the time PTT asserts, F9's interlock has nothing left to interlock. **B2
measured it**: `POST /ptt {"on": true}` with broadcast FM running returned **200**, `transmitting:
true`, `rescues: 0 → 1`, and the journal line *"switched the second receiver off before keying
(rescue #1). It was tuned to 104.3 MHz."*

So the honest third consequence is **"any transmission turns it back off"** — Talk, a voice service,
and the automatic station ID, which on a station with an open controller session will do it inside
the ID interval without anybody asking. That is what the UI says, and it says it because the bench
said it, not because this ADR reasoned about it.

**A second correction, about the record rather than the radio.** The brief said `M` on the front
panel "offers a channel save, so anyone at the radio can overwrite a stored channel". ADR 0160 item 6
photographed the `CH-01` / `SAVE?` **prompt**; whether confirming it overwrites a stored channel was
never measured. Guardrail 1 says a hardware fact is not asserted from memory, so the UI states what
was measured — digits type into the broadcast receiver, `M` opens a save-to-channel prompt — and
claims no overwrite. A vitest case pins the absence.

---

## Decision

### 1. Two frame classes, and neither can do the other's job

`ClearBroadcastFm` is unchanged: no action parameter, `ACTION_OFF` hardcoded, and ADR 0157's test
that `ClearBroadcastFm(1)` raises `TypeError` stays green. A new `SetBroadcastFm(action, hz, band)`
expresses **ON and TUNE only** and refuses `"off"` in `__post_init__`.

ADR 0157 made *"this server cannot turn broadcast FM on"* a property of the code rather than a rule a
reviewer keeps re-checking. That property was load-bearing **on the key-up path** — the boot assert
and the pre-key-up rescue — which runs before every over and must never be able to deafen the
station it is about to key. It is kept exactly there. The operator's route gets the mirror. The
symmetry is the safety argument, and it is enforced by the type system rather than by review.

### 2. Validation splits exactly where the firmware splits it

| checked | where | on failure |
|---|---|---|
| action ∈ {`on`, `tune`} | `SetBroadcastFm.__post_init__` | **422** |
| band ∈ 0..3 | host — `BROADCAST_FM_BAND_MAX`, a protocol constant | **422** |
| `hz % 100_000 == 0` | host — `BROADCAST_FM_RASTER_HZ`, already in `frames.py` | **422**, and no frame is sent |
| the band's own Hz limits | **the radio** — `BK1080_GetFreqLoLimit`/`HiLimit` | `ERR_BAND` → **422** |

Refuse, never round (ADR 0156): `0x0873` truncates sub-10 Hz detail because 10 Hz of a repeater
channel is nothing, and 100 kHz of the broadcast band is *a whole adjacent station*. The band's
frequency limits are deliberately **not** copied into the host: `dock.h:325-330` refuses to keep them
in `dock.c` because *"a second copy of a hardware table … is a drift hazard worth more than the
testability it would buy"*, and that argument reaches one layer further out. The host asks and
reports the radio's verdict.

Validating in the frame's constructor rather than in the route means there is no code path anywhere
in this server that can put an off-raster frequency on the wire. `MockRadio.set_broadcast_fm` builds
the same frame to validate and throws it away, so the mock refuses precisely what the wire refuses
and cannot drift from it.

### 3. A refusal that arrives is an answer. Only silence is unavailability.

| reply | HTTP | why |
|---|---|---|
| `APPLIED` | **200** + the read-back | ADR 0156's doctrine: what the radio is *actually* on |
| `ERR_TX` (8) | **409** | keyed, or somebody is holding the monitor key |
| `ERR_OFF` (9) | **409** | a `tune` on a receiver that is off — a state conflict, not a bad number |
| `ERR_BUSY` (2) | **409** | the host holds full-control |
| `ERR_BAND` (6) | **422** | outside the band that was named |
| `ERR_FIELD` (4) | **422** | unreachable — the host checked first; arriving means the host is wrong |
| `ERR_NO_HAL` (5) | **501** | no BK1080 in this image, and the capability is **retracted** |
| no reply | **503** | switched off, unplugged, or pre-F8 |

This needed a new exception, and the reason is layering rather than taste. `TuneBusy` is a
`TuneError` is a `RadioUnavailable`, and `create_app` turns any `RadioUnavailable` into a 503 — so
**503 was the answer a courtesy refusal got purely by inheritance**, for a radio that replied
promptly and named a condition it will be out of in a second. The API decides status codes and must
not import a backend's exception module to do it, which is the same argument that put
`RadioUnavailable` in `base.py` in the first place. So `base.py` gains `RadioBusy`, `TuneBusy`
subclasses both, and the API's trio is complete: `UnsupportedCapability` → 501, `RadioBusy` → 409,
`RadioUnavailable` → 503.

### 4. One route, both directions, two capabilities

`POST /broadcast-fm` with `{"action": "off" | "on" | "tune", "hz": …, "band": …}`.

- `off` → `AiocBaofeng.clear_broadcast_fm()`, a new **`Radio`-level method** — literally what ADR
  0158 R4 asked for — gated on `CLEAR_BROADCAST_FM`, and **never refused mid-transmission**. Turning
  the second receiver off during an over gives the station its ears back; it is the direction the
  key-up path already takes on its own, and blocking the one safe action behind the unsafe one's
  guard is how ADR 0161 finding 2 stayed open for four cycles.
- `on`/`tune` → `set_broadcast_fm(...)`, gated on the new `SET_BROADCAST_FM`, and refused
  mid-transmission with a 409 that never reaches the wire.

**`clear_broadcast_fm` is added to, never renamed**, and the smaller-sounding reason comes first:

1. **The name is already published.** It is in `/capabilities` on every deployed station and
   documented in `docs/api.md`; a client may already branch on it. Removing a published capability
   name to improve a word is a breaking change bought with nothing.
2. **`base.py`'s "the next cycle widens it" meant widening the *surface*, not replacing the *name*.**
   The objection it recorded was specific — a capability called `set_broadcast_fm` would have
   *"advertise[d] a switch with no on position"*. This cycle supplies the on position; it does not
   invalidate the other name.

And a third, about the record: ADR 0157's **"the first capability with no route"** is the clearest
illustration in this repo of what earned-by-reply means — a capability that states what a radio has
*proved*, with nothing to gate. Renaming it away would delete the example. `clear_broadcast_fm` also
keeps its second job as the key-up **cost** gate (ADR 0161).

**They are earned by the same reply with one exception, and the exception is reachable.**
`CLEAR_BROADCAST_FM` keeps its rule: any `0x087A`, including `ERR_NO_HAL`, because an image built
without `ENABLE_FMRADIO` is a *definitive negative* and "this station can never be deafened by a
second receiver" is exactly what the capability claims. `SET_BROADCAST_FM` is earned by any `0x087A`
**except `ERR_NO_HAL`**, which **retracts** it — the one reply on this wire that is a certainty about
the image is allowed to withdraw a grant made on softer evidence. So `docs/api.md` now tells a client
what the pair means, including that `set` without `clear` cannot happen.

A stated imprecision rather than a papered-over one: `ERR_SHORT`, `ERR_BUSY`, `ERR_FIELD` and the
`ctx->tx_on` half of `ERR_TX` are all returned by `dock.c` **without consulting the HAL**, so
strictly they prove nothing about whether a BK1080 driver is in the image. Treating them as evidence
errs toward advertising, which is the safe direction here: the cost is a 501 on a button the UI
already handles, and the alternative is hiding the control for ever on a radio that would have worked.

### 5. The relay mute arms before the wire and disarms only on proof

The ON exchange was measured at ~0.4 s (ADR 0160) and this cycle's own bench at 101 ms; ADR 0163's
cadence polls every 2.0 s. A mute armed off the *reply* would therefore relay up to ~2.4 s of a
commercial station to whatever is at the far end of a link, which may be somebody else's repeater
(97.113(b)). On the front-panel path that window is unavoidable and ADR 0163 prices it. **On this
path the ordering is entirely ours, so the leak is zero.**

```
with assume_broadcast_fm_on(cadence):     # armed BEFORE the frame goes out
    radio.set_broadcast_fm(...)           # a refusal propagates; the hold releases
    observe_broadcast_fm(cadence, on)     # a definite read-back, inside the hold
```

**A hold, not a write**, and that is the whole design. A write-then-restore loses two ways: a poll
landing inside the window clobbers it, and a refusal has to *guess* what to restore — which is wrong
if somebody pressed `F+0` in the meantime. The hold leaves the cadence's own reading untouched
throughout, so when it releases, what the cadence knows decides. It is a counter, not a flag, so two
concurrent requests cannot disarm each other. OFF is never pre-armed: arming is only ever the
pessimistic direction.

**Amending ADR 0163's single-writer invariant, with the reason.** 0163 reserved
`tuner.broadcast_fm` to `clear_broadcast_fm` because the *probe* only ever receives a refusal, and a
probe-written `on=True` could refuse a busy station's key-up on evidence that does not support it.
This route receives an `APPLIED` reply carrying state, frequency, band and flags — the complete
reading — so it writes. The invariant was never about arity.

A consequence worth stating: after the operator turns broadcast FM on, `refuse_if_deafened` reads the
block this route wrote, so a key-up whose clear is refused **will** refuse. That is correct — the
station really is deaf — and nothing about the deliberateness of the choice changes it.

### 6. `BroadcastFm` gains `band`

The route takes the band byte explicitly, so the read-back has to report it: 76.5 MHz is one station
under band 1 and a different one under band 2, and `hz` alone is half an answer. `None` where nothing
reported it, never `0`, because `0` is a real reading. This changes the `/status` block's key set,
which `tests/test_api.py` pins — updated deliberately, and `docs/api.md` with it.

### 7. The UI states the consequences rather than hiding them

A new `BroadcastFmPanel` ("Second receiver"), mounted when the radio has earned either direction and
self-hiding when it has earned neither — an unusable control is noise, and this one would be noise
with a frightening warning attached.

- **Consequences 1–3 are visible at all times**, in an amber `.notice`, not behind a disclosure and
  not in a modal. An operator deciding whether to press the button should not have to press it to
  find out what it does.
- **The second is worded to the measurement.** *"Real overs on this station's own channel are
  withheld from the links too, even though the radio hears them."* ADR 0163's M3 recovered a
  witness's 1000 Hz tone at power 0.995 *during* an over with the probe still reading FM — the mute
  is deliberately coarser than the radio. **"The station is deaf" is the thing M3 disproved**, so it
  must not appear; a vitest case asserts the word is absent.
- **The button is the two-step arm/confirm** `RestartButton` already uses (ADR 0047), self-disarming
  after 8 s. One stray click should not silence both links.
- **Consequence 4 — the repurposed keypad — is not in the pre-commit copy.** It appears only while
  broadcast FM is shown **active**, in the red `notice-rx-paused` variant. The person it protects is
  whoever walks up to the radio later and never saw the confirm; putting it at commit time shows it
  to the one person who does not need it.
- **Turning it off has no confirm step.** Leaving is always safe.
- `TalkControl`'s lockout sub-line is rewritten. It sent the operator to the radio's EXIT key because
  that was the only remedy; it now names the card on the same screen. That sentence *was* ADR 0161
  finding 2, in the UI, and this is where it stops being true.

---

## Bench

Deployed `5eccc21` on the bench station over SSH. **The AIOC tty was never opened by a script** —
ADR 0163 killed the dock link that way and `/status` reported healthy throughout — so every number
below came through the HTTPS/WSS API or the journal.

### B4 — the refusals, against the real radio

| asked | answer |
|---|---|
| `on` 104.35 MHz | **422**, host-side, no frame: *"104350000 Hz is off the 100000 Hz raster…"* |
| `on` 64.0 MHz, band 0 | **422** carrying **the radio's own** `ERR_BAND` |
| `on` band 4 | **422**, host-side: *"the firmware's FM_Band is a two-bit field, so it would clamp this silently"* |
| `tune` while the receiver is off | **409**, `ERR_OFF` |

`broadcast_fm` was byte-identical before and after all four. The 64.0 MHz leg is the split working:
the host did not know that frequency was out of band, and did not pretend to.

### B1 — the mute, from an operator route, on a real radio

`POST /broadcast-fm {"action":"on","hz":104300000,"band":0}` → **200 in 101 ms**
(`00:31:52.698` → `00:31:52.799`), read-back
`{on: true, hz: 104300000, band: 0, blocks_tx: true}` — **F9's interlock bit confirmed on the wire
for the fourth cycle running**.

```
t=  6.1s browser_bytes=        0  rms=    0.0 | mumble withheld=   0 deafened=False unk=0 | fm on=False
t=  8.1s browser_bytes=    82560  rms= 7089.4 | mumble withheld=  43 deafened=True  unk=0 | fm on=True
t= 12.1s browser_bytes=   466560  rms= 3434.2 | mumble withheld= 243 deafened=True  unk=0 | fm on=True
t= 30.2s browser_bytes=  2208000  rms= 3957.2 | mumble withheld=1150 deafened=True  unk=0 | fm on=True
```

The mute is armed in the **same 2 s sample** as the request. Browser audio at full level throughout —
hearing FM radio in the browser is the feature (ADR 0162) — while 1150 frames were withheld from
Mumble over 22 s, with **zero** unknowns.

### B2 — the key-up: refused, or does it take the receiver back?

Predicted from the code, measured rather than asserted. **Not refused.**

```
before: {'on': True,  'hz': 104300000, 'blocks_tx': True,  'rescues': 0}
POST /ptt {"on": true}  -> HTTP 200, transmitting: true
after:  {'on': False, 'hz': 104300000, 'blocks_tx': False, 'rescues': 1}
WARNING uvk5: this station was in broadcast FM and could not hear its own channel — switched the
        second receiver off before keying (rescue #1). It was tuned to 104.3 MHz.
```

### B3 — retune, and the way out

`tune` to 98.5 MHz on a running receiver → **200**, read-back `hz: 98500000`. `off` → **200 in
106 ms** (`00:34:25.459` → `00:34:25.565`), and `deafened` went `True → False` on the next sample:
the links resumed. Browser audio stopped 2 s later as the squelch gate closed on a quiet channel.

### The browser leg — the first end-to-end operator path in this arc

**It ran.** The operator drove the card from a real browser on the LAN (`192.168.1.30`); every number
below was taken server-side.

| | |
|---|---|
| `POST /broadcast-fm` ON, from the browser | **00:47:39**, 200 — the mute logged its first withheld frame in the **same second** |
| browser audio, unaffected | **3 805 440 B**, RMS 4137–5999 across the window |
| Mumble, withheld | **1952 frames** at that point, `deafened: true`, `deafened_unknown: 0` |
| `POST /broadcast-fm` OFF, from the browser | **00:48:42**, 200 — `deafened` back to `false`, withheld frozen at **3092** |
| `rescues` after a full browser ON → OFF | **0** — the fix of finding 2, on the operator's own path |
| browser total for the session | **6 069 120 B** |

Two things the screenshots settled that no assertion could. The card renders the red keypad warning
**only** while broadcast FM is active, which is the placement decision working as argued. And the
mute's own log line now reads *"Turn the second receiver off (POST /broadcast-fm, or the SECOND
RECEIVER card in the web UI), or press EXIT on the radio"* — that sentence **was** ADR 0161 finding 2,
and this is the run where it stopped being true.

### `acceptance.py` — a regression check, reported as one

**9 of 9 attempted PASS**; `split-minus` **SKIP** (no `Bench Split Minus` preset in `radio.toml`),
which still makes the banner print `RESULT: FAIL` — ADR 0161 finding 8, unmoved for a **fourth**
cycle. `auth` passed. This run says nothing about the route or the cadence: ADR 0163 established that
its `systemd` stage restarts the service, the Mumble link does not autoconnect, and with no bridge
relaying there is no poller. It is here because this branch touches the key path indirectly, not as
coverage.

### B5 — restored

**147.555**, `tune_persist` off, broadcast FM off, both units `active`, `rescues: 0`, and both config
files byte-identical to the pre-cycle snapshot (`radio.toml` `ead78a44…`, `radio-secrets.toml`
`ae86f7f1…`). Zero EEPROM writes in the journal for the whole cycle.

### What the bench found that pytest could not

1. **The ON capability could never be earned.** The deployed station came up advertising
   `clear_broadcast_fm` **and nothing else**: `_set_broadcast_fm_seen` was set only inside
   `set_broadcast_fm`, which is reachable only through a route gated on the capability that method
   would earn. The card would never have rendered and the button could never have been pressed. Both
   pytest tests passed, because both reached the flag through the one call that cannot run in
   production. Fixed — earned from the same `0x087A` the boot assert's clear already receives — and
   the new test asserts the property *from outside*: after nothing but the boot clear, a station must
   be able to offer the way in.
2. **The operator's own OFF was counted as a rescue.** `rescues` went 0 → 1 → 2 on the bench, and the
   second was the off button. The rescue flag is armed by a probe seeing the receiver running, and
   since ADR 0163 the **cadence** probes every 2 s — so a deliberate clear always found it armed.
   `docs/api.md` documents that number as key-ups rescued; a published number that counts something
   else is ADR 0159's "a flag must not lie", one layer up. Fixed with `disarm_rescue()`, reached by
   `getattr` so the duck-typed tuners are untouched; re-verified on hardware (`rescues` stayed 0
   across a full ON → OFF).

Neither was reachable before this cycle, and neither would have been found by any test that did not
run against the deployed station.

---

## Consequences, and what was deliberately not done

- **B1/B3 were run twice**: once as the REST-identical path (same route, body and token the card
  sends, driven by `curl`) and once by the operator from a real browser. Both are reported above with
  their own numbers. vitest coverage of the component was never offered as a substitute for the click.
- **`acceptance.py` does not cover any of this**, and ADR 0163 established why. Run as a regression
  check, reported as one: 9 of 9 attempted PASS with `split-minus` SKIP.
- **The mute still withholds real overs** while broadcast FM is selected (ADR 0163's M3 semantics),
  and the UI now says so in the operator's own words rather than only in an ADR.
- **No squelch-composition successor**, and no firmware. The fork is not touched.
- **`rescues` is now the honest count** of key-ups rescued. It will look *lower* than it did on the
  same station yesterday; that is the fix, not a regression.

### Carried forward

- **`0x0878` reports `tx_ok = 1` while F9 refuses to key** — a firmware defect, to fix alongside
  whatever merges F9 to fork `main`. Unmoved (ADR 0163 finding 2). **F9 is still not on `main`**
  (`d086a23` is F8), so a build from `main` has `0x0879`/`0x087A` and no TX interlock.
- **A second process on the AIOC tty kills the dock link and the transport never recovers** (ADR 0163
  finding 1). Unmoved, and the reason nothing here touched the tty.
- **`GET /status` and `/link/status` legitimately disagree** during the front-panel window. Unmoved.

### New findings

1. **Browser RX is squelch-gated, so "0 bytes" is not a fault.** `RxPump` publishes only frames the
   gate opens on, so a quiet channel produces no browser audio at all. Every browser-audio number in
   this arc (ADR 0162, ADR 0163 B1, B1 above) was taken with broadcast FM running, which is loud and
   continuous. Worth writing down because a future cycle measuring a *quiet* channel will read zero
   and think something broke.
2. **The API token is written to the journal in cleartext on every audio-socket connection.** The
   `/audio/rx` and `/audio/tx` sockets authenticate with `?token=` in the query string, and uvicorn's
   access log prints the full path: `"WebSocket /audio/rx?token=<the token>" [accepted]`. Anything
   that can read the user journal can read the LAN token. Guardrail 4 is explicit that auth here is
   gated access rather than secure access, so this is not an emergency — but a credential in a log
   file is its own small cycle, and the query-string handshake is a websocket constraint rather than
   a choice, so the fix is probably a log filter.
3. **`docs/server-notes.md` said the bench radio runs F8. It runs F9**, measured in ADR 0161 B1 and
   again this cycle (`flags=0x03`, and `blocks_tx: true` on every ON reply). Corrected.
4. **Two stale sentences in `docs/api.md`**, both corrected here: the 503 row still prescribed a
   server restart as the broadcast-FM remedy (ADR 0161 dropped the latch), and the `ERR_TX`
   explanation still said "an open squelch is most of an active QSO" (ADR 0163 refuted that from
   firmware source and the prose was never updated).

## Verification

`uv run pytest` — **2169 passed, 5 skipped** (baseline 2117/5). `npx vitest run` — **13 files, 131
tests** (baseline 12/116). Red run before any implementation: **50 failed, 2115 passed, 5 skipped**
and **14 failed, 117 passed** — all behavioural; the one collection error was fixed and re-run first,
because a cascade is not evidence (ADR 0162).
