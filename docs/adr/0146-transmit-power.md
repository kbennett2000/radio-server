# 0146 — Transmit power becomes something the operator can set

Status: Accepted

Builds on ADR [0142](0142-the-server-picks-the-repeater.md) (the tuner and the power-scale bug) and
[0145](0145-instant-by-default.md). Inherits ADR [0132](0132-dock-band-and-the-register-model.md)'s
finding about what this bench can and cannot measure.

## Context

The operator asked whether power levels can be edited. They could not — not from config, not from a
preset, not from the API. Every layer above `VfoImage` was hardcoded: `power: int = POWER_HIGH`, and
`from_preset(cls, preset, *, power=POWER_HIGH)` was the only place it was ever set, called by nobody
who passed anything else.

Measured before touching it, rather than inferred from the code: the radio's own read-back reported
**`power=7` on all 186 tunes** in the preceding three hours. `7` is `OUTPUT_POWER_HIGH` in an enum
that runs `USER, LOW1..LOW5, MID, HIGH`.

### Everything below the seam already worked — and it is the *calibrated* path

`0x0873` carries the level; the firmware maps the wire's 0/1/2 through `DOCK_POWER_MAP` to
`OUTPUT_POWER_{LOW1, MID, HIGH}` and then calls `RADIO_ConfigureSquelchAndOutputPower(vfo)`, which
computes `TXP_CalculatedSetting` **per band from the radio's own flash calibration**
(`App/app/uart.c:808-856`). The host mirror is `FIRMWARE_POWER = (1, 6, 7)`, and the EEPROM record
has carried it since ADR 0142 (byte 12).

That is worth stating because it is the exact inverse of the dock backend's problem. Over the dock
the host writes PA bias directly and the per-band calibration is in flash it cannot read — bias
12/255, `band_matched: false`, "the radiated power is uncharacterised" (ADR 0128/0132/0134). In
Baofeng mode the radio does that arithmetic itself. Setting power here is the good path; it simply
had no way in.

So this is one more field along a seam that already carries frequency, split, tone and mode.

## Decision

`"low" | "mid" | "high"` at every layer an operator touches; the 0/1/2 ints stay inside `vfo.py` and
the wire. Three steps because three is what the dock map offers — a fourth would be a level the radio
silently rounds off.

**`baofeng.uvk5_power` (default `"high"`)** is the boot value, so nothing moves for anyone who does
not touch it. **`POST /power`** and a selector in the Tune card change it live. **A `[[presets]]`
entry may name its own.**

**Absent on a preset is deliberately not "off".** The split and the tone belong to the *channel*, so
a preset omitting them means none, and leaving the last one running is the bug ADR 0133 fixed. A
power level belongs to the *station* — how hard to talk, not who to talk to — so a preset that says
nothing means "however I am set", and forcing a default on every channel tap would silently undo the
operator's own choice. `apply_preset` is therefore the one field it applies conditionally.

**One visible level.** A preset that names a level moves the station level, so `status.power` is
always the current one and the UI control always reflects it. There is no hidden per-channel value
fighting a visible control.

**`status.power` reports what the radio confirmed**, not what was asked. It is `None` before the
first tune — the radio is on whatever its front panel says and the host cannot see it — and `None`
for a level this server did not choose, since the front panel reaches `LOW2..LOW5` and `USER`, which
have no name here. Naming one anyway would be inventing a number (ADR 0134's rule).

**Intent and confirmation are answered separately.** `commit_tuning` deliberately keeps `_pending`
when the tuner raises, so a failed setter is retried by the next tune — that is how `set_tone` has
always behaved. The station level moves with that pending channel, or a failed `set_power` would
reach the radio on the next tune while the server said it had not. `status.power` reads `_tuned`,
which a failed tune never updates.

### The check this made necessary

`SetVfoTuner.apply` validated the frequencies and the tone against the `0x0874` reply and **only
logged the power**. Harmless while power was a constant; not once it is settable. `out->power` is
read out of `gEeprom.VfoInfo[0].OUTPUT_POWER` *after* the firmware applied it, so it is checkable for
free — and the scale is a trap that has already bitten once: assigning the wire's 0/1/2 raw lands
"high" on `LOW2`, which tunes perfectly and never opens a repeater (ADR 0142). Asking for low and
silently getting high is worse, because an operator turns power down for a reason.

## Acceptance — two claims, and only one of them could be measured

**Claim 1 — the level reaches the radio. Proved, and the radio is the witness.**

| asked | `status.power` (3 passes) | `OUTPUT_POWER` read back |
|---|---|---|
| low | low, low, low | **1** (`LOW1`) |
| mid | mid, mid, mid | **6** (`MID`) |
| high | high, high, high | **7** (`HIGH`) |

`scripts/bench/power_levels.py -n 3`, exit 0. The right-hand column is `v->OUTPUT_POWER` out of the
radio's own VFO struct after it applied the channel — not a value this server sent and read back to
itself.

**Fail-first, run against the bug.** The same gate against a build whose `set_power` accepts the
request and drops it — which is precisely what the code did before this cycle — scored
`low -> high`, `mid -> high`, **exit 1**.

**Claim 2 — it changes what goes out. NOT MEASURED, and that is a fact about this bench.**

The witness reported RSSI **0 throughout, including its idle floor**. That is not "the levels were
indistinguishable": this kv4p firmware reports `latest_rssi` as 0 even while cleanly demodulating a
carrier, which **ADR 0132 had already established** — and `uvk5_pa_sweep.py` had already produced two
confident-and-wrong "FLAT: bias is not the knob" verdicts from exactly that dead field before it was
taught to refuse.

I proposed that instrument in this cycle's plan without checking what the project had already
written down about it. The gate now says **NO MEASUREMENT** in those words when the witness reads
zero, and names what measuring it would take: a field-strength meter, or the person-with-a-handheld
route `uvk5_audible_sweep.py` takes. A flat line from a broken meter looks exactly like a flat line
from a real null, and only one of those is a finding.

The exit code reflects claim 1 alone. Failing the run because a witness inches away cannot resolve
1 W from 5 W would be scoring the bench's geometry as a defect in the radio.

`uv run pytest` 1873 passed / 5 skipped; `npm test` 71 passed.

## Consequences

An operator can turn power down for a repeater they can hit easily, a dense site, or battery life,
and up for range — from the Tune card, per channel, or in config. What each level is **in watts is
not answered anywhere in this server**, and no code or document here may claim one: the number is
computed by the radio from calibration this host cannot read.

`SET_POWER` is advertised by the UV-K5 tuner and the mock. The dock backend's power is a raw `0x36`
bias write — a different mechanism with its own open questions — and `kv4p.high_power` is
constructor-only; both 501, and the UI hides the control rather than offering a dead one.

## Out of scope

- **CHIRP's `Power` column.** `chirp.py` does not parse it; mapping per-radio power names onto three
  levels is its own question.
- **Watts, and claim 2.** Both need equipment this bench does not have. Filed beside ADR 0137's
  standing "absolute RF power unmeasured" gap, which this does not close and does not narrow.
- **kv4p and the dock backend**, as above.
- **2 m** — still unverified end to end; the witness is SA818-UHF.
