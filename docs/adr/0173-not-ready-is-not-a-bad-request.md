# ADR 0173 — Not ready is not a bad request

**Status:** Accepted · 2026-08-01 · closes the finding [ADR 0172](0172-the-mock-stops-accepting-what-the-radios-reject.md) opened on the bench, and settles for the host the question [ADR 0164](0164-the-on-path.md) had already settled for the radio

## Context

ADR 0172's bench run found a second refusal on `POST /mode` that had nothing to do with the bad word
it went looking for. On an **untuned** station — the state every service restart leaves behind —
`AiocBaofeng._stage` refuses, because every setter on that backend builds a whole channel and a
channel needs a receive frequency. That refusal was a `ValueError`, so ADR 0172's brand-new
`except ValueError → 422` arm caught it, and the 500 became a 422.

**The brief for this cycle says that refusal still reaches the client as a 500, and it does not.**
`origin/master` is `586a021`, the ADR 0172 merge (PR #232, merged 17:03 the same day); the arm caught
*both* cases from the moment it shipped. What it did was give the readiness refusal the **bad-value
code**, and that is the defect this cycle is actually about. Correcting the premise first, because
everything below follows from what was measured rather than from what was expected.

A 422 tells a client: *the body is wrong, change a value and send it again.* There is no value that
makes an untuned station tuned. `{"mode": "FM"}` is as valid as a mode gets.

And the project had already decided this, one layer down. ADR 0164 mapped the radio's own `ERR_OFF`
— a `tune` on a receiver that is off, a pure precondition — to **409**, in those words: *"a state
conflict, not a bad number."* So the host contradicted itself: **409 when the radio noticed the
precondition, 422 when the host noticed it.** A distinction about who was looking, not about what
was wrong.

## The blast radius, measured on the station before anything was deployed

The service was restarted to reach the untuned state — the ordinary path to it, not a contrived one
— and every route that touches the guard was probed on `db30fb9`:

| request, untuned station | master | this cycle |
|---|---|---|
| `POST /mode {"mode":"FM"}` | **422** `set a frequency before a split, tone or mode` | **409**, same sentence |
| `POST /mode {"mode":"nfm"}` | **422**, same | **409**, same |
| `POST /tone {"tone":100.0}` | **422**, same | **409**, same |
| `POST /split {"tx_hz":147555000}` | **422** `set a frequency before a split` | **409**, same sentence |
| `POST /mode {"mode":"AM"}` | **422** `mode must be FM or NFM, got 'AM'` | **422**, unchanged |
| `POST /modulation {"modulation":"FM"}` | **200** | **200** |
| `POST /power {"level":"high"}` | **200** | **200** |
| `POST /channel {"n":1}` | **501** `set_channel` | **501** |
| `POST /scan` | **501** `scan` | **501** |

`GET /status` was identical across every refusal, on both builds: `frequency`, `mode`, `tone`,
`tx_frequency` all `null`. The refusal happens before anything is staged, in the broken shape and
the fixed one alike, which is what made it safe to run against a live station.

**The two `200`s are as much of the map as the refusals, and both are deliberate.**
`set_modulation` is a one-shot frame that is not part of a channel, so it works before a frequency
has ever been set — ADR 0150 §7 argued that explicitly. `set_power` records a station-wide default
for the next tune, and its own comment names the refusal it is dodging. Reporting *why* a route is
exempt is the difference between a map and a list of failures.

`POST /ptt` was **not** probed, and that is a decision rather than an omission: the station runs the
`baofeng` backend, whose `ptt` has no frequency precondition at all, so the probe would have keyed a
transmitter to learn nothing.

### Two things the plan for this cycle got wrong, both corrected by measurement

1. **The uvk5 500s are latent, not live.** `Uvk5Radio` expresses the same condition as a
   `Uvk5KeyingError` — a `RuntimeError` no route catches — so `POST /split` and `POST /ptt` were
   predicted to be 500s. They are not. `Uvk5Radio.__init__` seeds `self._frequency` from registers
   `0x38`/`0x39` at connect, so it is `None` only for the handful of lines between the attribute's
   declaration and the register read. **Both guards are dead code.** Measured, not reasoned: a radio
   freshly built over the firmware fake reports `_frequency == 0`, and `POST /ptt` against it returns
   **200** — keying, with the backend's own log line warning that 0 Hz is the wrong band's
   calibration. They are converted anyway and pinned by a test that forces the state, with the
   deadness recorded; a guard that cannot fire today is exactly the kind that fires in three years
   when someone makes the boot probe lazy, and on that day it should already say what the fleet says.
2. **The claim that no test asserted on any of this was wrong.** It came from grepping for
   `set a frequency before`; the one test that exists matches on `set a frequency`. See *Collateral*.

## Decision

### 1. A new exception type — `RadioNotReady`, in `base.py`

The condition is not a value error and never was. `ValueError` was carrying it because Python had a
class lying around, and the status code was inherited from that accident.

Two negatives are load-bearing, and both are stated in the class's docstring because both break
silently if someone later "tidies" the hierarchy:

- **Not a `ValueError` subclass.** The per-route `except ValueError → 422` arms run *before* any
  app-wide handler, so a subclass would go straight back to wearing the value error's code — and
  those six arms would quietly stop meaning "the value is wrong".
- **Not a `RadioUnavailable` subclass.** That is a 503 the moment a route does not catch it: the
  accident of inheritance ADR 0164 introduced `RadioBusy` to escape. 503 sends someone to look at a
  cable; this radio is right there, listening.

It lives in `base.py` for `RadioBusy`'s reason: the API decides status codes and must not import a
particular backend's exception module to do it.

**409, and it shares the code with `RadioBusy` on purpose.** The two divide on what the caller does
next, which is the only thing a status code is for:

| | the body | what the client does |
|---|---|---|
| `422` | wrong | change a value, resend |
| `409` `RadioNotReady` | right | make a **different** call first, then resend unchanged. Never clears by itself |
| `409` `RadioBusy` | right | change nothing, **wait**, resend unchanged. Clears by itself |
| `503` | right | nothing helps; go look at the hardware |
| `501` | right | never going to work on this radio |

Both 409s say *resend unchanged*; they differ only in whether you must act first, and the `detail`
sentence names which. Inventing a fourth 4xx to encode that would be taxonomy this project does not
have, for a distinction the message already carries.

### 2. App-wide handlers, and the reason that is not a reversal of ADR 0172

`@app.exception_handler(RadioNotReady)` → 409, beside the existing `RadioUnavailable` → 503. **No
route body changed.** A route added tomorrow gets this right by writing no code at all.

ADR 0172 rejected an app-wide handler in the sharpest terms it had, so the difference has to be
stated rather than assumed. All four of its reasons were about `ValueError` being **Python's** class:
stdlib text like `invalid literal for int()` leaking into a body every other 422 fills with an
operator sentence; genuine 500s relabelled as the client's fault; `PATCH /settings`'s documented 400
split in two. None of them survives contact with a domain exception that nothing raises by accident
and that always carries a sentence written for an operator.

The fourth was the decisive one and deserves a real answer, not a dodge:

> A global handler does not make the next route remember; it makes the next route's forgetting
> invisible.

That holds precisely when a route **had something to do and skipped it** — a route that never looked
at a value still answering 422 is a lie about the route. Here a route has nothing to do. Whether this
radio has been tuned is the backend's state, and no route could check it without reimplementing the
backend's model. Nothing is masked, so nothing becomes untestable; the test moves to where the
knowledge lives, at the backend, which is where this cycle's backend-level pins are.

**ADR 0172 finding 4 survives literally unchanged, and is strengthened.** `POST /tuning/persist` and
the broadcast-FM `off` arm must still not grow a `ValueError` arm — both take a pydantic-validated
body with nothing a 422 could tell a client to change, and `off` is the always-available way *out* of
a bad state (ADR 0164), the worst possible place to relabel a server bug as the operator's fault.
This cycle adds **zero** `ValueError` arms, and leaves both routes better off: each now answers 409
correctly if a backend ever raises `RadioNotReady` there, still without gaining an arm. That
reasoning is written into the comment beside the new handler, where someone about to add one by
symmetry would actually read it.

### 3. The same omission-proofing for `RadioBusy`

`@app.exception_handler(RadioBusy)` → 409. `RadioBusy` is a `RadioUnavailable`, so without this it
was a **503 by accident of inheritance** everywhere except the two `/broadcast-fm` arms that catch it
by hand — the trap its own docstring names, still open two ADRs after being written down. Starlette
walks `type(exc).__mro__`, so the specific handler wins over `RadioUnavailable`'s, and the local arms
still win over both where a route wants to say more. Measured red: an uncaught `RadioBusy` returned
**503** before this handler and **409** after.

`UnsupportedCapability` deliberately does **not** get the same treatment. Its 501 body is a
different, machine-readable shape (`{"error": …, "capability": …}`) that the web client parses, and
nine hand-written arms would become dead code. That is a cycle, not a footnote.

### Rejected

| alternative | why not |
|---|---|
| **Keep one code for both** — leave it a 422 | It contradicts ADR 0164 for an identical condition one layer down, and 422's contract is "fix the body" when the body is already right. Its honest merit is below, under *What this does not do*. |
| **A route that inspects the exception** | It would have to match on message text — ADR 0133 already records how brittle exception/message coupling is — and be repeated in every route, reproducing the exact omission defect. It also puts backend vocabulary in the API layer, undoing the layering argument that put `RadioBusy` in `base.py`. |
| **`RadioNotReady(RadioBusy)`**, inheriting the 409 | Semantically false: `RadioBusy` promises the condition clears on its own, and a client that waits on this one waits forever. It would also drag `RadioUnavailable` back into the MRO. |
| **A new 4xx code** for precondition-not-met | 428 is about conditional requests; 424 is WebDAV. 409 is what this project already uses for state conflicts, and the `detail` carries the rest. |

## Collateral — one test, and what it was really asserting

With the change applied, the full suite failed **once**:
`tests/test_aioc_baofeng_tuning.py::test_a_setter_before_any_frequency_is_refused_not_guessed`.

```python
with pytest.raises(ValueError, match="set a frequency"):
    radio.set_tone(100.0)
assert tuner.applied == []
```

**What it was actually asserting is the line after the `raises`:** that a refused setter writes
*nothing* to the radio — it refuses rather than inventing a frequency to hang the tone on. That claim
is untouched. The exception class was scaffolding for the assertion that follows it, and it is now
load-bearing for a reason the test did not have when it was written: the class is what makes this a
409 rather than a 422. Fixed by changing the name in the `raises` and saying so in a comment.

Its existence also corrects this cycle's own claim that nothing tested any of this. Something did —
at the backend, for the refusal. **Nothing anywhere asserted what a client sees**, on any backend, at
any layer, which is how a readiness refusal wore a value error's status code across seventeen ADRs
without anyone noticing.

## What this does not do

**The browser cannot tell the difference.** `web/src/api.js` branches on 401, 501 and 503; 409 and
422 both fall through to a generic `ApiError` carrying `detail`, and the operator reads the same
sentence either way. The message was always right — only the code lied. So this is worth stating
plainly rather than overselling: the UI is unchanged, vitest is unchanged, and the value of the
change is for non-browser clients, for consistency with ADR 0164, and for the mechanism in decision 2
that makes the next route correct by default. A cycle whose visible effect is one number in a header
should say so.

## Acceptance

- **Red run, 5 failed / 3 passed.** `assert 422 == 409` three times (`/mode`, `/tone`, `/split`),
  once more for the no-movement check, and `assert 503 == 409` for the uncaught `RadioBusy`. The 3
  passes are pins: the `detail` sentence (always correct — only the code was wrong), the bad-value
  422 on a tuned station, and the value-before-readiness ordering.
- `uv run pytest` — **2294 passed, 5 skipped** (from 2283/5). `npx vitest run` — **14 files, 155
  tests**, unchanged, and no web file was touched.
- **Bench, on the station**: the table above. `422 → 409` on `/mode`, `/tone` and `/split`, with
  `AM` still a 422 and `GET /status` unmoved across every refusal.
- `scripts/bench/acceptance.py` — **exit 1**. 8 of 10 PASS (`systemd`, `presets`, `rx`, `dtmf`,
  `auth`, `tx`, `split`, `services`), `split-minus` SKIP for the missing bench preset, `web` FAIL.
  Re-run `--only web` **alone** rather than inferred from the summary: the single failing check is
  the witness's `kv4p GET /healthz → 404`, known and pre-existing. The station's own `/healthz` is
  200 and its serial reader is alive.
- **Station leave state, verified rather than assumed**: `frequency 147555000`, `mode FM`,
  `modulation FM`, `tx_frequency null`, `tone null`, `power high`, `tune_persist false`,
  `broadcast_fm.on false`, `tx_ok true`, `transmitting false`, reader running, unit `active`.

## Findings

1. **`tune_persist` off does not survive a restart, and the restore ritual has been hiding it.**
   `radio.toml:300` sets `uvk5_tune_persist = true`, so `POST /tuning/persist {"on":false}` is
   *runtime* state. Every cycle that ends by restoring persist-off and verifying it — as ADR 0172's
   did, correctly — is undone by the next `systemctl restart`, silently. Caught here only because
   this cycle had to restart the service to reach its subject, and then read the field. Either the
   config default is wrong or the restore is theatre; deciding which is a config cycle.
2. **`Uvk5Radio`'s two readiness guards are unreachable** — the constructor seeds `_frequency` from
   the radio's own VFO, so `None` is a state no constructed radio is ever in. Converted and pinned
   anyway. The interesting part is *why* nobody noticed the type inconsistency: dead code cannot
   diverge visibly.
3. **A radio that reports `0 Hz` for its VFO passes the readiness guard and keys.** Falls out of
   finding 2: `_frequency is None` is not the same test as "this radio is on a usable frequency", and
   the backend's own log line says so at key-up — *"transmitting on 0 Hz … radiated power is not
   characterised."* Only reachable behind a fake today. Worth its own look, because the guard that
   exists is not the guard the comment describes.
4. **The three mid-TX `RuntimeError`s are the new handler's first users.** `aioc_baofeng.py:610`
   (`set_tune_persist`), `:1139` (`commit_tuning`, reachable from `/frequency`, `/split`, `/tone`,
   `/mode`, `/power`) and `:1160` (`reboot_radio`) are all *"refusing to … while transmitting"* —
   textbook `RadioBusy`, currently 500s, mostly pre-empted by route-level arbiter checks. One line
   each now that the handler exists. Held back deliberately: busy and not-ready are different claims
   and one taxonomy decision per cycle.
5. **`Uvk5KeyingError` still carries two things that are not keying faults.** `radio.py:717`
   (`tx_allowed` false) is a deployment policy that never clears — closer to a retracted capability
   than a conflict — and `:781` (no TX confirm, with `kv4p/radio.py:470`) is a hardware fault that
   wants `RadioUnavailable` and a 503. Both are 500s on `POST /ptt`, which has no exception handling
   at all. Each needs a different code, so each is a decision.
6. **`MockRadio` still models no ordering constraint, and that is not simply the next chore.** ADR
   0172 finding 1 said the mock could not have found this; this cycle answers that by driving *real
   backends over fake serials* behind the real app, which is a stronger instrument than teaching the
   double. Encoding a rule would mean choosing one: `AiocBaofeng` refuses mode/tone/split untuned,
   `Uvk5Radio` only split and keying (unreachably), `Kv4pHt` never. That is the same intersection
   decision as `set_tone`, not an oversight.
7. **`test_docs_contract.py` still cannot see any of this.** It checks that paths and capability
   strings appear in `api.md`, not that the prose is true; the status-code rows said `/mode` 422s on
   a bad word while the station 422'd a good one. Same limitation ADR 0172 recorded as its finding 6,
   restated because this cycle's doc edits are again a review item rather than a gated one.

## Out of scope

The fork; the witness checkout; capping the `EventHub` queue; `modulation` on `GET /presets`;
`set_tone` and `set_channel` from ADR 0172's mock audit.
