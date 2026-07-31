# 0150 — The host learns to listen to AM

Status: Accepted

Implements the host side of the wire extension decided in ADR
[0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md), through the tuner seam of ADR
[0141](0141-one-byte-over-the-wire.md)/[0145](0145-instant-by-default.md) and the preset model
of ADR [0115](0115-channel-presets.md)/[0133](0133-repeater-split-and-chirp-import.md). Host only — the firmware
fork is not touched this cycle.

## Context

F7 firmware shipped last cycle. The radio answers `0x0877` set-modulation with an `0x0878` reply,
and `0x0873` no longer forces `MODULATION_FM` on every tune. **No host code speaks either frame**,
so the capability exists on the radio and is unreachable from the server: airband is still
impossible, and a preset cannot put the radio on the demodulator its channel needs.

The deployed station is `server.backend = "baofeng"` with `baofeng.uvk5_tuner = "hybrid"`, so
`HybridTuner` is the implementation that actually runs and `persist` defaults off — which makes
`volatile` true and puts `_reassert_channel` on the live key path rather than in a defensive branch.

### Read out of the firmware, not out of ADR 0149's prose

The wire contract below was taken from `App/app/dock.h` and `App/app/dock.c` at the fork's merged F7
commit. Nothing checks that the two repositories stay in step — ADR 0148 named that gap and it is
still open — so reading the source *is* the check, and the golden vectors are transcribed from the
fork's own host harness rather than from any description of it.

- `0x0877` carries **one byte**; `0x0878` carries **four**: `status`, `modulation`, `raw`, `flags`.
- Wire values are the **wire's own**: `FM 0`, `AM 1`, `USB 2` (number reserved, value refused at
  F7), `UNKNOWN 0xFF`.
- Status codes are `0x0874`'s, **holes and all** — `APPLIED 0`, `ERR_SHORT 1`, `ERR_BUSY 2`,
  `ERR_FIELD 4`, `ERR_NO_HAL 5`, with 3/6/7 unused so one table decodes both commands.
- On any refusal the firmware forces `modulation` and `raw` to `0xFF` and `flags` to `0`.
- `flags` bit 0 says whether the radio will key its **own** PTT path.
- `raw` is the radio's `ModulationMode_t`, **diagnostic only**: its numbering moves with
  `ENABLE_BYP_RAW_DEMODULATORS`.

One line of `dock.h` is a requirement on this repo rather than a note, and shaped two decisions
below: *"A reconnecting host must ASSERT the modulation it wants rather than assume FM."* The
firmware's sticky value is session state — reset by the radio's power switch, **not** by a host
restart.

### The name was already taken

`DockCommand.SET_MODULATION = 0x0872` and `class SetModulation` already existed, for the classic
Dock's `CMD_0872_t` — defined in stock firmware, **never dispatched**, never sent by this server.
The plain names now belong to the F7 pair and the stock one is `STOCK_SET_MODULATION` /
`StockSetModulation`. That rename is not cosmetic: it makes every stale call site fail loudly on the
new one-field signature instead of quietly sending a channel-changing frame under an opcode a radio
does not expect. Both values are pinned in one test, together, because the interesting failure is
them drifting into each other.

## Decision

**1. `SET_MODULATION` is its own capability, not a widening of `SET_MODE`.**

`SET_MODE` is wide/narrow **bandwidth** — `AiocBaofeng.set_mode` maps `FM`/`NFM` onto `VfoImage`'s
`narrow` byte inside `0x0873`. This is **FM/AM demodulation**, a different setting reached by a
different frame, and a backend can have either without the other. They are easy to read as synonyms
because both spell one of their values `"FM"`, which is exactly why they are separated at the
vocabulary level rather than at the docstring level: folding them would make `POST /mode` mean two
things and make its 501 unreadable.

The cost is forced and worth naming: `tests/test_capabilities.py` asserts
`len(FULL_CAPS) == len(Capability)`, so a new capability joins `CAT_CAPS` and `MockRadio` must
implement it. That is the same path `SET_POWER` took, and it is what keeps every API and UI test
hardware-free.

**2. `TUNING_CAPS` stops being one shared frozenset.**

`SetVfoTuner` and `HybridTuner` advertise `SETVFO_CAPS = TUNING_CAPS | {SET_MODULATION}`;
`EepromTuner` keeps `TUNING_CAPS`. It writes a channel record whose modulation nibble is a hardcoded
FM (`vfo.py:339`) into **stock** firmware that has no `0x0877` case at all — there is no version of
this that works for it.

`EepromTuner.set_modulation` **raises `UnsupportedCapability`** rather than returning. Not
advertising the capability is what makes the API 501; raising is what makes a direct call fail the
same way instead of reporting success for a radio still demodulating FM. A silent no-op here would
be the "no signal and no measurement look identical" fault (ADR 0140/0143) reintroduced one layer
down, and it is the one guardrail 3 exists to forbid.

**3. An absent `modulation` on a preset means FM — the opposite of `power`.**

`power` is absent-means-leave-alone (ADR 0146) because a power level belongs to the **station**: how
hard to talk is the operator's standing choice, and forcing a default on every channel tap would
silently undo it. A demodulator belongs to **what you are listening to** — an airband channel is AM
as a property of the channel itself, and a repeater is FM whoever is at the desk. So it defaults and
is written on *every* apply, exactly like `mode`.

Two things ride on that unconditional write. A channel list with one airband entry must return to FM
when the operator taps a repeater, or the repeater is inaudible for reasons nothing reports. And
because the firmware's sticky modulation outlives a host restart, a server that only wrote it when a
preset asked for AM could come back up believing FM while the radio is still on AM. Applying it
every time *is* the assert-at-connect `dock.h` requires.

**4. Decode by explicit value, never by enum position.**

`raw`'s numbering moves with a build flag and `0xFF` is a sentinel, not an index. Every mapping is an
explicit dict lookup with no positional fallback, and an unnameable modulation decodes to `None`
rather than to a neighbour. `SetModulationReply.name` returning `None` is load-bearing: a caller that
took a fallback as `"FM"` would read every refusal as a radio sitting on the one modulation that can
transmit.

**5. `tx_ok` is reported everywhere, and it also refuses a key-up.**

*Reported* on every backend, never gated on `server.backend`, because the asymmetry it describes is
*between* the keying paths: the AIOC's DTR line runs into `RADIO_PrepareTX`, which
`VFO_STATE_TX_DISABLE` blocks in any non-FM modulation, while the dock's own REG_30 keying never
enters it. The same radio state means "dead transmitter" on one backend and "transmits normally" on
another, and no client can infer which.

*Enforced* in `AiocBaofeng._key_on`. Without it, setting AM makes the DTR line go high, the radio
decline, and the over be silence — while `status()` reports `transmitting` and the UI lights up.
That is precisely the fault class ADR 0140/0143 spent four cycles on, and under guardrail 5 the
transmission it swallows is the **automatic station ID**, which Part 97 makes required controller
behaviour rather than a feature. So a measured `False` raises `RadioUnavailable` (a 503 carrying the
reason) before the line is asserted or any audio device is opened.

**Only a measured `False` refuses.** `None` means nobody has asked this radio, and an unknown must
never block a key-up: the whole tuning surface reports `None` before its first assertion, and a
station that would not transmit until someone had chosen a demodulator would be a worse failure than
the one this prevents.

**`ENABLE_TX_WHEN_AM` stays off.** AM is receive-only; the flag reports the condition and the refusal
makes it legible. Neither removes it.

**6. The key-path re-assert carries the modulation, and carries it first.**

`dock_ctx_t.modulation` is RAM, reseeded FM by a radio power cycle — exactly the staleness
`volatile`/`reassert` already exists for. One extra one-shot frame, arming no lockout. Order matters
and is not incidental: sending it *before* the `0x0873` is what makes the `tx_ok` the refusal reads a
measurement from milliseconds ago rather than a memory from whenever the operator last chose.

**7. Modulation is not staged through `VfoImage`.**

Every other setter builds a pending channel that `commit_tuning` writes. This one does not: the
modulation is not on `0x0873`'s wire and is not part of a channel. It is a one-shot frame the
firmware then keeps, so it needs no pending channel and works before a frequency has ever been set —
where `_stage` would refuse with "set a frequency before a split, tone or mode". That is the right
answer for a tone, which belongs to a channel, and the wrong one here.

## Acceptance

**Fail-first, run against the bug rather than cited — three times, because there were three
distinct ways to be wrong.**

- **The command carries the wrong byte** (`pack` always emits FM): **2 failures** — the byte-exact
  command golden and the round-trip. Nothing else moved, which is what tells you the golden is
  pinned to bytes rather than to whatever the implementation emits.
- **`tx_ok` never reads the flag** (returns `True` unconditionally): **5 failures** across three
  files — the reply golden, the refusal-sentinel case, and three tuner cases including the hybrid
  delegation. The blast radius is the point: the flag is load-bearing in more than one layer.
- **`EepromTuner.set_modulation` silently returns** instead of raising: **exactly 1 failure**, the
  refusal test. The capability-split test **stayed green**, which is stated here so nobody later
  cites the split as evidence that the refusal works. It is not; only the refusal test is.

**Golden vectors have an independent oracle, twice over.** Both frames were transcribed from the
fork's own host harness (`tests/host/test_dock.c`, cases 25 and 26, at the merged F7 commit) and then
re-derived from `tests/test_uvk5_frames.py`'s reference framer — a different implementation of the
documented framing than `frames.py`, written from the spec. Both reproduce byte-for-byte. The reply
fixture deliberately carries `flags = 0`: AM applies and cannot transmit, which is the case a host
most needs to read correctly.

**`uv run pytest`: 1939 passed, 5 skipped** (1884/5 before — 55 new tests).

## Consequences

- Airband and any AM receive service are reachable for the first time: `POST /modulation`, or a
  `[[presets]]` entry carrying `modulation = "AM"`.
- **A station on AM will not transmit, and now says so** with a 503 instead of dead air. An operator
  who leaves the radio on an airband preset has a station that cannot ID until they set FM. That is
  the firmware's behaviour, not a choice made here; what changed is that it is visible.
- `RadioStatus` gains `modulation` and `tx_ok`, both `null` on every backend that cannot answer them.
  The web UI reads capabilities generically, so it degrades correctly with no change — and gains no
  control this cycle.
- The `[[presets]]` schema gains a field. Existing configs are unaffected: absent means FM, which is
  what every existing preset already got.
- One capability, one route, one preset field, and one more frame in the key path when a modulation
  has been asserted. The frame arms no lockout, so the key path costs no additional wait.

## Out of scope

- **The web UI** — next cycle. No control is added and none is greyed differently.
- **A `doctor` F7 probe.** `doctor` still reports F6 only. The tuner raises a `TuneError` naming
  pre-F7 firmware at the point of use, which is where an operator meets it; a probe would be a
  second, weaker place to learn the same thing.
- **`vfo.py:339`'s `_MODULATION_FM`.** The EEPROM record's modulation nibble stays FM. That is now
  *consistent* rather than a latent bug, because `EepromTuner` does not advertise the capability.
- **`USB` on the wire.** The number is reserved in both repos; the value is refused in both. Nobody
  has put this radio on USB on a bench and it cannot transmit in it on this build, so accepting it
  later stays purely additive (guardrail 1).
- **The three-way opcode census** (`ADR 0111:52`, `ADR 0119:46`, `frames.py`) still disagrees with
  itself, and `0x0873/4`'s collision with the classic Dock's backlight pair is still shipped and
  still unwalkbackable. Unchanged by this cycle.
- **Any hardware claim.** The radio is not flashed with F7. AM audio over the AIOC and the PTT
  refusal remain `⚠ CONFIRM AT BENCH` items in the fork's `BENCH.md`; everything here is proven
  against fakes that model the firmware, and nothing in it has been measured on the air.

## Addendum (ADR 0151)

Recorded so a later cycle finds this stated rather than inferring it was reviewed.

**Decision 5's second half — the `tx_ok` key-up refusal in `AiocBaofeng._key_on` — was a
behavioural change to an existing keying path, and it was not asked for.** The brief for this cycle
specified that `tx_ok` be *reported*: "`RadioStatus` gains `modulation` and `tx_ok`", "`tx_ok` is
reported whichever backend is selected". It said nothing about enforcing it. Making a measured
`False` raise `RadioUnavailable` before the line is asserted changed how `ptt(True)`, `transmit()`,
the station ID and the TOT decorator all behave on a path that predates this cycle. That is a wider
blast radius than "add a field", and it shipped inside a cycle whose title was "set-modulation, host
side".

The behaviour is right and is **not** reverted: an AM key-up that produces silence while `status()`
reports `transmitting` is precisely the fault class ADR 0140/0143 spent four cycles on, and under
guardrail 5 the transmission it swallows is the Part 97 station ID. The Consequences section above
already states the operator-facing effect. What was missing from this ADR is that the change was
unrequested — so nobody reading it later concludes the scope was agreed in advance.

It is also what surfaced the bug ADR
[0151](0151-a-failed-key-up-must-give-the-radio-back.md) fixes. Before this refusal existed, a
`ptt(True)` on the deployed station essentially never raised, so `TxSession.feed` claiming the
arbiter *before* keying had never been exercised as a failure path. The first routine raise on that
path stranded the arbiter in `TRANSMITTING` for the life of the process. Shipping the refusal
without auditing what upstream did with a raising key-up is the specific gap; the fix is ADR 0151,
and the two should be read together.
