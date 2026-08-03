# ADR 0178 — Checks that silently answer the safe-looking default

**Status:** Accepted
**Date:** 2026-08-03
**Supersedes / relates to:** [ADR 0177](0177-the-key-up-race-is-real-and-it-costs-the-whole-transmission.md),
[ADR 0172](0172-the-mock-stops-accepting-what-the-radios-reject.md),
[ADR 0167](0167-a-claim-and-its-release-are-one-scope.md),
[ADR 0164](0164-the-on-path.md),
[ADR 0158](0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md)

## Context — the class, not the instance

ADR 0177 found `getattr(radio, "transmitting", False)` in the composition root, resolving the pause
predicate for a cadence that puts frames on the wire that is also this station's PTT line.
`Uvk5Radio` has no `transmitting` attribute, so it answered `False` — *"not keyed, safe to put a
frame on the wire"* — for ever. ADR 0177 measured what that wrong answer costs: one control exchange
in flight across the DTR assert and the witness's hardware carrier detect saw RF in **0 of 81
polls**. The station did not radiate at all, while `transmit()` returned normally.

Three findings already on the record are the same shape:

- **ADR 0164** — the ON capability was set only inside the method a route gated on it would call, so
  it could never be earned. *"Both pytest tests passed, because both reached the flag through the one
  call that cannot run in production."*
- **ADR 0167** — `test_relay_subscribers` matches **source text**, so a rename blinds a Part 97
  control while the suite stays green.
- **ADR 0172** — `MockRadio` accepted what every real backend rejects. *"Zero red is the shape of a
  blind spot, not a clean bill of health. If tightening a double changes nothing, the double was
  never asked the question."*

Each is a guard that reports safe because it never actually ran. This cycle audits the class, fixes
only what is provably live, and answers the structural question: is there a shape that makes this
unrepresentable?

**The deliverable is the map.** A big diff across this class is more dangerous than the class —
several of these sites sit on the keying path, and this arc has taken the Part 97 station ID down
twice there.

## The rubric

A duck-typed check is a **substitution point**: `getattr(x, "n", D)`, `hasattr(x, "n")`, or
`except: v = D` decides between *asking the object* and *using `D`*. Four questions, answered about
structure and never about current values:

1. **Reachable?** Can an object lacking `n` reach this site — measured against what can actually
   exist? (`REGISTRY` minus `SignaLinkV71`, whose `__init__` raises unconditionally; `_build_tuner`'s
   three tuner classes; `build_rx_gate`'s codomain; the settings a shipped `radio.toml` can hold.)
2. **Which way does `D` point?** Permissive — key up, put a frame on the wire, relay, trust — or
   conservative?
3. **Consumed?** A wrong default nobody reads is not live however wrong it is.
4. **Distinguishable?** Is there any counter, tri-state, log line or HTTP field that differs between
   "never ran" and "ran and said `D`"? **This is the criterion that does the work.**

**Harmless by construction** — (1) is NO *and you can name the enforcer*. This repo has exactly five,
and a verdict that cannot cite one is not this verdict:

1. an unconditional assignment before any reader;
2. a class that cannot be instantiated;
3. a closed factory whose whole codomain has the member;
4. a `runtime_checkable` Protocol that is *actually* `isinstance`-checked at runtime;
5. a test that goes red if the member disappears.

**A `Protocol` declaration alone is not an enforcer here.** `pyproject.toml` configures no mypy,
ruff or pyright, and there is no `.github`. Nothing type-checks this repo, so every `Protocol` is
documentation until (4) or (5) applies. That fact decides several verdicts below.

**Harmless only by accident** — (1) is yes, but (3) fails because an unrelated condition *in another
module* gates off the consumer. The operational test: *would a one-line change by an author who does
not know this site exists make it live?*

**Live** — all four: reachable on a config this project ships, permissive, consumed, and silent.

Two refinements the audit forced. **"Live" does not require a missing attribute** — `except
Exception: paused = False` is a substitution point with no name, and criterion 4 is what catches it.
And **ADR 0164's unearnable-capability shape qualifies on the same four criteria**, so it is audited
here rather than treated as a different family.

## The audit

**85 `getattr`/`hasattr`/`except AttributeError` sites in `radio_server/`.** 26 are not duck-typed
protocol seams and are excluded with reason, not silently: 13 `getattr(args, …)` on an argparse
`Namespace` (a missing flag means "not requested", which is a real answer); 4 `getattr(exc, "errno",
None)` normalising `OSError`; 4 on FastAPI `app.state`; 4 on the `logging` record factory, two of
which are deliberate sentinels; 4 two-argument `getattr` on dataclass fields, which **raise** and are
the correct shape; and one test seam on an injected `sounddevice`.

The remaining 59 are the audit. Grouped by verdict; every row names its enforcer or its consumer.

### Live (5)

| # | site | why it qualifies |
|---|---|---|
| L1 | `activity/gate.py` `_poll_once` + `rx/pump.py` `_start_gate` | **The worst thing in the audit.** `_poll_once` catches everything to `_busy = False`; `_start_gate` swallows a failed `start()` with a bare `except: pass`. A gate whose thread never spawned, or whose every tick raises, answers "channel clear" for ever and `RxPump` drops **every** received frame — no browser audio, no recording, no Mumble or D-STAR relay, no RX-activity edge, no scan dwell. Reachable on any `squelch = "cat"` station; the bench box's own `[uvk5]` block sets exactly that, one `POST /radio/select` away. And `PolledGate` is the only one of the three background pollers with no `stats()`, no denominator, and no staleness expiry. |
| L2 | `api/app.py` the ADR 0177 warning | The guard for this entire class **had no test** — a grep of `tests/` for its message text returned nothing — and ran once in the `create_app` body against the radio it was handed. `POST /radio/select` rebinds `radio` and never re-asked. ADR 0167's shape, one level out. |
| L3 | `api/app.py` `POST /diagnostics/reconnect` | Returned 501 *"the uvk5 backend has no serial transport to reconnect — there is nothing here that can be in the state this route repairs"*. `Uvk5Transport` has `alive`, `reader_error` and `reconnect()`. Two different refusals collapsed into one sentence that is true of only one of them. |
| L4 | `uvk5/tuner.py` `SetVfoTuner.capabilities` docstring | Claimed a boot-only probe was avoided because it *"would leave a radio that was switched off at startup missing the capability for ever"*. That is now exactly what happens — see F2. A docstring that states the opposite of the code is what the next cycle trusts. |
| L5 | four capabilities with no consumer | `TRANSMIT`/`RECEIVE`/`PTT`/`STATUS` are produced by every backend and consumed **nowhere** — zero references outside `base.py`. Live by criterion 4 and *only* criterion 4: their off state is not merely indistinguishable from unimplemented, it is unreachable. Recorded, deliberately not fixed — see the traps. |

### Harmless only by accident (4)

| # | site | the accident |
|---|---|---|
| A1 | `api/app.py` `transmitting` fallback | Inert on four of five backends. Harmless **only** because `AiocBaofeng` is simultaneously the only backend with a `probe_broadcast_fm` and the only one with a pause predicate — a coincidence between two attribute sets in two files. One method on `Uvk5Radio` makes it live. |
| A2 | both cadences' `except Exception: paused = False` | A hook that raises every tick was byte-identical, in every counter and every log line, to a hook answering `False`: `skipped` stays 0 and `polls` climbs in both. Accident rather than live because both wired hooks today are bound methods reading a plain flag, which cannot raise. |
| A3 | `rx/pump.py` + `activity/gate.py` `detects_signal` → `True` | The trusting direction: an unknown gate is assumed signal-aware, and the pump then asserts `active`, which the Mumble bridge reads as "RF is busy". Safe only because `build_rx_gate`'s whole codomain declares it (enforcer 3). |
| A4 | `aioc_baofeng.py` `disarm_rescue` | A tuner with a `probe_broadcast_fm` and no `disarm_rescue` makes `broadcast_fm_rescues` count the operator's own OFF press — ADR 0164 finding 2 reintroduced. Impossible across the three real tuners only because the arming and the disarming live in the same class. |

### Harmless by construction (50), with the enforcer named

- **Enforcer 1 (unconditional assignment):** `aioc_baofeng.py`'s two `getattr(self, "_transport",
  None)` probes — `self._transport = None` is assigned in `__init__` before any reader. Dead, and
  the probe misleads a reader into thinking the field is optional.
- **Enforcer 2 (uninstantiable class):** the three `close` probes in `api/holder.py` and `tx/tot.py`.
  Their comment cites `SignaLinkV71` as the backend without a `close`; that class raises in
  `__init__` and can never exist, and all four constructible backends define `close`. The *typing*
  justification stands on its own — `close` is genuinely not on the `Radio` protocol.
- **Enforcer 3 (closed factory):** the tuner-field probes — `tx_ready_at`, `modulation`, `tx_ok`,
  `broadcast_fm`, `reassert`, `read_rssi`, `persist`, `store`, `probe_broadcast_fm` — plus
  `kv4p/radio.py`'s `hasattr(transport, "alive")` and `_serial_port`, and `link/manager.py`'s
  `tx_stats` (whose own comment admits it is test-only).
- **Enforcer 4 + 5 (checked protocol, and a test that bites):** `aioc_baofeng.py`'s `volatile`. This
  one **corrected the plan that went in.** The plan recorded a closed factory as its only protection;
  in fact `volatile` is declared on the `runtime_checkable` `Uvk5Tuner`, that protocol *is*
  `isinstance`-checked in `_assert_boot_broadcast_fm`, and
  `test_every_production_tuner_satisfies_the_protocol` already pins all three tuners. Verified on
  this interpreter that the pin actually bites: a tuner missing **only** `volatile` fails the
  `isinstance`. So the keying-path default that looked least protected turned out to be the best
  protected — which is the rubric working, and the reason a verdict must cite an enforcer instead of
  asserting a vibe.
- **Enforcer 5 (a test goes red):** `logsafe.py`'s `getattr(secrets, "items", None)`, whose `()`
  fallback would silently disarm all value-based secret redaction. `Secrets.items` exists, and
  `tests/test_log_redaction.py` builds a real `Secrets` and asserts the token does not survive
  `redact()` — rename the method and that test fails.
- **Correct by design, cited as the house idiom:** `broadcast_fm_poll.py`'s five lifecycle probes on
  an argument that is *deliberately* a bare callable (ADR 0162), with `NO_CADENCE` as a **named**
  "there is no mechanism here" sentinel; `services/local.py`, which skips a module with no `PLUGIN`
  and then **fails loud** on a malformed one; `refuse_if_deafened`, where `None` never refuses and
  the docstring says why.

### The property trap is empty, not absent

A three-argument `getattr` cannot distinguish "the class has no such member" from "a `@property`
body raised `AttributeError`". Every probed name in this audit is either a method — where `getattr`
guards the lookup only — or a plainly-assigned instance attribute, so the trap is empty today. It
opens the day one of these names becomes computed. Recorded because "currently empty" is a different
claim from "cannot happen", and only one of them is true here.

## The structural question

**There is no global fix, and the reason is specific rather than a shrug.** The class requires
intercepting the *absence* of a member. Python offers three interception points; this tree forecloses
all three.

### 1. `isinstance` against a Protocol is **inoperative at this composition root**

Measured, not argued. On this interpreter (CPython 3.12.13):

```
bare  MockRadio vs Radio  : True
wrapped TotRadio vs Radio : False
TotRadio own members      : ['_arm','_disarm','_fire','close','ptt','set_on_timeout','tot','transmit']
inspect.getattr_static(t, 'status') -> AttributeError
```

`MockRadio` fully satisfies `Radio`. Wrapped, it does not. `_ProtocolMeta.__instancecheck__` resolves
members with `inspect.getattr_static`, which by design does **not** invoke `__getattr__` — and
`TotRadio` (`api/holder.py` wraps **every** production radio) forwards `receive`, `status`,
`capabilities` and every optional member through exactly that.

So a conformance guard at the composition root would report all four backends non-conforming,
unanimously and wrongly, and be silenced or deleted within a cycle. This kills the option whether the
protocol is fat (`Radio`) or narrow (a `PausableCadence`); narrow protocols do dodge ADR 0158's
all-or-nothing objection, but they do not dodge `getattr_static`.

The asymmetry that explains why the prior art works: **ADR 0158's guard checks the tuner, and tuners
are not wrapped.** The prior art does not generalise because the object it checks is not the object
this bug lives on. Nothing in this repo has ever protocol-checked a wrapped radio, which is why this
was never noticed.

And even where a protocol check *does* work, declaring a member does not stop the defaulted read —
`volatile` is declared, checked, and still read through `getattr(tuner, "volatile", False)`. ADR 0158
said *"declaring a member does not make conformance structural; checking it does"*. This cycle adds
that **checking is per-site**, and the defaulted read is what survives.

### 2. Static typing forecloses itself, twice

There is no checker: no `[tool.mypy]`/`[tool.ruff]`/`[tool.pyright]` in `pyproject.toml`, no
`.github`, no `Makefile`, no `tox.ini`. And it would not have caught it anyway —
`getattr(radio, "transmitting", False)` types as `bool` under mypy or pyright at any strictness.
Being legal on an object lacking the attribute is the *entire purpose* of the three-argument form.

### 3. `__getattr__` interception is already taken

`TotRadio` owns that slot, on the keying path.

### The answer

**Defensive `getattr` is the right call here** — not because there are four backends of differing
capability (that argues for tri-states, which this repo already has in `RadioStatus`, `WireStats`,
`BroadcastFm` and `deafened_unknown`), but because the alternatives are **measurably unavailable in
this specific tree**.

And "explicit rather than defaulted" is not a discipline to adopt — **it is one the repo already
keeps almost everywhere.** Of 85 probe sites, only **four** let a *value* default stand in for a real
answer against a duck-typed radio-like seam. Every other radio or tuner probe already defaults to
`None` and branches on it. What was missing was the rule written down and something that re-checks it.

A capability registry threading a tri-state `Unwired`/`Present` through ~20 call sites was rejected:
it relocates the judgement rather than eliminating it — the author still has to write the right thing
in the `Unwired` branch — and its expensive half lands on the keying path for no measured benefit.

## Decision

Fix the five live items. Ship two instruments. Carry the rest named.

**Nothing here touches `_key_on`, `_reassert_channel`, `_clear_if_deafened`, `ptt()`, `transmit()`,
or any file under `radio_server/tx/`, and no keying-path timing changes.** That is checkable from the
diff alone. There is no `docs/api.md` change and no `test_docs_contract` delta, deliberately: an
instrument that grows an HTTP surface is a different cycle.

1. **One resolver for the pause source** (`_pause_source`), plus
   `_warn_if_the_cadence_cannot_tell_the_station_is_keyed`, which logs *and returns* its message so
   the property is assertable without `caplog`. Called at `create_app` **and again after
   `POST /radio/select`**. Before this, the warning and `_station_wants_a_quiet_wire` each ran their
   own `getattr` chain a few lines apart — two answers to one question, itself a latent instance of
   this class. The cadence closure always followed the swap correctly; only the diagnostic was
   frozen, so this is the diagnostic catching up with the behaviour.

2. **`PolledGate` and `_start_gate` become readable — log lines only.** Three reasons, in order: the
   failure is invisible today and a log line fixes that with zero new surface; counters that reach no
   endpoint would repeat the sin this cycle names against itself (see F2); and a large RX-path
   behaviour change is the wrong shape for an audit cycle. Logged on **transitions**, not ticks — a
   5 Hz poller logging each failure is 432 000 lines a day, and a gate that fails, recovers and fails
   again is three facts, so first-only would make the second failure silent.

   **This closes the event and leaves the standing state uncovered, and nobody should read this
   cycle as having closed `PolledGate`.** There is still no staleness expiry: a gate whose poller
   thread died holds `_busy = False` indefinitely, `RxPump.active` never goes true, the relay's
   `dropped_rx_active` never moves, nothing ages out — so to anyone looking an hour later it is
   indistinguishable from a quiet band, and no log line reaches that person. The successor's shape
   already exists in the same package: `RssiPoller.reading()` returns `None` once no fresh
   measurement has landed inside `STALE_AFTER = 3.0` intervals, whose own docstring gives the reason
   — *"rather than showing the last thing it ever saw for the life of the process"*. Carried as F1.

3. **`pause_errors` on both cadences**, `null` where no hook is wired. Named for what it counts:
   **a nonzero value means the guard is broken and the cadence is reaching the wire unguarded — it
   does NOT mean any transmission was damaged.** Only an RF measurement at a witness says that; the
   counters that speak to key-ups are `WireStats`'. Both copies are kept honest by one parametrised
   test, because a guard fixed in one cadence and not the other is precisely what ADR 0176 found in
   ADR 0175's work. Classified above as *accident*, not live, and fixed anyway because it is three
   lines of pure observability on a hook this arc has already re-pointed twice.

4. **The reconnect route stops asserting a falsehood.** The two refusals are now separate and each
   says which one it is. Arming the uvk5 seam is a different cycle — it changes what `/healthz`
   returns and therefore what `acceptance.py`'s readiness check sees.

5. **The lying docstring is corrected** and the boot-assert log now names the remedy.

### Two instruments, because prose does not re-check itself

**`tests/test_defaulted_probes.py`** — a source scan pinning every probe whose default *is an answer*,
each row a **sentence** of why it is allowed and what its enforcer is. **Its first act was to catch
the cycle that wrote it:** extracting `_pause_source` turned `getattr(radio, "transmitting", False)`
into a `None` default with an explicit unknown branch, and the test went red on the count until the
row was removed. Four sites became three. **The residual is stated in the file:** a source scan
matches text, so a rename blinds it (ADR 0167) — it is a guard, not a proof.

**`tests/test_cadence_pause_wiring.py`** — a cross-backend test asserting the **consequence** ("does
the composition root warn about this radio") rather than an attribute list, so a rename follows the
resolver instead of blinding the guard. It reads the registry rather than restating it, so adding a
backend fails with *"you have not written a reason"*. It is **green today by design**: ADR 0177
recorded "harmless today only because that backend has no `probe_broadcast_fm`" in a HANDOFF
paragraph, and a paragraph is true on the day it is written and nothing re-checks it. This goes red
the day it stops being true. Paired with a negative double, so a resolver that never warns fails one
test and a resolver that always warns fails the other — neither failure can hide behind the other.

## Traps — rejected, with reasons

- **Flip `transmitting`'s default to `True`.** "Unknown means stay off the wire" sounds obviously
  right. On a future backend with a probe and no predicate the cadence would never poll, `deafened`
  would stay `null`, the ADR 0162 relay mute would never arm, and a deaf station would relay. It
  trades a wire-contention risk for a Part 97 hearing risk, silently, and would read as a safety
  improvement.
- **Widen `Uvk5Tuner` to declare `probe_broadcast_fm` / `disarm_rescue` / `set_broadcast_fm`.** The
  most dangerous edit in this audit, and it looks like a documentation change. The protocol is
  `runtime_checkable` and *is* `isinstance`-checked; `EepromTuner` implements none of the three.
  Widening it makes `EepromTuner` fail that check, the boot assert returns early, and **every eeprom
  and hybrid-persist station silently stops clearing broadcast FM at boot** — ADR 0157's entire
  defect, reintroduced by a type annotation.
- **Gate `/transmit` or `/ptt` on `Capability.TRANSMIT`/`PTT` (L5).** All four come from one shared
  frozenset every backend returns, so the gate could refuse nothing today — zero benefit — while
  creating a new refusal arm on the keying path for tomorrow. They are a vocabulary, not a gate.
- **Give `Uvk5Radio` `transport_health()` / `reconnect_transport()`.** Right, and not this cycle: it
  flips `/healthz` from always-200 to 503-on-dead-reader on a shipped backend and arms
  `TransportWatcher`.
- **Add a `paused=` hook to `PolledGate`.** Not needed — `Uvk5Radio.status()` already short-circuits
  its register read under `if not self._keyed`, so the hazard is closed one layer down. It would be
  new state on a 5 Hz path and it would change gate timing around a key-up.
- **Add a lock for the new `PolledGate` state.** `_busy`'s "a bool read/write is atomic under the
  GIL — no lock" is a deliberate contract on an object shared between a 5 Hz poller and a 50 Hz
  audio-path `__call__`. `_failing` is written only on the poller thread, exactly like `_busy`.

## Evidence

**Red run first: 20 failed / 17 passed**, plus `test_cadence_pause_wiring.py` red at collection.
Two of the passes were the paired negatives, vacuously green by design.

**One test passed for the wrong reason and was fixed before it could lie.** The
broken-hook-vs-working-hook comparison asserts that two `stats()` dicts are equal on master. It
passed immediately — because `age_s` is a live `time.monotonic()` delta and the two dicts differed by
microseconds. Freezing the clock made it fail properly. A test that compares two dicts must be shown
to be comparing them.

**Both new instruments were proved capable of firing**, not merely observed green: removing the
second `_warn_if_…` call site makes the swap test fail; the source scan finds the three real sites
with the three real defaults, matches a hypothetical fourth, and leaves the `None`-defaulted house
idiom alone.

**pytest 2384 passed / 5 skipped**, from 2351/5. **vitest 14 files / 155 tests**, untouched.

### Bench — the station returned to master and confirmed

Deployed commit **before**: `19cf0f5` on branch `adr-0177-the-key-up-race-measured`, **0 ahead /
2 behind** `origin/master`, clean. ADR 0177 left it there because master carried the TX fault at the
time. `./update-radio-server.sh` run bare — no refusal, because the branch is now an ancestor of
master, which is that guard's designed "put the box back on the mainline once the PR merges" path.
**After: `54f9e56`, `0 0` against `origin/master`**, new bundle `index-CUx4HnZr.js` served.

The **witness** checkout is `a6a4cd4`, 0 ahead / 42 behind, and **dirty** (`audio/dtmf.py`,
`link/entries.py`, `update-radio-server.sh`, plus an untracked `tls/`). It was **not moved**. The
instrument is therefore not `origin/master`, which is also why the `kv4p /healthz` 404 persists.

**ADR 0177's control arm, re-run on master** — and it reproduces the branch:

| arm | 1000 Hz | audio | witness carrier |
|---|---|---|---|
| ADR 0177, on its branch | 0.989 | 4.42–4.52 s | 48 / 83 |
| **this cycle, on master** | **0.989** | **4.52–4.62 s** | **45 / 81** |
| cadence arm, on master | 0.989 | 4.43–4.52 s | 46 / 82 |

438074 B is the exact top of ADR 0177's recorded 434852–438074 range. The cadence arm reports **0 of
3 polls reached the wire** with all three logged `SKIPPED (paused)`, and the reservation cost
**0.01–0.02 ms** of key-up latency — so ADR 0177's fix is live in deployed master, not just on the
branch it was measured on. `WireStats` climbed `key_ups` 2→3→4 across the trials with every race
counter at 0, which is the denominator doing its job.

**`acceptance.py`: 9/10 PASS**, `split-minus` SKIP for the missing fixture preset, `web` FAIL. The
`web` stage was re-run alone and fails on **exactly one** check — the known `kv4p GET /healthz → 404`
— with every other check passing, including `radio GET /healthz 200` and `radio serial reader alive`.
`tx` 1000 Hz 0.994 / duty 100.6 %, `services` 5.5 s / speech band 0.98, `split` 0.963.

**Station restored and verified**: 145.145 / TX 144.545 / 107.2 / FM / **low**, backend `baofeng`,
rssi 158, transport alive, `tx_ok: true`, `broadcast_fm.on: false`, links and D-STAR disconnected.
`uvk5_tune_persist` **reported as found (`true`), not flipped**; 45 EEPROM writes this session.

**A restart blanks the server's reported tuning, and that is the tri-state working.** After
`systemctl restart`, `/status` reports `frequency: null`, not a remembered value — ADR 0155's *"a
reconnecting host asserts; it does not assume"*. So "restored" was verified by read-back after
setting, and the null after a restart is recorded here so a later cycle does not read it as a
regression.

**A config claim in the plan was wrong and is corrected here.** The plan asserted `squelch = "cat"`
puts a `PolledGate` on the deployed station. It does not: the bench's `[baofeng]` block sets
`squelch_mode = "audio"`, and the `"cat"` setting is in the `[uvk5]` block. `PolledGate` is live on
any uvk5-backed station — including this box, one `POST /radio/select` away — but not on the station
as currently deployed. L1's severity stands; its reachability claim is narrower than the plan said.

## Findings — recorded, not fixed

**F1. `PolledGate` has no staleness expiry.** The log lines added here cover the event; the standing
state stays uncovered, and an hour later a dead gate reads exactly like a quiet band. `RssiPoller`'s
`STALE_AFTER` expiry is the successor's shape. **This cycle did not close `PolledGate`.**

**F2. Three instruments measure things nobody can see.** `RssiPoller.stats()` has no production
caller at all; the ADR 0177 `WireStats` counters reach `status()` but no operational reader; and both
bridges compute `skipped` and `polls` from `cadence_stats` and forward neither, so ADR 0176's entire
deliverable is dropped on the floor. That wants one small cycle of its own — **not another counter**,
which is exactly why `pause_errors` ships with a log line beside it and `PolledGate` got no counter.

**F3. uvk5 transport seams.** `Uvk5Radio` exposes neither `transport_health()` nor
`reconnect_transport()` although `Uvk5Transport` has `alive`, `reader_error` and `reconnect()`, so
`TransportWatcher` never fires and `/healthz` cannot 503 on a shipped backend — ADR 0166's mechanism
is off there. Only the 501's false sentence was corrected.

**F4. The circular broadcast-FM earn.** Every runtime sender of `0x0879`/`0x087A` is gated on the
capability its own reply would earn; the only ungated site is the constructor's boot assert. A radio
switched on *after* the server has both broadcast-FM capabilities unearnable until the backend is
rebuilt — which the boot-assert log now says, along with the remedy (`POST /radio/select` back to the
same backend). The mitigation is better than it looks: `broadcast_fm` renders `null`, not a confident
`false`. Making it earnable means an ungated frame on a wire shared with PTT and needs measuring.
*(On this run the radio was on at startup and both capabilities were earned — `/capabilities` lists
`clear_broadcast_fm` and `set_broadcast_fm`.)*

**F5. `skipped` still has no key-up denominator** — "the station never transmitted", "the hook is
wired and never fired" and "there is no hook" render alike. `pause_errors` separates only the fourth
case, a hook that raised.

**F6. `detects_signal` defaults trusting, twice**, and the safe direction would be the other one.

**F7. `disarm_rescue`** — ADR 0164 finding 2's shape, impossible today only because arming and
disarming live in the same class.

**F8. Dead probes that read as optionality** — `_transport` is assigned unconditionally; the `close`
probes cite a class that cannot be instantiated. Harmless, but they tell a reader a field is optional,
and that sentence is what a future cycle acts on.

**F9. The property trap is empty, not absent** — it opens the day a probed name becomes computed.

**F10. `TRANSMIT`/`RECEIVE`/`PTT`/`STATUS` are a vocabulary, not a gate** (L5).

**F11. Nothing type-checks this repo**, which is why every verdict above names its enforcer rather
than assuming one.

## Out of scope

The firmware fork, the witness checkout, EventHub's cap, and the ADR 0172 mock-audit items
(`set_tone`/`set_channel` permissiveness, `/channel`'s missing arm, the deliberately-permissive
`set_frequency`/`set_split`, `test_docs_contract`'s stated limitation). No UI. No keying-path timing
change of any kind.
