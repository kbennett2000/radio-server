# 0135 — Repeater key-up: retracting the power hypothesis, and building an instrument that could see the fault

Status: Accepted

Extends ADR [0132](0132-dock-band-and-the-register-model.md),
[0133](0133-repeater-split-and-chirp-import.md) and
[0134](0134-repeater-keyup-in-the-field.md). **Retracts ADR 0134's ranked hypothesis 1.**

## Context

None of the operator's 37 repeater presets key up. Simplex TX is good on the air.

ADR 0134 ranked "the radio's front-panel TX power setting" as the leading survivor, on the strength
of reading `L1` off a photograph of the radio's screen. The operator has since established, on the
bench, that **an Icom ID-51A and a Baofeng Mini open these same repeaters through this same antenna
at microwatts**.

That retires power, feedline, antenna, and the repeaters themselves, all at once. It also retires
the reasoning that produced it: a photograph is not a measurement, and ADR 0134 said so about
itself and then leaned on it anyway.

## What died this cycle, and how

All by reading the **compiled** driver. `App/driver/bk4819.c` exists in the firmware tree but is
**not** in `App/CMakeLists.txt` and does not ship; `App/driver/bk4829.c` is what runs. The two
differ in exactly the places that matter here, so analysis quoting `bk4819.c` was reading dead code.

| Hypothesis | Verdict | Evidence |
|---|---|---|
| `Dock_ForceTx` wipes host CTCSS via `ExitSubAu` | **DEAD** | `BK4819_PrepareTransmit` = `ExitBypass` + `ExitTxMute` + `TxOn_Beep`, nothing else (`bk4829.c:1222-1236`). The `CodeType` switch that calls `ExitSubAu` lives in `RADIO_SetTxParameters` (`radio.c:1025-1040`), which the dock hook does not call. |
| `Dock_ForceTx` retunes to the front-panel VFO | **DEAD** | No `BK4819_SetFrequency` anywhere in the hook's call chain; REG_38/39 untouched. |
| Our CTCSS code word is wrong | **DEAD** | Our `((tone*10 * 206488) + 50000) // 100000` is byte-identical to `bk4829.c:586`. Verified to decode back within 0.03 Hz across all eight tones in use. |
| TX power | **DEAD** | Operator, empirical: two other handhelds open these repeaters at microwatts on this antenna. |
| Duplex signs / tone columns | **DEAD** (ADR 0134) | 0 mismatches across all 41 presets. |

**On paper, the register programming is correct.** Nine hypotheses have now died across two cycles,
five of them to source reading. Reading more source is not going to find this.

Two discrepancies were found and are recorded here so nobody rediscovers them and mistakes either
for the answer:

- **REG_51 gain.** We write `0x904A`, the firmware writes `0x9040`. The only difference is the
  `<6:0>` "CTCSS/CDCSS Tx Gain1 Tuning" field: **74 vs 64**. Ours is *hotter*. The firmware's own
  inline comment claims 74 while its code writes 64, which is where our value came from. **Not
  changed in this cycle** — under-deviation is a live hypothesis and lowering our tone gain to
  match would move us the wrong way. It is a fidelity question to settle *after* measurement.
- **REG_50.** We write `0x3B20`; the firmware's `ExitTxMute` writes `0x3B18` *after* us and wins.
  Our write is inert.

## The real finding: the bench could not have caught this class of fault

The only over-the-air CTCSS check in the repo is `tone_power()` — a **dimensionless fraction** of
spectral energy, deliberately level-independent so it survives changes in power and geometry.

That is the wrong instrument for a deviation question, and not by a little. Measured against
synthetic signals with a known tone amplitude:

| CTCSS deviation | `band_rms` (this ADR) | `tone_power` fraction (what the bench used) |
|---|---|---|
| 100% | 5656 | 0.999987 |
| 50% | 2828 | 0.999950 |
| 25% | 1414 | 0.999800 |
| 10% | 565 | 0.998757 |

A tone at **one tenth** of correct deviation moves the old metric by 0.001, against a pass floor of
0.01. It would pass the bench and open nothing in the field — which is the reported symptom
exactly. On a dead carrier the fraction tends to `P_tone / (P_tone + P_noise)` and saturates near
1.0 as soon as the tone clears the noise, so it has resolution only where the tone is comparable to
the noise floor. That is not the regime under test.

Worse, that check runs **only** inside the split stages, hard-coded to 100.0 Hz, and those stages
skip on the current `radio.toml` — so in practice it has not been running at all.

Nothing anywhere reads back reg `0x51`, `0x07`, `0x40` or `0x43`. Deviation has never been
measured, in any units, ever.

## Decision 1 — measure deviation against a known-good transmitter

`scripts/bench/deviation_probe.py`. A radio that demonstrably opens these repeaters is a
calibration reference; the question becomes a ratio, and a ratio is answerable on this bench.

Both transmitters send a **dead carrier plus CTCSS, no audio**, on the same frequency into the same
witness at the same geometry. Every loss in the receive chain — the SA818's de-emphasis and LF
roll-off, Opus, the resampler — is common to both captures and divides out. **This reports a ratio
and never claims deviation in Hz**; the bench has no instrument that can measure that, and saying
so is part of the design.

The metric is `band_rms()`: mean removed, Hann window, every bin outside `f ± 5 Hz` zeroed,
transform back, RMS, divided by the window's own RMS to restore units. Verified linear in amplitude
to three decimal places over a 10:1 range (table above). Absolute, not normalised, **on purpose**.

**Three legs, because two cannot localise a fault.** The middle leg costs nothing — the radio is in
your hand:

| A ≈ B ≈ C | deviation is fine on all three; the fault is elsewhere |
|---|---|
| **A ≈ B ≫ C** | our dock register writes under-deviate the tone — a bug we own |
| **A ≫ B ≈ C** | the UV-K5 itself under-deviates in both firmwares — not a code bug |
| **B ≫ A** | the rig is broken; stop and fix it before believing anything |

**141.3 Hz, not 100.0 Hz.** Opus/SILK applies a high-pass whose corner moves through roughly
60–100 Hz as a function of signal content, so a measurement at 100 Hz confounds "the tone got
weaker" with "the codec's corner moved". 141.3 Hz is the highest tone in the operator's set and
sits above it. Our REG_51 gain is the same constant for every tone, so a result there generalises.

Two failure modes the probe refuses to report through:

- **A silent capture is not a zero-deviation measurement.** The RX pump gates frames on activity; a
  carrier modulated only by a sub-audible tone can fall under a level-driven gate and deliver
  nothing, which is byte for byte what no tone looks like. `witness_heard_anything()` refuses to
  record it.
- **An un-driven modulator is not a refutation.** The audio-level sweep adds a fixed 1000 Hz pilot
  alongside the swept tone, and `reached_the_limiter()` requires the swept tone to have visibly
  compressed before any verdict is read. Without that control a flat curve means "we never tested
  it" and reads as "audio does not squash the tone". It reports INCONCLUSIVE instead.

## Decision 2 — read the chip, not the source

`scripts/bench/uvk5_tx_regs.py` gains `0x51`, `0x07`, `0x40`, `0x43` and a `--tone` flag, so the
tone is actually armed when the keyed read-back happens.

Every claim above that the tone survives the key edge comes from reading firmware source. The
identical assumption was wrong for `0x33`/`0x36` — that is what ADR 0132 exists to record — so it
gets read off the chip. The dump decodes `0x07` back to Hz rather than printing a code word.

**Prediction written down before the run:** `bk4829.c:151` is the only REG_40 write in the entire
firmware — a boot constant `0x3516`, never per-band, never per-bandwidth, never from EEPROM, never
in `Dock_ForceTx`. So the radio's own front panel transmits with the identical deviation word. A
read-back of `0x3516` confirms the model; anything else is a genuine new finding.

## Decision 3 — the keying guard was one import away from a live repeater

`bench_frequency_only` gained a preset deny-list on top of its bench allow-list. Both, not either:
a bare deny-list would make a frequency in *no* preset keyable, which is weaker than what we had.

The hazard the allow-list alone cannot see is a real repeater imported onto a bench frequency —
then "this is a bench frequency" and "this is a live machine's channel" are both true, and only the
preset list can tell them apart.

Two design points that are load-bearing, both found by writing the tests:

- **Sources are unioned, not first-wins.** A freshly restarted server that loaded zero presets
  answers `GET /presets` with `200 []` — a successful read of nothing — which would unlock every
  frequency the file forbids. The file is always consulted too.
- **The exemption is per *preset*, not per *frequency*.** The runner's own split fixtures are
  presets on the bench pair, so something must be exempt; a preset qualifies only when **every**
  leg it uses is a bench frequency. Exempting per frequency instead would quietly exempt the
  bench-side leg of a repeater whose input landed on 445.800 — the exact accident the guard exists
  to prevent. It is never by preset *name*: calling a real repeater "Bench Split" must not launder
  it.

Unreadable presets return `None` and `None` refuses. An empty deny-list means "nothing is
forbidden", which is backwards for a guard whose failure mode is an unattended carrier on a
repeater's input.

`tests/test_bench_guard.py` is the first test coverage `scripts/bench/` has ever had. Verified by
mutation: exempting per-frequency, failing open on an unreadable preset list, and dropping the
deny-list clause each turn it red.

## What this does not claim

- **No RF was measured this cycle either.** There is still no shell on the bench box:
  `ssh kb@home` offers `publickey,password`, this machine holds no private key, and the agent has
  no identities, so password auth cannot be driven non-interactively. Unlike ADR 0134, the cycle
  did not fill that gap with reasoning — it built the instrument and stopped.
- **The root cause is still unknown.** Deviation is a hypothesis with a test, not a finding.
- **`band_rms` is verified against synthetic signals only.** It is arithmetic that behaves as
  designed; whether it survives a real SA818 and a real Opus round trip is what leg A measures.
- **Nothing on 2 m can be measured here.** The witness is SA818-UHF. The 15 VHF repeaters'
  behaviour is inferred, never measured (ADR 0132).

## Consequences

- One new bench script, one extended, one guard hardened, nine new tests. No production code path
  changed: `radio_server/` is untouched by this cycle.
- The REG_51 gain discrepancy (74 vs 64) is documented and deliberately **not** fixed, pending
  measurement.
- Three ordered measurements are queued and written down as a procedure rather than reported as
  done: register read-back, the three-leg comparison, then the level sweep only if the comparison
  does not already settle it.
