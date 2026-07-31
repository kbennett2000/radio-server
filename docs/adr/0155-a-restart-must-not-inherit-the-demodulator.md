# 0155 — A restart must not inherit the demodulator

Status: Accepted

Completes the AM arc of ADR [0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md) →
[0150](0150-the-host-learns-to-listen-to-am.md) → [0151](0151-a-failed-key-up-must-give-the-radio-back.md)
→ [0153](0153-one-frame-must-not-take-the-relay-down.md) → [0154](0154-two-controls-must-not-both-say-fm.md)
by closing the one path that never asserts a demodulator at all: **process start.** Applies the
"state it, never adopt it" rule of ADR [0132](0132-dock-band-and-the-register-model.md) to a value
that had no assertion point. Host only — no firmware change, no UI, no API change.

## Context

The UV-K5's modulation lives in the **dock session** — RAM that a host restart does not touch, and
that only the radio's own power switch reseeds. `SetVfoTuner` seeds `modulation = None` at
construction and assigns it only after a confirmed `0x0878`. Nothing at boot ever sends that frame:

- `AiocBaofeng.__init__` opens the serial handle, applies port settings and forces both lines low.
  **It sends no frame to the radio at all.**
- `_reassert_channel` — the only other place a modulation is re-stated — returns early on
  `self._tuned is None`, *before* its modulation block. On a fresh process nothing is tuned, so it
  never reaches it.

So a server restarted against a radio the operator left demodulating AM reports
`modulation=null, tx_ok=null`. Every layer above then behaves **correctly** and the fault still
lands: `_refuse_if_tx_disabled` refuses only a *measured* `False` and rightly lets `null` through;
the ADR 0154 UI does not lock Talk, because `null` must never lock. The first key-up drives the
AIOC's DTR line into a transmitter `VFO_STATE_TX_DISABLE` has already disabled. The over is silence,
`status()` reports `transmitting`, and under guardrail 5 the transmission it eats is the **station
ID**.

The mechanism was already written down, in `presets.py`:

> its sticky modulation is session state that outlives a host restart, so a reconnecting server must
> state what it wants rather than assume FM.

That is true only of the path that runs through `apply_preset`. **Boot does not run through it.** The
sentence describes a rule the server did not yet keep everywhere it applies.

## Decision

### 1. Write, not read — and what read would have cost

At construction the backend **states** `FM` and checks the reply. The server's belief becomes *true*
rather than better-guessed, and `tx_ok` is measured in the same round trip. Reading was rejected
three separate ways, and the losing option's cost is worth recording:

- **There is no get-modulation opcode.** Adding one means a firmware fork, a flash and a fresh bench
  proof — the whole ADR 0148/0149 apparatus — to *learn* a value the host can simply *make* true
  with a frame it already ships.
- **Inferring it from BK4819 over `0x0851` reads the wrong thing.** The firmware's own VFO state is
  the truth; a register read while un-keyed is its leftover. That argument is already in this repo
  twice, for `_pa` ("reg 0x36 read while un-keyed is the firmware's leftover, not what the PA will
  do on the next over", ADR [0134](0134-repeater-keyup-in-the-field.md)) and for split (ADR
  [0133](0133-repeater-split-and-chirp-import.md)).
- **Even a *successful* read is ADR 0132's "take whatever state you find."** It would report a
  demodulator this server never chose — precisely what the `None` seeding comment in `tuner.py`
  exists to forbid. A cycle whose goal is a true belief must not get there by reversing the rule
  that made the belief honest.

The write costs one `0x0877`, keys nothing, and **arms no TX lockout**: `SetVfoTuner.tx_ready_at` is
`None` always, because the dock opcodes are not among the sites that arm `SERIAL_TX_LOCKOUT_S`. On a
`HybridTuner` it stays one frame even with `persist=True` — `set_modulation` is a bare delegation to
the setvfo half and never touches flash.

### 2. In the constructor, not the lifespan

The house pattern for best-effort work at boot is a **lifespan** step (the D-STAR auto-link). It is
wrong here. `RadioHolder.rebuild` constructs a fresh backend through `_radio_factory`, and `_restore`
constructs a second on the rollback tail — a lifespan step reaches neither, so a **live backend swap
would reopen exactly this gap**. The constructor covers boot, swap and rollback with one call site
and no new protocol surface. The precedent is the dock backend itself, which already applies config
frequency/mode/tone at construction.

It runs as the **last statement of `__init__`**, after `atexit.register(self.close)`, so a raise that
is deliberately *not* caught (see 4) cannot leak the serial handle.

### 3. Best-effort — and the asymmetry with the fail-loud line twenty lines above is the point

`apply_port_settings` two dozen lines earlier is deliberately **not** suppressed: "a tuner that
cannot talk to the radio should fail at startup, where it is one clear traceback." This one is
caught. The two are not in tension:

- That one fails when the serial **handle** is unusable. Nothing this backend does works afterwards,
  so there is nothing to degrade *to*.
- This one fails when a perfectly good handle has a radio **switched off or unplugged at the far
  end** — an ordinary Tuesday, because operators power the radio after the server. And the failure
  is **representable**: `modulation` stays `None`, the honest "this server has not asserted one" the
  tri-state was built for, which nothing downstream mistakes for a measurement.

Taking the process down for it would make a station that boots before its radio *unstartable*, to
fix a fault that only bites on a key-up.

### 4. The exception tuple, argued member by member

`except (TuneError, OSError)`. Two members, no subsumption between them, so one clause is correct and
there is **no order to get wrong** — stated explicitly so a reader working from ADR 0153's
order-is-load-bearing rule does not go looking for one.

- **`TuneError` — in.** The designed failure, covering all three of its cases: silence (radio off, or
  pre-F7 firmware with no `0x0877` case), a refusal status, and a reply naming a different
  demodulator. All three raise *before* the record, so the post-catch state is exactly
  `modulation=None, tx_ok=None` with no cleanup needed. It is a `RadioUnavailable`: letting it escape
  would kill the process at boot, or — worse — roll back a **working** backend swap in
  `RadioHolder.rebuild`, whose `except Exception: self._restore(previous); raise` would undo a good
  switch because a radio was switched off.
- **`OSError` — in.** The one fault that reaches here unwrapped. `Uvk5Transport._raise_if_failed`
  re-raises the reader thread's stored error **verbatim**, and `serial.SerialException` subclasses
  `OSError`, so a cable yanked between the open and this frame arrives with no dock vocabulary on it.
  This is ADR 0153 §2 word for word: a yanked cable kills it identically, it simply arrives as a
  different exception type, and refusing it would make the tuple a **partial** fix rather than a
  smaller one.
- **`Uvk5Timeout` — out, deliberately.** It cannot escape: `send` wraps every write failure into it
  and `set_modulation` already converts it to `TuneError`. Catching it would be catching a type the
  shipped path cannot produce. It is pinned *instead* by a test that drives a genuinely silent radio
  — a stricter guarantee, because widening the tuple would **hide** a refactor that dropped that
  conversion, where the test makes it go red at construction.
- **`Uvk5Closed` — out.** Only raised once `_closed` is set, which only `close()` does. Unreachable
  microseconds after the transport is built.
- **`ValueError` — loudly out.** Raised only for a value outside `VALID_MODULATIONS`, i.e. a typo'd
  `BOOT_MODULATION`: a programming error that must fail at construction as one clear traceback, the
  identical rule this module already applies to `uvk5_power`. This is the decisive argument against
  `except Exception:`.

### 5. Gated on the capability, never on `hasattr`

`Capability.SET_MODULATION in tuner.capabilities()`, mirroring `_apply_fields` in `presets.py`. Never
`hasattr(tuner, "set_modulation")` — **every** tuner in the package has that attribute, and one of
them exists only to raise (`EepromTuner.set_modulation`, guardrail 3). The gate is what makes an
`eeprom` tuner on stock firmware, and a plain UV-5R with no tuner at all, silently correct instead of
a crash at construction.

### 6. `BOOT_MODULATION`, and why it is not `presets.DEFAULT_MODULATION`

Same four characters, deliberately not the same symbol. They answer different questions and would
change for different reasons:

- `presets.DEFAULT_MODULATION` answers *"what does a preset that names no modulation mean?"* — a
  question about the operator's channel list, which could reasonably move. A future cycle could
  plausibly make an absent `modulation` mean "leave the station alone", the way `power` already
  works.
- `BOOT_MODULATION` answers *"what does this server assert when nobody has said anything?"* — a
  question about whether the transmitter works at all, which must not move with it.

FM because it is the only value this station can **transmit** in: built without `ENABLE_TX_WHEN_AM`
the firmware disables its own PTT path for anything else, and that path is where the AIOC keys. A
test asserts the two constants are equal today, with a comment saying it documents a coincidence and
is *not* a coupling — it is where the argument gets re-had if one ever moves.

### 7. No config key, and therefore no opt-out

A `baofeng.uvk5_boot_modulation` key was considered and rejected. The primary reason is not its cost
(six code sites, a regenerated `radio.toml.example`, the settings canary, a docs row) but that it
would be **the sole member of an incomplete set**: this backend asserts no boot frequency and no boot
tone either, so an unattended airband monitor would come back on the right demodulator and the
**wrong frequency**, which is not a working station. The key would advertise a completeness the
backend does not have.

The whole-shaped successor is a **boot preset** — one key, one lookup in the existing `[[presets]]`
table, reusing `apply_preset`, which already writes modulation, frequency, tone and power together.
Named here as a future item, not as work for this cycle.

**The residual is stated plainly, as accepted cost rather than oversight: with no key, a restart
always pulls the radio out of AM, and there is no opt-out until that boot preset exists.**

### 8. A failed assert is recorded in the log only — for correctness, not economy

`logger.warning` naming the cause and the remedy, and nothing else. When the assert never landed,
`modulation` is `null`, which the ADR 0154 UI renders as `—` — and **`—` is true.** A UI channel
would explain *why* the value is unknown; it would not make it known. The reason belongs in the log,
which is this codebase's stated rule for an unmeasured value.

There is also a structural reason no boot diagnostic can reach the operating log today, recorded
separately below because it blocks more than this cycle.

## Acceptance

**Fail-first, one test, run against `origin/master` (`9e33329`): 1 red.**

`test_a_restart_against_a_radio_left_on_am_reports_what_it_is_actually_demodulating` builds the real
`AiocBaofeng` over the real `SetVfoTuner` over a fake dock radio seeded with the state the operator
left behind, and asserts the server's belief **equals the radio's state** — not that it equals
`"FM"`, which would still pass on a host that guessed right by luck. On master:

```
E       AssertionError: assert None == 'AM'
```

The defect in one line: the server believes nothing while the radio demodulates AM. On master the
fake records **zero frames sent**, so the radio simply stays where the operator left it.

The fake, `FakeSetVfoRadio`, **echoed** the request before this cycle, so its existing `mod=` knob
models a radio that *refuses to leave* AM rather than one that was *left* on it and complies. It
gained a `left_on=` seed and a sticky `demodulating` attribute, both purely additive: no reply byte,
no recorded frame and no raise changes, and its fifteen existing users are untouched.

**`uv run pytest`: 1981 passed, 5 skipped** (1973/5 before — 8 new tests, 3 repaired in place).
**`cd web && npm test`: 101 passed, 11 files, unchanged** — no web change, run as the gate that
proves it.

**Three existing tests changed, and how they were repaired matters.** All three pinned a state that
this cycle removes for a modulation-capable tuner. Two of them were repaired by reaching that state
the only way it still exists — a startup assert that **failed** — and not by resetting a fake after
construction, which would fabricate a state the production code can no longer produce:

- `test_set_modulation_reaches_the_tuner_and_is_reported_in_status` — `None` becomes `"FM"`/`True`.
  The **comment was the load-bearing edit**: it must still distinguish the FM we asserted and had
  confirmed from the FM the firmware seeds, or the next reader concludes ADR 0132 was reversed.
- `test_an_unknown_tx_ok_never_blocks_a_key_up` — reaches `None` via a failing assert, and is
  **stronger** for it: it now also covers the best-effort path's most important consequence, that a
  station whose radio came up second must still key.
- `test_nothing_is_reasserted_before_a_modulation_has_ever_been_chosen` — its premise is abolished
  for a capable tuner, so it now pins the same invariant on the failed-assert path.

The remaining eight `ModulationTuner` tests passed unchanged, each for a checkable reason. **That the
capability gate sorted every other fake in the suite correctly with no edit at all** — five fakes
advertising `TUNING_CAPS`, one advertising `SET_MODULATION` — is itself evidence it is the right
seam: it is the same predicate `presets.py` already gates on.

## Consequences

- On the `baofeng` backend with a `setvfo` or `hybrid` tuner, `status().modulation` is `"FM"` and
  `tx_ok` is `True` from the moment the server starts, instead of `null`. `docs/api.md` said `null`
  meant "before this server has asserted one" — that sentence became actively misleading and was
  rewritten rather than left to age.
- **An operator running an unattended AM airband monitor is worse off than before:** their radio is
  forced to FM on every restart and every backend swap. The honest rebuttal is that their AM never
  survived much anyway — the firmware reseeds FM on the radio's own power cycle, which this repo
  already states — so "AM survives" was only ever true for a host restart with the radio left
  powered. Mitigation ships today: an airband preset carrying `modulation = "AM"` restores it on one
  tap. The boot preset of decision 7 makes it automatic.
- A backend **swap** silently returns the radio to FM, because `rebuild` and `_restore` each
  construct a fresh backend. Intended — a rebuilt backend has a fresh, empty belief and must state
  rather than inherit — but operator-visible, so written down rather than discovered.
- A station whose radio is off at server start logs one warning and comes up working, reporting
  `null` for both fields. That is the same surface as a backend that cannot set a modulation at all,
  which is the accepted cost of decision 8.
- A plain UV-5R (`uvk5_tuner = "off"`) and an `eeprom` tuner on stock firmware are completely
  unaffected: the capability gate returns before any frame.

## Out of scope

- **R1 — a radio that refuses to leave AM leaves the belief `None`, and one branch discards a truth
  the radio sent.** Precisely one branch: on any non-`APPLIED` status the firmware forces the reply's
  modulation to `0xFF`, so a **refusal** can never describe a demodulator and there is nothing to
  keep; a **mismatch** reply carries what the firmware read out of the radio's own VFO, and
  `set_modulation` raises without recording it. Recording it would invert the record-only-on-success
  rule that three existing tests pin, and needs its own ADR to decide whether a *checked refusal*
  counts as a measurement. Benign today, and pinned by a test: `None` does not enable a bad key-up,
  it merely fails to prevent one.
- **R2 — a radio powered on *after* the server is never re-asserted.** Nothing retries. A retry needs
  a policy (when, how often, on whose thread) and a home; a boot-time loop would park startup behind
  a radio that may never appear; and the lazy-retry variant adds a frame to the RF key path that
  `_reassert_channel`'s own docstring says must stay fast — the path ADRs 0151/0153 spent three
  cycles making survivable. **And the residual is small**, because `apply_preset` writes modulation
  unconditionally, so a preset tap already re-asserts it. What is left is exactly: a radio powered on
  after the server, where the operator applies no preset and sets no modulation.
- **R3 — `doctor` never builds a tuner**, so `doctor --key-test` on a UV-K5-behind-AIOC station keys
  with zero modulation knowledge. `_build_backend` passes five kwargs and no `uvk5_tuner`, so the
  capability gate returns immediately. The one tool whose entire job is a deliberate key-up is the
  one place this cycle does not reach. The fix — doctor builds the configured tuner — changes what
  `--key-test` costs and is a doctor cycle.
- **R4 — no config key and therefore no opt-out from the FM-on-restart behaviour** (decision 7).
- **R5 — the EventHub does not exist at backend construction.** `build_app` builds the radio *before*
  `create_app` wires the hubs, so **no boot-time diagnostic of any kind can reach the operating log**
  — not this one, and not the next cycle's. Recorded by name so whoever wants one finds it written
  down instead of rediscovering it.
- **R6 — the stated `tx_ok=True` has a shelf life.** The front-panel knob can move to AM and nothing
  re-reads. Partly mitigated at key-up by `_reassert_channel`, but only once a channel has been
  tuned. The boot assert is a starting point, not a standing guarantee.
- **The three websocket talker-slot leaks** (`app.py:1873`, `:1995`, `:2095`), **`POST /mode`
  returning 500 on a bad value**, **`MockRadio.set_mode` accepting what every real backend rejects**,
  and **`GET /presets` omitting `modulation`** — all still carried from ADRs 0153/0154, all still
  their own cycles.
- **Any hardware claim.** The radio is not flashed with F7. Every `0x0877`/`0x0878` exchange here is
  modelled by fakes; nothing has been measured on the air.
