# 0145 — Instant by default, and the key-up that makes it safe

Status: Accepted

Builds on ADR [0144](0144-instant-and-persistent.md). Corrects its lockout arming rule.
Changes how ADR [0133](0133-repeater-split-and-chirp-import.md)'s `rx_tone` disclosure is delivered.

## Context

ADR 0144 shipped `hybrid`: `0x0873` moves the RF now, an EEPROM write makes it survive the power
switch. It works. But **persistence is the only thing that costs the six-second transmit lockout**,
and the operator asked for the other side of that trade:

> "if we switched to instant freq change the biggest downside the radio reboots and starts on a
> random channel and I have to set it back to where it was, right?"

Three corrections, and the third is the whole design.

**Not random.** The radio comes back on whatever its EEPROM last held — a *stale* channel, not an
arbitrary one. **Not a reboot.** Instant never reboots; that was `eeprom`'s cost and 0144 removed it.
The radio forgets only when it is switched off. **And the upside was not named:** the dock opcodes
arm no lockout, so instant means listen *and* talk at once, with no countdown at all.

That mode already existed as `uvk5_tuner = "setvfo"`. What did not exist was a way to **choose** it
without editing config and restarting — `POST /settings` returns `apply: "restart"`, which is the
wrong ceremony for a choice that changes with the afternoon.

### The hazard that makes this more than a default

`AiocBaofeng.status()` reports `self._tuned` — the channel *this server chose* — and the UI
highlights from it. Under storage that stays true across a power cycle. Under instant it does not:
the radio boots on the stale channel, the server and every browser still show the one you picked, and
**the next key-up transmits on the wrong frequency**, silently.

ADR 0144 put "re-assert before key-up" out of scope, reasoning that hybrid persists so it buys
nothing. Making instant available inverts that reasoning exactly.

## Decision 1 — persistence is a live switch, not a startup mode

`HybridTuner.persist`, default **off**, moved at runtime by `POST /tuning/persist` and reported as
`RadioStatus.tune_persist`. `baofeng.uvk5_tune_persist` sets the boot value only; flipping the switch
does not write config back.

`null` and `false` are kept distinct — "no such choice" versus "the switch is off" — so the UI hides
the control on `setvfo` (never stores), `eeprom` (always does) and every other backend, rather than
rendering one that does nothing.

Turning it **on** stores the channel the radio is *already* on. Otherwise "save to radio" would save
nothing until the operator happened to tap something, which is not what the words say. It is refused
mid-transmission, and that is not politeness: storing arms the lockout, and `SerialConfigInProgress()`
**cuts an over already in progress** (`app.c:1111, 1146, 1191`).

**`baofeng.uvk5_tuner` keeps its `off` default.** The operator's other question — "would a UV-5R
still work as good as it did before all this?" — is the reason, and the answer is yes: with `off`,
`_build_tuner` never runs, the port is never reconfigured for dock baud, `capabilities()` returns bare
`SHARED_CAPS`, and `tx_ready_in()` short-circuits. Defaulting the global to instant would make every
plain UV-5R start firing dock frames at a jack that has no UART on it.

## Decision 2 — a volatile tune is re-asserted before the line goes high

Tuners declare `volatile` — "a tune I made lives in the radio's RAM" — and `reassert(image)`.
`AiocBaofeng._key_on()` calls it **first**, ahead of even the lockout wait, because it is the one step
that can decide the key-up should not happen and the cheapest place to refuse is before anything has
been opened or waited on. A radio that does not confirm gets a 503 with the reason (ADR 0143); the
line is never driven high and no audio device is opened.

The mechanism is available precisely here. `SetVfoTuner.apply` sends **no HELLO** — `0x0873` needs no
session — while a HELLO arms six seconds of mute at `uart.c:355`. So **re-tuning the radio costs
strictly less than asking it where it is.** One frame, its `0x0874` read-back, about ten milliseconds,
no lockout.

`EepromTuner.reassert` is an empty body with a comment, and the emptiness is load-bearing: its
`apply` sends a `Reset` and sleeps out a lockout, so wiring it here would reboot the radio at every
key-up. A test asserts no `Reset` leaves it.

## Decision 3 — the `rx_tone` notice goes

ADR 0133 decided `rx_tone` is stored, not honoured, and **says so out loud**. The fact is unchanged;
the delivery was wrong. The notice fired on every apply of every channel carrying one — **29 of the
operator's 41** — was never actionable, and read as an error in the same amber box the actionable
skips use. It also said *"not supported on this radio"* when it meant *this software, on every radio*.

Repeating an unactionable warning beside the actionable ones is how the actionable ones stop being
read. So the web UI filters the notice on a discriminator the payload already carried: a capability
gap has a `Capability` string, an unhonoured field has `capability: ""` and a `reason`.
`GET /presets`, `POST /presets/apply` and the config docs are untouched.

## Correcting ADR 0144 — the lockout is not a consequence of writing

0144 armed `tx_ready_at` only when flash actually changed (`if wrote else None`). That is wrong twice.

`gSerialConfigCountDown_500ms` is armed by the HELLO at `uart.c:355` and by every EEPROM **read** at
`393`, not just the write at `447` — the four-site fact 0144 itself established — and `write_channel`
opens with a handshake and ends with a read-back verify. **So a store that changes no flash still
mutes the radio**, and reporting it ready is exactly the ADR 0142 fault the bench caught by failing
every carrier row on attempt #1. It survived only because `commit_tuning` short-circuits a re-tap
before the tuner is reached; it surfaces after a service restart, when the server has forgotten the
channel and the radio has not.

Second, the instant path must not *clear* a deadline it did not cause. A lockout still running belongs
to earlier traffic and is still real. Instant arms nothing and now clears nothing.

## Consequences

| | instant (default) | stored |
|---|---|---|
| channel change | one dock frame, ms | plus an EEPROM write |
| transmit after a change | **immediately** | after ~6.5 s, counted down in the UI |
| radio switched off | boots on the last *stored* channel | boots on this one |
| first key-up after that | **correct** — re-asserted | correct |
| listening after that | **stale until a channel is tapped** | correct |
| flash wear | none | one write per change |

That "stale until tapped" row is the honest residual cost, and it is measured below rather than
described.

## Acceptance — measured on hardware

**The fail-first, re-measured this session rather than cited.** The persistence row run against the
merged `master` (17ad15b) with `setvfo`:

| Build | Mode | Persistence row |
|---|---|---|
| `master` 17ad15b | `setvfo` | **0/6**, carrier 0.00 s, exit 1 — *"the radio did not come back on the decoy"* |
| this branch | `setvfo` | **6/6**, carrier 2.18–2.25 s, exit 0 |

Same radio, same config, same script, one code change.

**What that row now measures changed, and reading it the old way inverts the result.** It asks whether
the next over goes out on the channel the operator picked after the radio has been switched off. Two
mechanisms satisfy that now — storage, and the key-up re-assert — so `setvfo` *passing* is the fix,
not a broken gate. The script says so where the next cycle will read it.

**Separating them needed a new row**, `run_storage_contrast`, with the same decoy discipline 0144
learned: store a known decoy first, so "instant stored nothing" and "storage already held it" cannot
produce identical evidence.

| Row | Result |
|---|---|
| STORE 1 — storing arms the lockout | 5/5, 6.49 s |
| STORE 2 — an instant tune arms none | 5/5, null |
| STORE 3 — after a reboot it hears the **stored decoy** | 5/5, tone 0.96 |
| STORE 4 — and is deaf on the instant channel | 5/5, tone 0.00 |
| STORE 5 — but a key-up still lands there | 5/5, carrier 2.18–2.23 s |

Rows 3 and 5 together are the whole ADR: the radio came back on the *old* channel, and the over went
out on the *right* one anyway.

**The rest of the modes, on the same branch:**

| Gate | Mode | Result |
|---|---|---|
| differential, 8 carrier/silence/RX rows | hybrid **instant** | **16/16** — RF still follows with a dock round-trip in the key path |
| persistence | hybrid **instant** | **6/6** |
| persistence | hybrid **stored** | **14/15** — see below |

That stored figure is two runs and both are reported. The first scored 5/6: KEEP 3 (the silence row)
read **0.30 s** of carrier against a 0.25 s bar, where a real carrier on that row reads 2.21 s and the
other trial read 0.00. The re-run scored **9/9**, all silence rows 0.00. It is a brief blip near the
threshold on a row this cycle did not touch — in stored mode `volatile` is False, so
`_reassert_channel` returns immediately and the key path is byte-identical to ADR 0144's. Recorded
rather than re-run until green.

`uv run pytest` 1851 passed / 5 skipped; `npm test` 65 passed.

## Two gates that were wrong before the code was

**STORE 2 failed an implementation that was correct.** It read `tx_ready_in` immediately after STORE 1
stored a channel, and saw that store's deadline still counting down — 6.49 s, then 6.39 s a tenth of a
second later — which it scored as the instant tune having armed one. A draining deadline and a fresh
one are indistinguishable in a single sample. The row now drains the lockout before asking, and passes
5/5.

**STORE 4 was contaminated by the row before it.** STORE 3 reads 0.96 on a 1000 Hz tone and STORE 4
immediately looks for the absence of that same tone, so anything left in the RX pump's buffer lands
squarely on the discriminator: one trial in five read **0.22** against a 0.05 floor where the other
four read 0.00–0.03. The fix is a settle between the paired probes, not a wider threshold — and the
order stays "deaf second", because carry-over in that direction can only produce a false **fail**.
Reversed it would produce a false pass. With the settle the row reads **0.00 on all five**, max
included, which is what confirms the diagnosis rather than merely making the row green.

Both are the ADR 0144 lesson again, one level down: a row written with the failure in mind still has
to be checked against something that should pass, or it fails the wrong thing.

## Out of scope

- **Honouring `rx_tone`.** The UV-K5 record has the fields — byte `[8]` rx code and the low nibble of
  `[10]` are hardcoded to OFF (`vfo.py`) — but tone squelch makes the radio deaf to anything without
  the matching tone, which lands on the RX pump, the VAD and DTMF decode. Separate cycle.
- **Retiring the `uvk5` backend.** `baofeng` plus a tuner now matches it on frequency, split, tone and
  mode. What only `uvk5` still has is `SCAN`, a real RSSI busy line, PA read-back and
  register-confirmed keying — and it carries the stuck-key history, and `POST /radio/select`
  baofeng→uvk5 still segfaults (139).
- **Persisting the switch across a service restart.** The config key sets the boot value.
- **2 m TX** — still unverified: 36 of 38 repeater presets are 145/144 MHz and the witness is
  SA818-**UHF**. Unchanged here, and it stays unclaimed. Absolute RF power likewise.
