# ADR 0175 — Signal strength on the deployed backend, and what reading a register costs a transmission

**Status:** Accepted · 2026-08-03 · builds on [ADR 0122](0122-uvk5-v3-f3-bench-loose-ends.md) (the
meter), [ADR 0125](0125-uvk5-v3-rx-pump-cat-gate-decouple.md) (the cadence), [ADR
0132](0132-dock-band-and-the-register-model.md) (the register model) · closes the gap [ADR
0160](0160-the-bench-answers-back.md) filed as finding 2

## Context

`status.rssi` was `null` on `AiocBaofeng`. The field exists in the shared model
(`base.py:454-460`), is documented (`docs/api.md`), and is filled by `uvk5` and `kv4p` — but it had
**no source at all** in the backend the station actually runs. ADR 0160 filed that as a real gap
after its airband hunt had to sweep 760 channels by audio RMS instead of asking the radio, and
`docs/server-notes.md` warns operators that the standard `curl /status | jq .rssi` advice does not
apply here.

The brief asked three questions before any design, and was right to. Two were answered from source,
one by measurement, and the measurement produced a result the design had to be rebuilt around.

*(Three ADR numbers in the brief are off by a few, and are corrected throughout: the reg-0x67
measurements are **0122**, not 0123; `PolledGate` and the 14 % duty figure are **0125**, not 0127
— 0127 is bounded graceful shutdown, cited in the `rssi` docstring for a different reason.)*

## 1. Is there a register-read path today? No — and it is a policy, not an oversight

`ReadRegisters` (`0x0851`) is constructed in exactly three places tree-wide: `uvk5/radio.py:393`,
`uvk5/transport.py:479` (the connect liveness elicit), and `doctor.py`. **`tuner.py` contains no
register read of any kind.** Everything the hybrid path knows comes from reply read-backs cached on
`SetVfoTuner`, each seeded `None` from nothing.

The capability was never missing. `aioc_baofeng.py:509-513` hands the dock `Uvk5Transport` **the
serial handle the backend already owns**, so the read is one call away. What stood in the way is a
written position, stated three times — `aioc_baofeng.py:441-447`, ADR 0155:51-57, and ADR 0132's
*"take whatever state you find"*: inferring the demodulator from a register read *"reads the
firmware's leftover rather than its VFO truth"*.

**That refusal does not reach RSSI, and the distinction is the load-bearing one in this ADR.** Those
three are about **seeding a belief the radio owns** — modulation, split, band — where a leftover
value becomes a wrong decision on a later key-up. RSSI is a **measurement**: instantaneous, never
carried across an operation, and nothing in the server decides anything on it (`base.py:456`:
*"Diagnostic only"*). There is no state to adopt wrongly. The policy stands for exactly what it
refused.

## 2. Will `0x0851` answer in baofeng mode? Yes — settled from source, then measured

`App/app/uart.c:1504-1523` puts `0x0850`/`0x0851`/`0x0871`/`0x0873`/`0x0877`/`0x0879` in the
ordinary non-blocking dispatch, deliberately **not** with `0x0870` full control, with the reason in
the comment: *"the main loop keeps running, so the radio keeps sampling its own PTT pin and stays a
radio."* The same comment records that none of them arms the six-second lockout —
`gSerialConfigCountDown_500ms` is set at exactly four sites (`CMD_0514`, `CMD_051B`, `CMD_051D`,
`CMD_052F`) and none is reachable from there. `dock.c:387-399` loops the requested addresses through
`hal->read_reg` and emits one `0x0951` each: no write, no EEPROM, no state change. The repo had
already written the conclusion down at `transport.py:86-89`, ending *"VERIFY ON BENCH"*. This did.

**`0x0870` must never be entered from this path.** It makes the tuner's own opcodes answer
`ERR_BUSY`, and the `0x0871` exit retunes the synthesiser from the radio's own VFO.

## 3. Does reg 0x67 mean anything in stock receive mode? Measured: yes, unambiguously

Read-only probes over the AIOC with the service stopped (the ADR 0127 borrow-and-restart pattern
every Tier-2 bench script uses), opened through `transport.open_serial` so DTR and RTS are held low
from the moment the port opens — on this station `baofeng.ptt_line = "dtr"`, so a plain
`serial.Serial(port)` would key the transmitter on a receive-only probe. No `Uvk5Radio` (its
constructor writes), no `AiocBaofeng` (sound card, PTT), no `0x0870`. **The radio under test was
never keyed at any point in this cycle.** The kv4p witness on :8091 was the only transmitter.

| arm | answered | min | mean | max | sd |
|---|---|---|---|---|---|
| liveness, stock receive | 10/10 | 105 | 107.3 | 110 | 1.6 |
| **silence**, 6 interleaved rounds | **72/72** | 103 | 106.8 | 114 | 1.8 |
| **witness carrier**, same rounds | **72/72** | 310 | 310.2 | 311 | 0.4 |
| serial only / +capture / +capture+playback | 48/48 | — | — | — | — |

Arms interleaved rather than blocked, because a run whose conditions drift ranks its arms by when
they ran (ADR 0140, the lesson `uart_while_streaming.py` cites). Separation **+203 counts**, and the
populations do not touch: silence max 114, carrier min 310, a 196-count gap.

**The numbers cross-validate against work that predates them.** ADR 0122 measured a keyed 445.800
carrier at **~311** under *full dock control*; this measured 310-311 on the same frequency with the
radio in its own stock receive mode. ADR 0132 measured a per-band floor of **107 on 445.800** vs
154-156 on 147.555; this measured 106.8 on 445.800, and after deployment the station reads **161-170
on 145.145**. The register means the same thing in baofeng mode as in dock mode, and **the floor
moves with the band**, so nothing may hardcode one.

Edge behaviour, 4 key/un-key cycles sampled at 0.15 s: the reading **rises on the first sample after
key-up** (the 253/284 ramp-in ADR 0122 also saw) and is back at the floor **two samples after
un-key**. The channel is never more than a couple of polls ahead of a 0.5 s cadence.

### The one loose end, reported rather than explained away

The first probe's streaming stage threw **ten `0` samples in 48**. Three attempts to reproduce it
failed: 150 samples with the capture held open continuously, 105 across 27 sound-card open/close
cycles, and 136 across 8 key/un-key edges — **391 samples, zero zeros**, on top of the 144 in the
silence/carrier arms. The hypothesis that open/close cycling caused it is **refuted**. What produced
those ten is not known.

The design does not need to know. **`0` is reported as no reading**, on three independent grounds:
ADR 0132 measured `reg 0x30 = 0` taking this register 157 → 0 *because the receiver was off*; the
floor is ~107 counts, so 0 is fifty-odd dB below anything a working front end reports; and ADR 0122
records it reading 0 right after open before climbing to the noise floor. Rendering 0 as a signal
level would rebuild ADR 0132's exact fault — a station reporting `rssi 0 / busy false` for ever,
indistinguishable through the API from a quiet one.

## 4. The finding that rebuilt the design: a register read destroys a transmission

The first deployment shipped a 0.5 s cadence with no exception for transmit. `acceptance.py` came
back with **three** stages failing where the previous cycle had one. Bisected against `master` on
the same station, same frequency, same witness, minutes apart:

| check | master (c8b8d0a) | branch, cadence running through the over |
|---|---|---|
| `tx` — kv4p recovered 1000 Hz | **0.989** | **0.026** |
| `tx` — audio reaching the witness | 434852 B / **4.42 s** | 70866 B / **0.70 s** |
| `services` — announcement length | **5.3 s** | **0.9 s** |
| `services` — speech-band energy | 0.98 | 0.28 |

**A 12-byte register read every half second wrecks the transmitted audio.** The AIOC is one USB
composite device — the CDC serial interface and the audio interface share a cable, a controller and
the radio's K1 jack contacts — and the isochronous audio-out stream feeding the transmitter does not
survive the contention. Both stages came back to master's numbers the moment the cadence was paused
while keyed: tone **0.989**, 4.52 s of audio, announcement **5.1 s**.

**This is a gap in evidence the repo already believed it had closed.** ADR 0144's
`uart_while_streaming.py` measured dock frames surviving a running sound card, 18/18, and that is
what `aioc_baofeng.py:495-504` cites for holding one handle. It is a different claim: it proved the
**frames** got through, never that the **audio** did. The station has been tuning mid-session on
that evidence ever since — a tune is a handful of frames at a moment nobody is transmitting, which
is why it has never bitten. A *cadence* is the first thing to put steady traffic on that wire, and
it found the edge immediately.

Pausing costs the meter nothing. `status()` reports `null` while keyed anyway — a receiver cannot
measure a channel through its own carrier — so a poll taken during an over was always going to be
discarded. It was pure risk on a wire that is also the PTT line.

## Decision

1. **`Uvk5Transport.read_register`** — one implementation of the read and its match predicate.
   `Uvk5Radio._read_register` delegates, behaviour unchanged. The predicate is load-bearing:
   `m.register == reg` is what makes concurrent reads safe, and is the whole basis of ADR 0125's
   thread-safety argument for `PolledGate`.
2. **`SetVfoTuner.read_rssi`**, delegated on `HybridTuner` — `wire_timeout=0` so it always yields to
   a key-up or a tune (ADR 0163's property), every failure and a raw `0` collapsing to `None`.
   Placed on the `0x0873` half and not the EEPROM half, whose every method opens a session the radio
   charges six seconds of TX lockout for; a cadence must not be able to reach one.
3. **`RssiPoller`** — a `BroadcastFmPoller`-shaped cadence at **0.5 s**, a module constant marked
   *VERIFY AGAINST HARDWARE*, not a config key (there is no `baofeng.*` cadence key and ADR
   0125:138-139 argues against adding one). It **pauses entirely while `_transmitting`**, and skipped
   rounds are counted apart from failed ones. It **expires**: a reading unrefreshed for 3 intervals
   reports `None`, which `PolledGate` and `BroadcastFmPoller` do not need — they hold a verdict about
   a state that persists, this holds a measurement of a moment, and a stale measurement rendered as
   current is not late, it is wrong.
4. **`doctor --rssi` stops being uvk5-only** — the instrument that took the measurements is the one
   that ships. On baofeng it prints counts with no busy verdict, because there is no threshold on
   that backend to score them against.

**`busy` stays `False` and the deployed squelch stays `audio`.** This surfaces a diagnostic number;
it does not become a carrier-detect. No UI, no new API fields, no firmware.

### Why the cadence and not a read inside `status()`

ADR 0125 measured the alternative: `CatBusyGate` doing a ~100 ms serial round-trip per 20 ms audio
frame took the RX pump to **14.3 % duty with zero bytes reaching the browser**, and inline caching
only slowed the failure (84.4 %, still overflowing the 60 ms ring). Its rule — *you cannot do a
100 ms blocking serial read on the thread that must drain a 60 ms ring, ever* — is about the audio
thread, and `status()` is not that thread today. But `CatBusyGate` calls `radio.status()` and is one
config key from being on it. A cadence is the shape that cannot regress into the measured fault.

0.5 s is sized from this cycle's own edge measurement, not by analogy: 0.2 s is `PolledGate`'s
because its verdict gates audio and lag is audible; 2.0 s is the broadcast-FM poll's because it
watches a human pressing a key. This gates nothing and follows an over, and 0.5 s matches the
station's own `audio.vad_hang`, so the number is never staler than the gate beside it.

## Acceptance

- **pytest 2322 passed / 5 skipped** (from 2305/5). Red run first: **10 failed / 4 passed** — the
  four that passed on `master` are the negative pins, which pass trivially while `rssi` is always
  `null`.
- **vitest 14 files / 155 tests**, untouched — `web/` has zero `rssi` references and still does.
- `tests/test_baofeng_rssi.py` is **the harness that did not exist**: the first test anywhere to wire
  `FirmwareFakeSerial` into `create_radio("baofeng", ...)`, so the backend, `HybridTuner`,
  `Uvk5Transport` and the frame codec are all real. `test_aioc_baofeng_tuning.py` — 82 KB of it —
  injects a `SpyTuner` over a serial fake with no `read`/`write` at all.
- Bench, deployed: `/status.rssi` **161-170** on 145.145. `scripts/bench/rf_listen.py`, the script
  `server-notes.md` currently offers *instead* of RSSI here, run through the deployed HTTPS API at
  445.800: **silence 48/48 polls answered, floor/peak 104/111, median 106; witness carrier 48/48,
  317/331, median 327**, with 51840 B of audio corroborating the carrier. `squelch open 0/48` in
  both, which is the pin that `busy` was not touched.
- `acceptance.py`: exit 1, **9/10 PASS**, `split-minus` SKIP, `web` FAIL on its single known check
  (`kv4p GET /healthz → 404`; the witness runs older code because `update-radio-server.sh` does not
  update it). Re-run alone to confirm nothing else in that stage fails.
- Station restored to **145.145 / TX 144.545 / 107.2 / FM / low** and verified;
  `uvk5_tune_persist` reported **as found (`true`)** and not flipped.

## Findings, recorded rather than fixed

1. **`uart_while_streaming.py` proves frame delivery, not audio integrity**, and
   `aioc_baofeng.py:495-504` cites it for a broader claim than it measured. The gap is now known and
   has one guard (this cadence's pause). Anything else that puts steady serial traffic on the AIOC
   needs the same treatment or the same measurement.
2. **The ten unreproduced `0` samples.** 391 samples across three deliberate reproduction attempts
   produced none. Design is conservative regardless; the cause is open.
3. **`kv4p`'s `rssi` is non-null but is a known-broken meter** — it reads `0` while cleanly
   demodulating (ADR 0132:254, ADR 0146:96-97), and the deployed witness reports exactly that. A
   non-null number from a meter that does not work is worse than `null`, and it is the same fault
   this ADR refuses to commit on baofeng.
4. **`aioc_baofeng.receive()` discards the xrun flag** (`:976`) that ADR 0125 Decision 3 fixed on
   `uvk5` only. This backend has no overflow visibility at all.
5. **`update-radio-server.sh` does not update the witness** (`docs/server-notes.md:110`), which is
   why the `web` stage's 404 persists across cycles.
6. **`FirmwareFakeSerial` modelled a radio older than the bench's.** It answered neither `0x0877`
   nor `0x0879`, both of which the deployed F9 firmware answers — costing every test built on it a
   3 s `SetVfoTuner` timeout at construction (45.7 s → 3.5 s for this module once modelled) and
   reporting the radio as pre-F7 while doing it.
7. **`docs/server-notes.md` contradicts itself about `rssi`** — `:47-50` says there is no host-side
   read on this backend, `:300` tells operators it is the first thing to check. Both sentences are
   still there; this cycle makes the second one true and updates the first.

## Out of scope

The firmware fork, the witness checkout, EventHub, the ADR 0172 mock audit items, `0x0870` full
control, any UI, and any change to squelch or the keying path. An S-meter, a live squelch threshold
and scan-stop-on-activity are all now *reachable* on this station — this ADR establishes the number
they would read, and builds none of them.
