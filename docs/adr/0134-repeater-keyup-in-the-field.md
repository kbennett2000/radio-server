# 0134 — No repeater keys up in the field: what was measured, and what the station now reports

Status: Accepted

Extends ADR [0132](0132-dock-band-and-the-register-model.md) and
[0133](0133-repeater-split-and-chirp-import.md). Supersedes nothing.

## Context

ADR 0133 shipped repeater split TX and imported the operator's 37 local repeaters. On a good
antenna with clear line of sight, **not one of them keys up**, while simplex TX is confirmed good
on the air.

That pairing is the whole diagnostic. Simplex and repeater work differ in exactly two things — the
transmitter moves to the repeater's *input*, and it must carry the right CTCSS access tone — and
both are new. The obvious suspicion was a duplex-sign error, because ADR 0133's RF proof used
**one** synthetic preset, `Bench Split`, at rx 445.800 / tx 446.400: a **positive** 600 kHz offset,
on 70 cm. Every real 2 m repeater here is −600 kHz and every 70 cm is −5 MHz. A sign fault is
invisible against that fixture and fatal against all 37.

This ADR records what the measurements actually said, which is not what was expected.

## What was measured, before anything was changed

Read-only, against the live station (`GET` only, authenticated, nothing keyed):

| Check | Result |
|---|---|
| `GET /capabilities` | includes `set_split` |
| `GET /presets` | **41** (37 imported + 3 bench + `Bench Split`) |
| Duplex sign, all 41 | **0 mismatches** — 34 × −0.600/−5.000, 3 × +0.600, every sign correct |
| CTCSS tone, all 41 | **0 mismatches** — 107.2 / 100.0 / 103.5 / 141.3 / 123.0 / 88.5 |
| `GET /status` | `frequency 147555000`, `tx_frequency null`, `tone null` |

**Four hypotheses died on that data:**

1. **Duplex sign.** `chirp.py:174` is `frequency + DUPLEX_SIGNS[duplex] * offset` with
   `{"-": -1, "+": 1}`. The deployed presets confirm it end to end: `W0CRA145.14` is rx 145.1450 /
   tx 144.5450.
2. **CHIRP tone semantics.** `TSQL` → `cToneFreq`, `Tone` → `rToneFreq`, `Cross` → `rTone`/`cTone`.
   This is CHIRP's own model — TSQL encodes *and* decodes on `ctone`. 26 of the 37 rows leave
   `rToneFreq` at its 88.5 default while the real tone is in `cToneFreq`, and every one of them
   imported the `cToneFreq` value.
3. **The browser PTT path skipping the split.** Every keying path — the `/audio/tx` WebSocket,
   `POST /transmit`, `POST /ptt`, and the automatic station ID — converges on `_key_on`, which reads
   `self._tx_frequency` and `self._tone` at key time. There is no split-unaware key path.
4. **The backend not advertising `SET_SPLIT`.** It does.

**So the fault is not in the data and not in the split plumbing.** It is in what leaves the antenna,
or in whether the split is still armed at the moment of key-up. That is a different cycle from the
one the symptom suggested, and the rest of this ADR is about making those two things *visible*,
because neither was.

## Decision 1 — the station reports the PA setup it actually transmitted with

`RadioStatus` gains `pa: PaState | None` (`bias`, `gain`, `band_matched`, `tx_frequency`), recorded
by the uvk5 backend at each key-up from the reg-0x36 read `_correct_tx_band` was already doing.

This is the `rssi` argument again (ADR 0131/0132), and it is the reason this ADR exists at all: a
station whose transmissions reach nothing is **indistinguishable over the API** from one that is
working. Every call returns 200. The operator's report was "no repeater keys up", and there was no
number anywhere in the system to check that against without stopping the service and opening the
serial port by hand — which is the exact anti-pattern ADR 0127 exists to prevent.

`band_matched` is the load-bearing field. `Dock_ForceTx` sets the PA up from `gCurrentVfo` — the
radio's **own front-panel VFO** — not from the frequency the host tuned (`uart.c:729-732`).
`_correct_tx_band` recomputes the gain byte but **deliberately keeps the firmware's bias byte**,
because that bias is per-band calibration out of an SPI flash the dock cannot read; inventing one
is the mistake ADR 0128 removed. So whenever the host transmits outside the radio's VFO band, the
bias in use is the **other band's** calibration and the radiated level is uncharacterised.

The radio's VFO is on **445.800 (UHF)**. The 37 repeaters span both bands, so **at most one band
can ever be correctly biased**, and the 15 VHF repeaters are transmitting on a UHF bias byte. The
host cannot fix this — reporting it is all it can honestly do.

`pa` survives un-key on purpose: the question it answers ("why did that over not reach anything?")
is asked *after* the over. A failed 0x36 read sets it to `None` rather than leaving the previous
over's value, because a stale reading describes a PA setup this transmission never had.

The same event escalates from INFO to **WARN**. It previously read as a routine correction; it now
says the bias is the wrong band's calibration, that the power is uncharacterised, and what to do
about it (put the radio's VFO on the band you are transmitting in).

## Decision 2 — the Talk control says where it will transmit, in both directions

`TalkControl` renders `SPLIT — transmits on 144.5450 MHz` or `SIMPLEX — transmits on 145.1450 MHz`.

**The SIMPLEX half is the point.** The armed split is process-local (`self._tx_frequency`, never
seeded from hardware) and three ordinary actions clear it silently:

| Caller | Reachable from the browser |
|---|---|
| `scan/engine.py:240` — every scan hop | yes, one button; stopping the scan never restores it |
| `api/app.py:1121` — `POST /frequency` | yes, the Tune card |
| `uvk5/radio.py:369` — construction | yes: backend select, and every `systemctl restart` |

Before this, the only indication was a `StatusPanel` row that *disappears*. A row vanishing is not a
signal an operator holding the Talk button can notice, and a repeater preset whose TX leg has been
cleared transmits on the repeater's **output** — audible to nobody, with every API call returning
200. A card that announced only the split would have been exactly as silent in the failure case, so
the simplex state is stated out loud and tested first.

`StatusPanel` also grows a `PA (last over)` row, which names the wrong-band case in words rather
than showing a bias byte nobody can interpret.

## Decision 3 — the audit that would have caught this class, and the leg that was never keyed

**All 37 rows, recomputed independently.** `tests/test_chirp.py` pinned 4 rows by value; the other
33 had no per-row assertion. A duplex-sign or tone-column error is uniform — it lands on all 37 or
none — so spot-checks are the one shape that cannot rule it out. The new test recomputes every row's
input frequency and transmitted tone from the export by a different route (exact `Decimal`, explicit
per-mode column choice) and compares all 37. Verified by mutation: inverting `DUPLEX_SIGNS` and
swapping the TSQL column each turn it red.

**A negative-offset RF stage.** `split-minus` is the exact mirror of `split` — the same two bench
frequencies with the legs swapped, rx 446.400 / tx 445.800 — so it exercises the negative branch
while keying nothing new. Both stages are now one parameterised proof rather than a copy that could
drift from its twin, and each fails loudly if its fixture preset does not match the legs it claims
to test. Nothing in the backend's split math is sign-sensitive on inspection (the offset is compared
with `abs()`, both legs validate as absolute frequencies, the synthesiser word is unsigned) — but
"sign-agnostic on inspection" is not "measured", and 34 of 37 repeaters use the untested sign.

**A key-up carrying a tone *and* a split.** Every split test keyed with no tone; every tone test
keyed simplex. So the shape all 37 presets actually use had no coverage, and nothing asserted the
CTCSS pairs reached the key-up batch at all. Verified by mutation: deleting `*self._tone_pairs()`
from `_key_on` fails this test and **no other test in the suite**.

## Decision 4 — three keying-safety holes the split fixtures opened, closed

Adding a second split fixture at 445.800 turned three latent hazards in the bench runner into
reachable ones. All three are about the same thing: **an armed split makes "where is the radio
pointing?" a different question than it used to be**, and three places were still answering the old
one.

- **`stage_presets`' home restore matched on `frequency` alone.** `Bench Split` also sits on
  445.800, so `next()` could restore "home" with a split armed to 446.400 — and the stage's own
  `restored to 445.800` check inspects only `frequency`, so it passed green. `stage_tx` would then
  key on the armed leg while the witness listened on 445.800: zero carrier, zero RMS, a red X
  identical to a dead transmitter. That is the "started bisecting a regression that did not exist"
  failure `rf_witness`'s docstring says already cost this bench real time.
- **Every early return in the split stage skipped the restore.** Harmless for the positive stage,
  whose RX leg *is* home — and not harmless for the negative one, which listens on 446.400. The
  restore now runs in a `finally`, so a bail cannot strand the station off the only frequency the
  kv4p can hear.
- **`_set_session` keyed through `POST /services/99` with no `bench_frequency_only`.** Pre-existing,
  and the only keying path in the runner outside the guard. `rf_witness` compares the two radios'
  *receive* frequencies, so it would pass a station whose split was armed to a real repeater's
  input — exactly what that guard exists to refuse. Now guarded for both digits: dispatching any
  service is a request that may key, and the guard costs one `/status` read.

The fixture check also pins the **tone**, not just the two legs. Without it a drifted `tx_tone`
surfaces as a failed CTCSS check, which looks precisely like the backend having stopped putting
CTCSS on the carrier — one of the two things this cycle exists to make observable.

## What this does not claim

- **It does not identify the field root cause.** It removes four candidates with evidence and makes
  the two survivors observable. The ranked survivors are: the radio's front-panel TX power setting
  (the dock cannot set it — ADR 0128 — and the measured bias is 12); the wrong-band bias above; and
  a split cleared before key-up.
- **No RF was measured this cycle.** There is no shell on the bench box from the cycle environment
  (`Permission denied (publickey,password)`, no key present), so every claim here is either
  read-only HTTP against the live station or a unit test. The register-level and RF steps are
  written down as a procedure rather than reported as done.
- Reading `L1` off a photograph of the radio's screen is not a measurement. Whether the bias byte
  tracks the OUTPUT POWER menu is the next thing to measure, and it is cheap: read reg 0x36 keyed at
  Low, then at High.

## Consequences

- `/status` carries `pa`; it is `null` on every non-uvk5 backend and before the first over, and
  nothing decides anything on it.
- The web shows where Talk will transmit, and what the last over radiated.
- `split-minus` joins `STAGES`. It needs a `Bench Split Minus` preset in the deployed `radio.toml`;
  without it the stage SKIPs, and a skipped stage is not a pass (ADR 0129) — the run exits non-zero.
  The fixture is documented in the runner's docstring rather than in `radio.toml.example`, which
  deliberately ships no live preset.
- Still unproven, and stated as such: negative-offset split over RF, any 2 m split over RF (the
  bench has no 2 m receiver — ADR 0132), and whether the PA bias is the field fault.
