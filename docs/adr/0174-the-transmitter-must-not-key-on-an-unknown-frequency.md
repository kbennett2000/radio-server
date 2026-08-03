# ADR 0174 — The transmitter must not key on an unknown frequency

**Status:** Accepted · **Date:** 2026-08-03 · **Supersedes/depends on:** [ADR 0173](0173-not-ready-is-not-a-bad-request.md), [ADR 0132](0132-dock-band-and-the-register-model.md), [ADR 0133](0133-repeater-split-and-chirp-import.md), [ADR 0134](0134-repeater-keyup-in-the-field.md), [ADR 0112](0112-uvk5-radio-class-and-keying.md)

## Context

[ADR 0173](0173-not-ready-is-not-a-bad-request.md) went looking for two `500`s in `Uvk5Radio` and
found them dead. The reason it found is this ADR's subject: `Uvk5Radio.__init__` seeds
`self._frequency` from BK4819 registers 0x38/0x39 and **adopts whatever comes back**, so a register
file answering `0` produces a model saying *the frequency is 0 Hz*. That is a different statement
from *I do not know where this radio is*, and it is the only one of the two that gets past
`_key_on`'s `self._frequency is None` guard. Measured then: a fresh radio reported `0`, and
`POST /ptt` returned **200**.

0173 was right not to probe it. The behaviour of a BK4819 whose synthesiser is commanded to DC while
the PA rail is up is exactly the thing you cannot learn without radiating, and "key it and see" is
the wrong order for a question about an unlicensed emission.

### What the firmware settles, from source

In dock mode the **host** owns the synthesiser. `Dock_ForceTx` (`App/app/uart.c:778`) never calls
`BK4819_SetFrequency`; it reads `gCurrentVfo->pTX->Frequency` — the radio's own VFO — only to pick
the RX filter path and to set the PA up from flash calibration, and leaves 0x38/0x39 exactly where
the host put them. Its own comment says so. Nothing in the dock TX chain validates a frequency:
`dock.c:378` edge-detects the REG_30 TX-enable bit and drives the PA.

So with 0x38/0x39 at zero, a key-up commits: `BK4819_PickRXFilterPathBasedOnFrequency(0)` takes the
`< 28 MHz` branch; the host's own `_correct_tx_band` reads 0 as VHF (`_pa_gain_for`) and forces the
VHF LNA bit and gain byte; `Dock_ForceTx` raises the PA-enable GPIO and applies a bias calibrated
for a *different* frequency. **PA rail up, band path chosen, synthesiser at DC.** There is no
BK4819 datasheet in either tree and no divider setting for 0, so whether the PLL simply never locks
or the VCO sits at a rail is undocumented and unmeasured — and both outcomes are an emission nobody
chose. That uncertainty is the argument for never reaching the write. No radio was keyed.

The backend was not even silent about it. The red run's own log, from master:

```
WARNING uvk5: transmitting on 0 Hz but the radio's own VFO set the PA up for the other band
(reg 0x36=0x0000, gain 0x00); ... radiated power is not characterised.
```

`_correct_tx_band` printed **"transmitting on 0 Hz"** into the journal and keyed anyway. The
software knew. Nothing was listening, because knowing was not wired to refusing.

### Where it was live, and where it was not

| constructor | `frequency=` | does 0 survive? |
|---|---|---|
| `holder.py:75` → `backend_kwargs` — **the server** | `uvk5.frequency`, **REQUIRED** (`spec.py:964`) | **No.** `set_frequency` runs at construct and validates |
| `doctor.py:1346` → `_build_backend` — **`--key-test`, `--tx-tone`** | `cfg["frequency"]`, baseline **`None`** | **Yes** |
| `make_radio` in the suite (110 of 123 calls) | omitted | **Yes** |

`_uvk5_config` sets `"frequency": None` and then deliberately keeps that baseline when the REQUIRED
key raises on read — *"a REQUIRED-but-unset key raises on read — keep its baseline default"*
(`doctor.py:398`). That is the config state a first bring-up is in, which is what doctor exists for.
So the defect was live in two human-invocable RF modes, and **not** reachable through the server.
Saying that plainly matters more than claiming a bigger blast radius: the server was safe by
accident of a REQUIRED key, not by design, and the tool built for the moment before that key exists
is the one that keyed.

## Decision

**1. An illegitimate read never enters `_frequency`.** New `_seed_frequency()` beside
`_seed_reg30`/`_seed_reg33`: read 0x38/0x39, run the existing `_validate_frequency`, and return
`None` with a WARNING naming the raw register words when the read is not a frequency.

**2. Nothing else changes.** The guard was never missing — `_key_on` has raised on
`self._frequency is None` all along, and ADR 0173 already made it a `RadioNotReady` → **409**. The
model was lying to it. Fixing the lie makes *both* of 0173's converted guards live: `_key_on` and
`set_split`.

**3. `doctor`'s two keying modes report the refusal.** `_uvk5_keying_core` catches `RadioNotReady`
as a FAIL naming `uvk5.frequency` (it caught only `Uvk5KeyingError`); `_tx_tone` prints a refusal
and returns 1. A diagnostic that crashes tells the operator less than the wrong answer did.

### Why upstream rather than a precondition on `ptt`

| | a guard on `ptt` | validating the seed |
|---|---|---|
| what it must know | "is this a real frequency" — so it writes the validation anyway | the same validation, once |
| what it fixes | the key-up | the key-up, `set_split`, `_correct_tx_band`, `_restore_rx_frequency`, and `status()` |
| definitions of "a frequency" | two | one |
| what `/status` says | `frequency: 0`, for the life of the process | `frequency: null` |

The last row is the decisive one. A `ptt` guard would stop the transmission and leave the API
telling an operator the station is tuned to 0 Hz — the same lie one layer up, in the field a human
reads. And the repo had already written the rule down: `base.py:324`, *"0 Hz is not a frequency."*

The precedent is not stylistic, it is this backend's own scar tissue. `_seed_reg30` refuses to adopt
a word the radio cannot be receiving on and repairs it ([ADR 0132](0132-dock-band-and-the-register-model.md));
the split is never seeded at all, because *believe whatever you find* is a named fault class
([ADR 0133](0133-repeater-split-and-chirp-import.md)); `_pa` reports no reading as no reading
([ADR 0134](0134-repeater-keyup-in-the-field.md)). `_frequency` was the one seeded value that adopted
anything.

**What "unknown" means, and what it does not.** A *valid* read-back is the radio's own VFO and is
known enough to transmit on — the posture `_seed_reg30` already takes. The alternative considered
and rejected was to refuse until the host itself had written 0x38/0x39 this process: it closes the
ADR 0133 class completely, but it would refuse `doctor --key-test` on a perfectly healthy
unconfigured radio, on no evidence that anything is wrong with it. So a healthy radio is unchanged,
and the cost the brief anticipated — "changes what a fresh radio reports" — applies **only when the
read is not a frequency**.

`_validate_frequency` rather than `== 0`, so there is one definition. Its raster half cannot fail
here (the registers *are* in 10 Hz units, so a read is always on the raster); the range half is the
one with teeth, and it catches a garbled read-back as well as a bare zero. Not a raise: failing
construction would turn a repairable state into no radio at all, and `uvk5.frequency` is REQUIRED,
so the server's own `set_frequency` lands a few lines later. Refusing to *transmit* until then is
the whole of the safety claim.

## `AiocBaofeng` — the same state, and not the same hole

The brief asked whether the aioc backend has this by another route, and said to say so loudly if it
does. It has the *state* and not the *defect*, and the deployed station is the proof either way.

`AiocBaofeng._key_on` refuses for deafness, channel re-assert, AM and TX lockout — **never** for
"no frequency"; its sequencing guard covers `set_mode`/`set_tone`/`set_split` only. Measured on the
station immediately after this branch was deployed and restarted:

```
{'backend': 'baofeng', 'frequency': None, 'tx_frequency': None, 'mode': None,
 'tone': None, 'power': None, 'modulation': 'FM', 'tune_persist': True, 'tx_ok': True}
```

It reports `frequency: null` — honestly, because it never adopted a zero — and it will key anyway.
That is correct there: on baofeng the **radio** owns the frequency (CLAUDE.md: *"No CAT: frequency
is set by hand on the radio"*), so an untuned host is the ordinary post-restart state and the carrier
goes out on the channel the operator dialled. A host that does not know is not the same as *nobody*
knows. Applying this ADR's refusal there would stop station ID, every voice service and `/transmit`
after every restart — it would brick the live station to fix nothing. Unchanged, as instructed.

`Kv4pHt` is a third shape again: it seeds its model from the device's NVS-preserved tuning rather
than zeros, with the reasoning written down (`kv4p/radio.py:317-323`). It does not *validate* what it
read, so it has a latent version of this; reported, not touched (finding 4).

## Acceptance

**Red run — 10 failed, 1 passed** (`tests/test_uvk5_unknown_frequency.py`, against master's source):

- `POST /ptt` → `assert 200 == 409`, the fail-first case, and the TX word `(0x30, 0xC1FE)` on the wire
- `transmit()` → `DID NOT RAISE RadioNotReady`
- `status().frequency` → `assert 0 is None`, and `assert 5000000 is None` / `assert 2000000000 is None`
  for the out-of-band seeds — an out-of-band read-back was adopted too
- `POST /split` → 200, ADR 0173's other guard equally dead
- `_uvk5_keying_core` → `assert 0 == 1`: doctor reported **PASS** on a key-up at 0 Hz
- the one pass is the pin: a healthy 147.390 seed is adopted and keys

**Suite.** `uv run pytest` **2305 passed / 5 skipped**, from 2294/5 — exactly the eleven new tests.
`cd web && npx vitest run` **14 files / 155 tests**, unchanged; no web file touched.

**Collateral: one test, and the number is the finding.** With the fix in and the fake still
unseeded, the whole suite gave **1 failed / 2304 passed / 5 skipped** — and the single failure was
`test_enter_hw_mode_retries_a_dropped_first_0870`, which asserts *no warnings were logged* and now
saw the new one. Not a keying test. Every uvk5 test that keys already tunes first or passes
`frequency=`, which is precisely why this survived: **the suite never once exercised a key-up on a
radio it had not tuned**, so the guard's deadness was invisible. Seeding the fake's 0x38/0x39 (below)
took it to 2305/5 with no other change.

`FirmwareFakeSerial` now seeds 0x38/0x39 to **147.390 MHz** for the reason its own 0x30 comment
already gives: an empty register file answers 0, so an unseeded fake modelled the *fault* rather
than a working radio. 147.390 because nothing else in the suite tunes there — "it keyed on the seed"
can never be mistaken for "it keyed on a tune".

**Bench, station 8090 (`baofeng`, UV-K6 over AIOC), witness 8091.** Deployed `65ef57a` via
`./update-radio-server.sh origin/adr-0174-no-keying-on-an-unknown-frequency`; unit `active`,
post-restart status quoted above. `acceptance.py` **exit 1**: `systemd`, `presets`, `rx`, `dtmf`,
`auth`, `tx`, `split`, `services` **PASS**; `split-minus` **SKIP** (no `Bench Split Minus` preset);
`web` **FAIL**. Re-run alone with `--only web`: the single failing check is the witness's known
`kv4p GET /healthz → 404`, with `radio GET /healthz → 200` and the serial reader alive.

`POST /ptt` was **not** probed on the station. Its backend is `baofeng`, which has no frequency
precondition on `ptt`, so the probe would key a transmitter to learn something already known. The
uvk5 guard is not reachable on this hardware; its evidence is pytest, and this ADR says so rather
than implying the bench covered it.

**Left as found** — and *as found* had to be measured, because an earlier read of this cycle was
wrong (finding 1): frequency **145 145 000**, tx_frequency **144 545 000**, mode FM, modulation FM,
tone **107.2**, power **low**, `tune_persist` **true**, broadcast FM off, reader running. Acceptance
leaves the radio on 445.800, so the channel was re-applied and verified rather than assumed.
`tune_persist` is reported at what it was found at and **not flipped** — the operator has been told
(ADR 0173 finding 1) and it is his call.

## Findings

1. **A read that was not a reading, in this cycle's own bench work.** An early "read-only" probe
   reported the station untuned. It had actually gone to a **stale `/tmp/st.json`** left on the box:
   the `curl` that was supposed to write it returned `HTTP 000` and wrote nothing, because 8090
   serves **HTTPS** (`server.tls_cert`/`tls_key` are set; `acceptance.py` has defaulted to
   `https://` all along) and the probe used `http://`, which the server closes with an empty reply.
   The station was in fact tuned to 145.145 / 144.545. The planning note built on it was corrected
   before anything was restored. Worth recording as the exact fault class this ADR is about — a
   value that looked like an answer and was not one — arriving through the instrument rather than
   the subject.
2. **The backend logged "transmitting on 0 Hz" and keyed.** `_correct_tx_band`'s warning has been
   there since ADR 0132. A warning that does not gate anything is a record of the fault, not a
   defence against it; wiring the two together is what this cycle did.
3. **`doctor --key-test` reported PASS on a key-up at 0 Hz.** The tool whose entire job is "does
   this radio key cleanly" answered yes. Fixed here, but the shape generalises: a diagnostic that
   only checks the step it names will confirm a broken setup as healthy.
4. **`Kv4pHt` does not validate its seeded tuning either** (`kv4p/radio.py:317-323`). It seeds from
   the device's preserved NVS state rather than zeros — better than adopting anything — but a device
   answering nonsense would be believed. Its own cycle: the kv4p owns tuning over a different wire
   and the failure modes need measuring before a guard is written.
5. **`AiocBaofeng._reassert_channel` cannot re-assert a channel it does not know.** It returns early
   when `_tuned is None`, which is every restart. The guard exists precisely because a volatile tune
   is lost when somebody uses the radio's power switch — and the one moment it cannot help is the
   one where the host has forgotten too. Benign today (the radio holds the operator's channel), and
   a real gap for a station whose tune persists in the host's model but not the radio's.
6. **The three mid-TX `RuntimeError`s in `aioc_baofeng.py` (`:610`, `:1139`, `:1160`) are still
   `RadioBusy`**, carried forward unchanged from ADR 0173's findings. One line each, and the
   app-wide 409 handler is already waiting for them.
7. **`_key_on` checks `tx_allowed` before the frequency.** Deliberate and unchanged — a receive-only
   node should say *this backend does not transmit*, not *I do not know where I am* — but it means
   only a radio with TX enabled can demonstrate this guard, which is why every test here forces it.

## Out of scope

The firmware fork; the witness checkout (its `/healthz` 404 is a known stale build); capping the
EventHub queue; `modulation` on `GET /presets`; the `set_tone`/`set_channel` items from ADR 0172's
mock audit; and any change to the deployed `baofeng` keying path.
