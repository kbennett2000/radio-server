# 0158 — The host refuses to key a station that cannot hear itself

Status: Accepted
Date: 2026-07-31

## Context

[ADR 0156](0156-a-deaf-station-can-still-transmit.md) established the hazard and
[ADR 0157](0157-a-station-that-cannot-hear-itself.md) built the instrument. This cycle spends it.

A UV-K5 carries a **second receiver** — a BK1080 commercial-FM chip, 64–108 MHz — beside the BK4819
every other dock opcode drives. While it runs it holds the speaker line the AIOC listens on, so the
station hears **nothing** on its own channel. And `RADIO_PrepareTX` has no `gFmRadioMode` term: the
only `gFmRadioMode` reference in the whole of `App/radio.c` is the VOX gate at `:897`. So the radio
transmits normally throughout — including the automatic station ID that Part 97 makes required
controller behaviour rather than a feature (guardrail 5), going out into a channel nobody is
monitoring.

ADR 0157 gave the server an OFF path and a tri-state `broadcast_fm` status block, and deliberately
**no ON path**, on the grounds that until an interlock exists the ON path cannot ship. This cycle
builds the host half of that interlock. There is still no ON path.

### Correcting the record: F8 is merged, and ADR 0157 says otherwise

ADR 0157 states that fork PR #6 is open, that F8 is unmerged, and that "no radio in existence runs
F8". **All three are now false, and the last one was already at risk when it was written.**

| claim | verified for this cycle |
|---|---|
| fork `origin/main` | **`d086a23`** — "Merge pull request #6 from kbennett2000/f8-dock-broadcast-fm" |
| `git merge-base --is-ancestor 5f4c581 origin/main` | **YES** — F8 is an ancestor of `main` |
| `0x0879` in `main` history / `DOCK_CMD_SET_FM` in its tree | present |
| PR #6 | **MERGED `2026-07-31T15:38:12Z`** |

PR #6 merged roughly 75 minutes before PR #214 (ADR 0157) landed at `16:53:07Z`, so ADR 0157's
verification was true when it was run and stale by the time it was merged. This is exactly the
failure mode CLAUDE.md guardrail 1 warns about, arriving from the other direction: not a fact
asserted from memory, but a *verified* fact that expired. The correction matters because ADR 0157
leaned on "no radio runs F8" to argue that `CLEAR_BROADCAST_FM` had to be earned rather than
declared. **That decision is unaffected** — earning it is right whether or not any radio has the
firmware — but the supporting sentence must not be quoted forward.

Two consequences for this ADR:

- `0x0879` is now a command some radios genuinely answer, so the `broadcast_fm` block can hold a
  real measurement rather than being structurally `None` everywhere. The interlock is live, not
  latent.
- The firmware successor below is **unblocked**, not blocked. It was recorded as waiting on PR #6.

**No bench result is claimed here.** Whether any particular radio answers `0x0879` is a hardware
fact this cycle did not measure, and guardrail 1 forbids asserting it. Nothing was flashed.

## Decision

### 1. One predicate, in `base.py`, and the shared helper *is* the deliverable

`refuse_if_deafened(broadcast_fm: BroadcastFm | None)` lives in `radio_server/backends/base.py`
beside `BroadcastFm`. `AiocBaofeng` and `MockRadio` both call it. The two call sites are not the
point — the single implementation is, because the *message* is what this cycle ships, and two copies
of a message is precisely how two distinct causes converge into one undiagnosable "cannot transmit":
the failure being prevented, reintroduced by the fix.

Why this rule is a free function while `_refuse_if_tx_disabled` is a backend method. They look alike
and differ in kind:

- `tx_ok=False` is the **radio** refusing. The host raise is a *prediction about hardware*, and about
  *which* hardware: `VFO_STATE_TX_DISABLE` stops the PTT **pin** the AIOC drives and does nothing to
  the `uvk5` dock's register keying. Same radio state, different consequence per backend — a
  per-backend rule belongs in a backend.
- `broadcast_fm.on=True` is **not** the radio refusing. The radio transmits perfectly happily; that
  is the entire fault. The refusal is **host policy**, and the policy is identical everywhere. On a
  backend with no second receiver the block is permanently `None` and the predicate is a no-op, so
  being universal costs nothing.

Being a free function also makes the tri-state rule pinnable in three assertions with no radio, no
serial fake and no audio fake — strictly better evidence than the `tx_ok` rule has today.

### 2. Only a definitive `on=True` refuses

```python
if broadcast_fm is None or not broadcast_fm.on:
    return
```

A `null` block is "nobody has asked this radio". An unmeasured field must never lock a transmitter —
the `tx_ok` rule, for the same reason: a station that would not transmit until someone had measured
is a worse failure than the one being prevented. Both directions are pinned, at the predicate and
again at the backend, the API, the bridges and the browser.

Because `BroadcastFm` is a tri-state *block*, "unknown" and "verified off" arrive here already
distinguished. This predicate spends what ADR 0157 bought rather than trying to recover a
distinction the type threw away.

### 3. Placement: first in `_key_on`, ahead of the channel re-assert

```python
self._refuse_if_deafened()      # new
self._reassert_channel()
self._refuse_if_tx_disabled()
self._await_tx_lockout()
```

Three arguments, and the third is the one that decides it:

- **Cost.** It is strictly cheaper than the step it displaces. `_reassert_channel` sends `0x0877`
  and `0x0873` and waits for `0x0874`; this is one attribute read with no I/O. ADR 0145's rule —
  refuse before anything has been opened or waited on — reaches one step further here, to before
  anything has been *sent*.
- **Severity, and it is not a fresh judgement.** `AiocBaofeng.__init__` already argued this ordering
  for the two boot asserts and a test already pins it. Inverting it in the key path would be an
  unexplained disagreement with the boot path about which fault is worse.
- **Maskability.** `_reassert_channel` can raise `TuneError` of its own. Refusing after it would let
  an unrelated tune failure hand the operator a message about the channel on a station whose real
  problem is that it cannot hear at all. Refusing first guarantees the worse fault always wins the
  report — the same reasoning as ADR 0153's separate `try/except` per boot assert.

The cost is real and accepted: a refused key-up does not re-assert the modulation, so `tx_ok` on a
deaf station goes staler than on a hearing one. Nothing keys, so nothing unsafe follows.

The gate fires **once per key-up, not per frame**: streaming `transmit()` returns early when already
keyed and never re-enters `_key_on`.

### 4. The message is diagnosably distinct, and names a remedy that actually works

> the radio's second receiver is running (tuned to 103.2 MHz) — it holds the speaker line, so this
> station hears NOTHING on its own channel and would transmit into it anyway, station ID included.
> Clear broadcast FM on the radio (press EXIT, or power-cycle it) AND restart the server: this
> server records the second receiver once, at startup, and never re-reads it, so it cannot see the
> radio recover on its own.

No clause, identifier or remedy is shared with the AM refusal. An operator reading a 503 body, a
`tx_failed` event, or a throttled bridge log line can tell them apart on the first clause. `hz` is
rendered exactly as `SetVfoTuner.clear_broadcast_fm`'s boot warning renders it, so the two name the
same station the same way; a blanked `hz` degrades to "on an unreported frequency", reusing the
phrase already in the tuner rather than inventing a second.

**The "AND" is load-bearing.** Pressing EXIT alone does not clear the refusal — see 5.

### 5. This interlock latches, and that is a real behaviour change

ADR 0157 R1 worried that a host gate *fails open*. This one fails **closed and stays closed**.
`tuner.broadcast_fm` is written only by `SetVfoTuner.clear_broadcast_fm`, called only from
`_assert_boot_broadcast_fm`, called only from `__init__`. Nothing re-reads it. So once the block
latches `on=True`, an operator who clears broadcast FM at the radio has a station that hears fine
and a server that refuses every key-up until the process restarts (or `POST /radio/select` rebuilds
the backend).

This is the first refusal in this API an operator cannot clear from the radio's front panel: the AM
refusal clears the instant someone POSTs `/modulation FM`. It is the right trade — a deaf
transmitting station is worse than a restart — but it is a behaviour change, it is stated in the
refusal message itself, it is documented in `docs/api.md`, and it is the strongest argument for the
successor in R2.

### 6. `MockRadio` enforces the same predicate

`MockRadio.ptt(True)` and `MockRadio.transmit()` call it; `ptt(False)` does not, because refusing to
*stop* is the dangerous direction in a transmitter (ADR 0090/0093/0099). In `transmit()` the gate
goes **after** the format check, so `AudioFormatMismatch` keeps taking its own named path through the
bridges' ordered `except` ladders.

This looks like it contradicts `mock.py`'s stated reason for *not* enforcing the `tx_ok` refusal
("there is no radio here to decline"). It does not, and the docstring now says so: the AM refusal is
a prediction about a PTT pin this mock does not have, while this one is server policy about a state
this mock genuinely models. CLAUDE.md's mock-first rule is exactly that server policy must be
drivable without hardware — and `left_in_broadcast_fm` was added last cycle in those words, "so the
API and UI cycles that follow can drive the dangerous case without hardware."

It is also what makes the bridge and controller fail-first tests real. Every existing refusal harness
in this repo injects the refusal *at the double* by overriding `ptt`/`transmit` to raise; a test
built that way is red whether or not production code changed, and proves only that a bridge can catch
an exception someone else threw.

Blast radius: `_broadcast_fm` defaults to `None`, and `left_in_broadcast_fm=True` appeared nowhere in
the repo before this cycle. Only tests that opt in are affected.

### 7. Nothing else changed to carry the refusal

No new route, no new exception type, no new counter, no new event, no second refusal mechanism.
`RadioUnavailable` reaches the app-wide handler (503 with the reason), the two bridges' existing
`except RadioUnavailable` ladders (counted, throttled, over ended, loop alive), `Controller._keying`
(a `tx_failed` event carrying the reason), and `TxSession._key_up`'s unwind (arbiter released). That
those all worked untouched is asserted, not assumed — see Acceptance.

### 8. TalkControl gains a second cause, and its wording carries the limit

`state.broadcast_fm?.on === true`, mirroring `tx_ok === false`'s `===` for the same reason, inside
the existing load-bearing parentheses. Deafness outranks the demodulator in the label, matching the
backend's order and for the backend's reason. The Spacebar path consults it too — the keyboard is the
bypass `disabled` cannot cover, and unlike the six-second tune mute this cause never expires.

The sub-line states its **own limit**, in the operator's terms rather than only here:

> Note this button cannot see broadcast FM switched on at the radio's own keypad, so an enabled Talk
> button is not proof the radio is hearing.

ADR 0157 declined a UI on the grounds that the server never re-reads, so a status row would say "off"
while an operator's own keypress left the station deaf. That objection is correct and it is answered
by disclosure rather than by omission: a lockout that *fires* is strictly better than none, and the
one that does not fire must not read as a clean bill of health. A silent omission would have left the
same false impression with nothing on screen to correct it.

### 9. Finding 7: the recorded fix was already in the tree, and that is the finding

ADR 0157's finding 7 asked for `clear_broadcast_fm` to be declared on the `Uvk5Tuner` protocol "so
conformance is structural". It was **already declared** — both it and `broadcast_fm` — so that fix
was a no-op. The actual defect is that **nothing in the repo ever checked the protocol**: it is
`@runtime_checkable`, but there were zero `isinstance` calls and zero annotations using it, and
`AiocBaofeng` reads every tuner field through `getattr(..., default)`. Declaring a member does not
make conformance structural; checking it does.

`_assert_boot_broadcast_fm` now guards on `isinstance(tuner, Uvk5Tuner)`, after the capability gate
so it fires on exactly the coupling bug and not on every duck-typed tuner in the suite.

Verified empirically rather than cited: on this repo's **Python 3.12.13**, `isinstance` against this
`@runtime_checkable` protocol **does** check data members as well as methods — a fake carrying every
member except `clear_broadcast_fm` returns `False`.

**A skip lands exactly where a failure lands:** `broadcast_fm` stays `None`, because the server
genuinely does not know. Never "off" — that is an affirmative claim that the station can hear, and
the one wrong answer that matters here. There is no third outcome. The log level is **WARNING**,
against the INFO used for a silent radio, and the asymmetry is the point: a radio that does not
answer is an ordinary fact of life on older firmware, while a tuner advertising a capability it has
not implemented is a programming fault somebody has to fix. It names the type.

**Not applied to `_assert_boot_modulation`.** `Uvk5Tuner` is a fat protocol and `isinstance` is
all-or-nothing across ten members, so gating the ADR 0155 demodulator assert on it would let a
missing `clear_broadcast_fm` — a member with nothing whatever to do with the demodulator — silently
cost a station its demodulator assert. That is ADR 0153's "a failure of the first must not skip the
second" reappearing through a conformance check instead of a shared `try/except`. A test pins that
the demodulator assert still runs against a non-conforming tuner.

A conformance test covers all four shipped tuners as belt and braces. It is **green on master** and
is labelled a regression guard, not evidence.

### 10. `build_radio`, since ADR 0157 was asked and did not answer

The brief for ADR 0157 asked for the boot asserts to be evaluated for a move to `build_radio`
(`api/holder.py`), and asked that the conclusion be argued either way rather than left silent. **The
evaluation happened during that cycle and never reached the record** — ADR 0157 mentions
`build_radio` only in the unrelated context of moving a blocking call off the event loop. That is a
process failure worth naming: an argued decision that is not written down is indistinguishable from
one that was never made, which is why it is being asked for a second time.

The conclusion, recorded now: **the asserts stay in the backend constructor.** `build_radio`
constructs via `create_radio(backend, **backend_kwargs(settings, backend))` and returns a `TotRadio`
wrapper. `backend_kwargs` is derived from `Settings` and cannot express the `tuner=`,
`_serial_factory=` and `_audio=` DI seams every test in this area uses, so an assert placed there
would not run under test through the same path it runs in production — and it would newly run
against `MockRadio`, which has no tuner and no dock. The constructor is where the tuner exists and
where the failure is one clear traceback. This is now two cycles of cost for a question whose answer
is "no", so it is recorded here as settled rather than re-opened.

## Acceptance

Fail-first, with the weak evidence labelled weak and the vacuous passes named rather than counted.

**Red run 1 — 13 behavioural failures + 1 collection error**, across `tests/test_mock_radio.py`,
`test_aioc_baofeng_tuning.py`, `test_link_bridge.py`, `test_dstar_bridge.py`, `test_controller.py`,
`test_api.py`, `test_tx_audio.py` (13 failed, 303 passed, 1 error):

- 7 backend, including the one that reproduces finding 7 — a tuner advertising `SET_MODULATION`
  without `clear_broadcast_fm` raises `AttributeError` out of a **constructor**, caught by neither
  handler, taking the whole backend down at build.
- 1 Mumble bridge, 1 D-STAR bridge, 1 controller (the periodic station ID), 2 REST routes
  (`POST /ptt`, `POST /transmit`), 1 `TxSession`.
- 1 collection error — `refuse_if_deafened` not yet importable. **Weak evidence**: it proves a name
  is absent, not that behaviour is wrong. Labelled as such.

**Not evidence.** These passed on master and are regression guards: `broadcast_fm is None` does not
gate; a measured `on=False` does not gate; unkeying a deaf station is never refused; the controller
keys normally once the receiver is cleared; `GET /status` still reports the block; all four shipped
tuners satisfy `Uvk5Tuner`; a tuner that does not claim the capability is skipped silently.

**Browser.** 9 new vitest tests, of which **5 are red** without the JSX change (verified by
restoring the unmodified component and re-running) and **4 pass trivially** — the "does not lock out"
negatives. Only the 5 are counted as evidence.

**Green.** `uv run pytest` → **2043 passed, 5 skipped**, against a baseline of 2014 passed, 5 skipped
— **+29 tests, no regressions**. `npx vitest run` → **11 files, 110 tests**, against 11 files and 101
tests — **+9, no regressions**.

Branch cut fresh from `origin/master` `68b0e39` (PR #214 merged); `git merge-base --is-ancestor
origin/master HEAD` confirms nothing is stacked.

## Consequences

- A station whose second receiver is known to be running cannot transmit at all — no over, no
  bridge relay, no voice service, and no station ID. That is the intent: an ID transmitted into a
  channel the station cannot hear is not compliance, it is noise with a callsign on it.
- **The refusal latches until restart** (decision 5). New, real, and documented in three places.
- The gate is only as good as the block, and the block is a boot-time record. Broadcast FM switched
  on at the radio's own keypad after startup is invisible to it. This is the lead argument for the
  firmware successor below, and it is disclosed in the browser rather than hidden.
- One new obligation on tuner fakes, now enforced rather than remembered: a double that advertises
  `SET_MODULATION` must satisfy `Uvk5Tuner`, or its broadcast-FM assert is skipped with a warning
  naming it. Previously this was an `AttributeError` out of a constructor.
- `docs/api.md`'s prose remains unguarded by any test (ADR 0157 R6, unchanged). Every doc edit here
  was made and checked by hand; the contract test only verifies that each capability *string*
  appears somewhere in the file.

## Out of scope

**R1 — the firmware half, now unblocked, and the argument for it has changed.** A `gFmRadioMode`
term in `RADIO_PrepareTX`. The lead argument is no longer divergence-versus-correction: it is that
**the host gate cannot see the front panel.** The server never re-reads broadcast-FM state, so F+0 on
the radio leaves the station deaf with the browser showing a live Talk button and this gate silent —
and a host gate also fails open on a crash. Only a firmware term closes that, and it survives the
host being dead. Its costs stand: divergence from upstream, which [ADR 0148](0148-the-firmware-is-a-product-too.md)
treats as a real running cost, and a changed front-panel behaviour for an operator who currently
listens to broadcast FM and keys occasionally. On that last point this ADR takes a position —
**correction, not regression.** Transmitting while your receiver sits on a broadcast station is blind
transmit, and no amateur operating practice defends it. PR #6 has merged, so this is no longer
blocked on anything but a decision and a cycle.

**R2 — the pre-key-up re-clear, which is what un-latches decision 5.** ADR 0157 R2 noted that
`_reassert_channel` already re-asserts the *modulation* before every key-up while this state gets
only a boot snapshot. Two things to record precisely:

- Done naïvely it is a transmit outage, not a slowdown. `SetVfoTuner`'s timeout is **3.0 s**, the
  backend constructs it with the default, and the transport does not retransmit on this path. On any
  radio whose firmware lacks the command that is 3.0 s of dead air before every over, then a
  `TuneError` — which is a `RadioUnavailable`, so uncaught it refuses *every key-up on every
  station*.
- Gated on the **earned** `Capability.CLEAR_BROADCAST_FM` it is neither. The capability is granted
  only after a radio has actually answered `0x087A`, so on firmware that cannot answer the cost is
  one frozenset membership test — no frame, no wait — and on firmware that can it is one dock round
  trip that arms no transmit lockout. The circularity that forbids this gate at *boot* (the
  capability is earned by the frame it would gate) does not apply in the key path, which runs after
  the boot assert has already earned it or not.

That version makes `broadcast_fm` a measurement taken milliseconds before the line goes high instead
of a memory, which closes both the front-panel blind spot *and* the latch. It is the named successor.
Not built here because it belongs with a bench proof, and nothing has been flashed.

**R3 — the `uvk5` backend has the identical hazard and no gate.** It advertises neither
`SET_MODULATION` nor this capability, runs no boot assert, and holds full control (`0x0870`), which
per ADR 0156 makes `0x0879` answer `ERR_BUSY`. Its `broadcast_fm` is permanently `None`, so
`refuse_if_deafened` is a no-op there — honest, and structurally unfixable over this wire today.

**R4 — `AiocBaofeng` advertises `CLEAR_BROADCAST_FM` with no `Radio`-level method behind it.** The
tuner earns the capability and `capabilities()` is `SHARED_CAPS | tuner.capabilities()`, but the
backend exposes no `clear_broadcast_fm` while `MockRadio` does. Harmless today — no route calls it,
and `docs/api.md` documents the absence as deliberate — but it is the shape guardrail 3 forbids, and
it is also the missing piece of an in-band remedy for the latch. The route cycle should fix it rather
than discover it.

**R5 — firmware-version negotiation.** The empty-`0x0879` probe remains the real fix for paying a
round trip against firmware that cannot answer.

**R6 — the fork publishes no OFF vector.** `PROTOCOL.md` has four `0x0879`/`0x087A` vectors and none
is the frame this server sends; it was derived in ADR 0157 and labelled derived. Now that PR #6 has
merged, publishing it upstream is a small firmware-repo cycle.

**R7 — `docs/api.md`'s prose counts and prose claims are unguarded by any test.** Carried unchanged.

Everything carried by ADR 0157 stays carried.

## Source of truth

Fork `kbennett2000/uv-k1-k5v3-firmware-custom`, `origin/main` at **`d086a23`**, which merged F8 from
`5f4c581` via PR #6 on 2026-07-31. Firmware claims in this ADR were read from that history. No
firmware was written, built or flashed in this cycle, and no bench result is claimed.
