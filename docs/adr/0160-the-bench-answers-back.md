# 0160 — The bench answers back

Status: Accepted
Date: 2026-07-31

## Context

ADRs [0148](0148-the-firmware-is-a-product-too.md) → [0158](0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md)
built an AM arc and a broadcast-FM arc across eleven cycles. Every one of them ended with the same
sentence: *nothing flashed, no hardware claim*. The reasoning was careful, the goldens were derived
twice, the fail-first runs were recorded — and not one line of it had ever met a radio.

**The operator flashed F8 by hand.** This cycle is the first in the arc to run on hardware, over SSH,
against the deployed station. It measures; it does not fix.

### The premise had to be checked, and three parts of it were wrong

The brief said "the radio is flashed with F8, read `docs/BENCH.md` for the deployment specifics."
Four pre-flight measurements, taken before any change:

| # | measured | consequence |
|---|---|---|
| P1 | `ssh kb@192.168.1.62 true` → **exit 0** | access confirmed by exit code, never by output text |
| P2 | deployed checkout on branch **`transmit-power`** @ **`4ad0a87`** = **ADR 0146** | **12 ADRs behind master.** The host code for 0150/0155/0157/0158 was not on the box. Every item in the brief was unrunnable until it was |
| P3 | `GET /capabilities` had **no `set_modulation`, no `clear_broadcast_fm`** | P2, confirmed from the running process rather than from the checkout |
| P4 | **`docs/BENCH.md` does not exist in this repo** | it is the *fork's* file. Every ADR that names it says "the fork's `BENCH.md`", and this cycle is forbidden to touch the fork |

P2 is the one worth keeping. **A bench cycle's first job is to establish that the thing under test is
the thing that is deployed**, and eleven cycles of "no hardware claim" had quietly made that check
feel unnecessary. It was not: the station had been running ADR 0146 code for five days
(`ActiveEnterTimestamp` = 2026-07-26 21:40:50 UTC) while master moved twelve ADRs ahead of it.

Master was deployed (`git switch --detach origin/master`, `uv sync --extra hardware --extra tts
--extra mumble`, web bundle rebuilt, restart), with the ADR-0146 branch kept as a one-command
rollback. `radio.toml` and `radio-secrets.toml` were confirmed untracked and gitignored first, so
the branch switch could not touch them; their SHA-256s are unchanged from the pre-cycle baseline.

## Decision

This ADR records measurements. It changes no `radio_server/` code and fixes nothing — **a bench
cycle that also changes code cannot tell you which change produced the result.**

### Gate A — the firmware level, measured, not assumed

There is no version command (fork `PROTOCOL.md`: *"There is no version command … So ask the
*command*"*). But master already ships the probe: `AiocBaofeng.__init__` sends a `0x0879` and then a
`0x0877` at every construction (`aioc_baofeng.py:333-334`). **Deploying master and reading the
startup journal *is* the firmware ladder**, with no instrument to write and nothing that can key.

```
INFO: radio_server.backends.uvk5.tuner: uvk5: broadcast FM is off; the station can hear its own channel
INFO: radio_server.backends.uvk5.tuner: uvk5: demodulating FM (raw 0); the radio will key its own PTT path
```

Both frames answered. A `0x087A` reply is F8-only; a `0x0878` reply is F7-or-later; pre-F8 firmware
is silent to the first and pre-F7 to the second. `GET /capabilities` gained `set_modulation` and
`clear_broadcast_fm`, and the dock session announces itself on every establish:

```
INFO: radio_server.backends.uvk5.tuner: uvk5: dock session established; radio reports 'F4HWN v5.7.0'
```

**F8 is on the radio, confirmed end to end over the AIOC.** The version string is the Fusion base
(ADR 0118's `v5.7.0` pin) and does not distinguish F6/F7/F8 — the opcode answers do.

**The repo said otherwise, in two places, and both are corrected in this PR.** `HANDOFF.md:718`
("The radio is not flashed with F7") and `docs/server-notes.md` (F6) were written by cycles that
never ran on hardware, so nothing updated them after the operator's manual flash. They are not
history to be preserved — they are load-bearing claims that were false, and a future session reading
them would re-derive a doubt this cycle has settled.

### The 14 items

Every item carries a measured result or a stated reason it could not run.

#### AM — ADR 0149/0150

**1. `POST /modulation AM` → 200, and the raw reply bytes.** The API leg returned **200** with
`modulation: "AM"`, **`tx_ok: false`**. For the wire itself the service is no help — it logs decoded
fields at INFO and never bytes — so the frames were taken directly off the AIOC with the service
stopped:

| direction | raw frame | inner payload | decode |
|---|---|---|---|
| `0x0877` AM | `abcd0500616415e62f11d5dcba` | — | — |
| → `0x0878` | `abcd08006e6410e62e900c40decadcba` | **`7808040000010100`** | APPLIED, modulation=1, raw=1, flags=**`0x00`** → `AM`, **tx_ok False** |
| `0x0877` FM | `abcd0500616415e62e30c5dcba` | — | — |
| → `0x0878` | `abcd08006e6410e62e910d41decadcba` | **`7808040000000001`** | APPLIED, modulation=0, raw=0, flags=**`0x01`** → `FM`, **tx_ok True** |

**This is exactly what F7 predicted, at the bit.** `FLAG_TX_OK` is clear in AM and set in FM — the
`VFO_STATE_TX_DISABLE` path ADR 0149 traced through `GENERIC_Key_PTT` → `RADIO_PrepareTX`, reported
as a flag rather than removed.

**2. AM audio over the AIOC — the load-bearing item. CONFIRMED, as two separate claims.**

Nothing in this repo had ever shown that AM audio traverses the AIOC at all. It does. The two claims
are kept apart deliberately, because (a) alone rests on an FM carrier envelope-detected and a later
reader would rightly question it.

*(a) A strong FM broadcast station, demodulated in AM.* Proves the demodulator moved and the audio
path carries what it produces. RMS, 4–8 s dwells:

| carrier | AM | FM |
|---|---|---|
| 88.1 MHz | **9653.7 / 8738.8 / 8298.7** | 15224.2 / 13742.1 / 12903.7 |
| 89.7 MHz | **9570.1 / 8675.6** | 12566.9 |
| 98.5 / 105.3 / 106.5 (dead) | **0.0** | 0.0 |

*(b) A true AM transmission.* An audio-RMS sweep of the whole airband, 760 channels at 25 kHz —
**738 of them read exactly 0.0**, so the floor is clean and every hit is a signal:

| MHz | RMS | speech-band ratio |
|---|---|---|
| 133.400 | 13949.3 | 0.971 |
| 126.100 | 13351.5 | **0.983** |
| 124.775 | 12964.0 | 0.982 |
| 132.225 | 12351.6 | 0.962 |
| 124.000 | 9459.1 | 0.949 |
| 135.400 | 8724.6 | 0.962 |

Speech-band ratios of 0.95–0.98 are voice, not noise. And the **reciprocal control** is the one that
settles it — same frequency, adjacent dwells, only the demodulator changed:

```
AM  124.7750    rms 7405.7   speech 0.372
FM  124.7750    rms    0.0   speech 0.000
```

An AM transmission yields audio under AM demodulation and **nothing** under FM. `0x0877` moves a real
demodulator on real hardware, and AM audio reaches the host.

**3. `tx_ok false` locks out TX, and the witness reads no RF.** Run on **445.800** with the kv4p
witness in band. The band move is recorded plainly: the witness is a UHF-only SA818 and the station
was found on 147.555, so a witness aimed at the station would have read "no RF" whether or not the
radio keyed — **that is not weak evidence, it is no evidence** (`server-notes.md:76-79`). Positive
control first; an absence counts only because the same setup had just produced a presence:

| | modulation | tx_ok | `POST /ptt` | witness busy | witness RMS |
|---|---|---|---|---|---|
| positive control | FM | true | **200** | **20/29 (69%)** | **1298.5** |
| **item 3** | AM | **false** | **503** | **0/33 (0%)** | **0.0** |
| **item 4** | FM | true | **200** | **20/30 (67%)** | **1275.6** |

The 503 carried the AM reason verbatim:

> the radio is demodulating AM and refuses its own PTT path (VFO_STATE_TX_DISABLE — this firmware is
> built without ENABLE_TX_WHEN_AM, so AM is receive-only). Set modulation FM to transmit.

**4. Back to FM — the arc did not cost the station.** `tx_ok` true, a witnessed carrier at
essentially the level of the pre-AM control (1275.6 vs 1298.5; 67% vs 69%).

**The repeater leg was deliberately not keyed.** [ADR 0133](0133-repeater-split-and-chirp-import.md) is standing —
keying a repeater's real uplink is the operator's business, never the runner's — and this runner is
unattended. The electrical claim item 4 rests on is proven above into the bench setup; the repeater
confirmation is an operator action and is recorded as not run, not as passed.

#### Broadcast FM — ADR 0156/0157/0158

**5. `0x0879` ON — and it is the dangerous combination, on hardware.**

```
payload 7a0808000001e07d37060001
  → APPLIED, state=1 (ON), raw_hz=104300000, band=0, flags=0x01
  → on=True  tx_ok=True
```

`on=True` **and** `tx_ok=True` off one frame, exactly as ADR 0156 argued must be representable
because it is real: the BK1080 deafens the station and the BK4819 demodulator it does not touch goes
on reporting that the radio will key. Time-to-first-byte 0.401 s.

The **three blanking sentinels** of ADR 0156 are confirmed field by field. A read probe against a
switched-off receiver:

```
payload 7a08080009ff00000000ff00
  → ERR_OFF, state=0xff, raw_hz=0, band=0xff
```

`state` and `band` blank to `0xFF` (because `0` is a real reading of both) and `freq_hz` to `0`. The
same probe with the receiver running answers **`ERR_BAND`** instead of `ERR_OFF`, so the two refusals
distinguish the state even though the `state` byte itself is blanked.

**The OFF leg's flash stall, measured — a `⚠ CONFIRM AT BENCH` item whose only source was a firmware
comment.** Time-to-first-byte was **0.100 s on the flash-writing OFF leg and 0.100 s on a second,
already-off OFF leg** — identical, and the same as the read probe. No multi-second erase stall
appears on the wire at this resolution. (0.100 s is at the reader's poll granularity, so the honest
statement is **≤ ~0.1 s**, not a precise figure.) Both OFF legs returned byte-identical replies,
consistent with the `memcmp` short-circuit that makes clearing an already-off receiver write no flash.

**Broadcast FM audio DOES reach the AIOC — the fork's `BENCH.md` §8 placeholder, answered.** Taken at
the sound card with `arecord -D hw:CARD=AIOC_K6` and the service stopped, so nothing in radio-server
is between the BK1080 and the number, and bracketed by controls on *both* sides:

| BK1080 | RMS | peak |
|---|---|---|
| OFF (control A) | **107.1** | 144 |
| **ON** | **3308.5** | 32768 |
| **ON** (repeat) | **3028.7** | 32768 |
| OFF (control B) | **108.7** | 1360 |

~30× the floor, reproduced, and returning to the floor when cleared. **The relay half is NOT
established**: whether radio-server's RX pump forwards that audio to `/audio/rx` could not be
measured, because the only way to have broadcast FM on *while the service runs* is the front panel,
and a 12-minute host-side capture could not be proven to overlap the operator's keypresses. One trip
to the radio settles it; recorded as not run rather than inferred.

**6. The front panel — operator observation, photographed.** Four screens, transcribed as shown:

| after | screen |
|---|---|
| `F` then `0` | `MO CL 100%` · **`FM`** · **`104.3`** · `VFO` · **`87.5-108M`** |
| keys `1`,`4`,`7` | `FM` · **`147.-`** · `VFO` · `87.5-108M` |
| `M` | `FM` · **`CH-01`** · **`SAVE?`** · `87.5-108M` |
| `EXIT` | **`445.800`** · `FM  H  W  SQL1` · `12.50K` · `VFO` |

Three things fall out of these, and one contradicts the arc:

- **`F`+`0` does enter broadcast FM**, and it came up on **104.3** — the exact frequency the
  instrument had left the BK1080 tuned to. That independently corroborates that the host's
  `broadcast_fm.hz = 104300000` is a genuine read-back off the radio.
- **The keypad is NOT locked. It is *repurposed*.** ADR 0156 justified making the screen follow the
  radio partly on "the dead keypad", and the keypad is not dead: digits go into **direct frequency
  entry on the broadcast band** (`147.-` being typed under an `87.5-108M` band line), and `M` opens a
  save-to-channel prompt. This is arguably worse than dead — an operator typing a frequency believes
  they are moving the station and are moving a *broadcast receiver*. The premise should be corrected
  where it is relied on.
- **`EXIT` restored `445.800`** with its mode, power, bandwidth, squelch and step intact — item 7's
  channel-restore half, at the front panel, with no restart.

**7. OFF restores the channel and its audio.** The channel half is confirmed at the front panel
(above): `EXIT` brought back `445.800 / FM / H / W / SQL1 / 12.50K`, no restart. The audio half is
confirmed too: after the broadcast-FM
excursion the station hears again, and better than at any other point in the session —
**FM 88.1 MHz, RMS 21730.2, speech-band 0.975**. The F3a mirror-image fault that
`Dock_RestoreFmAudio` exists to prevent did **not** occur; `RADIO_SetupRegisters` muting a running
BK1080 did not leave the audio path dead. The "without a restart" half is an operator observation
(there is no host OFF route — see finding 3).

**8 and 9 — NOT RUN, and the reason is the finding.**

Item 8 asks for a key attempt refused by the host with the broadcast-FM reason; item 9 asks that
clearing FM at the panel leaves the host still refusing. Neither can be run on this station, and
tracing why is worth more than either would have been.

`refuse_if_deafened` refuses only on a definitive `on=True`. The server's `broadcast_fm` block is
written in exactly one place — `AiocBaofeng.__init__` — and **the boot assert immediately before it
clears the very condition the gate tests.** So:

- **Boot with the radio deaf** → the assert clears it, block records `on=false`, gate silent.
  Measured (item 11 below).
- **FM switched on at the panel after boot** → nothing re-reads the block. It stays `on=false`, and
  `tx_ok` reports the BK4819 as `true`. **The gate does not fire.** A key attempt here is not refused
  — it transmits blind into a channel the station cannot hear.

The only remaining path to `on=True` is a firmware that answers APPLIED while reporting the receiver
still running (`tuner.py:316`, the "radio REFUSED to leave broadcast FM" WARNING) — a malfunction,
not an operating state.

So item 8's key attempt was **not performed**. The brief forbids keying while broadcast FM is on with
the host gate bypassed; the gate here is not bypassed but *blind*, and the radiated result is
identical — a blind carrier on air. **Verifying that the host refuses was the test, and the measured
answer is that it does not refuse, because it cannot see.** Item 9's latch could not be observed
because the latch never engages.

This confirms ADR 0158's own carried finding 1 — *"the host gate cannot see the front panel. F+0 on
the radio leaves the station deaf with a live Talk button and this gate silent"* — on hardware, and
without putting a carrier on air to do it. **F+0 is now measured to do exactly what that finding
says** (item 6 above), so the sentence is no longer a prediction.

**One claim deliberately NOT made.** A 12-minute host-side capture ran across the operator's trip to
the radio and recorded `broadcast_fm: {on:false}` and audio RMS `0.0` on all 123 samples. That is
consistent with host blindness — but the keypresses could not be proven to fall inside the window,
so it is **not** cited as evidence. The blindness conclusion rests on the code path, which is not
timing-dependent.

#### Startup and switching — ADR 0155/0157

**10. AM left on the radio across a restart: the server states, it does not adopt.** Before the
restart, `modulation: "AM", tx_ok: false`. After:

```
INFO: uvk5: demodulating FM (raw 0); the radio will key its own PTT path
/status → {"modulation": "FM", "tx_ok": true}
```

ADR 0155's "state it, never adopt it" holds against real sticky session state, and `tx_ok` moved
false→true as a *measured* consequence rather than a remembered one.

**11. The boot-deaf residual — ADR 0157's assert works against a genuinely deaf radio.** The radio
was put into broadcast FM by the instrument and verified there (`state=1`, 104.3 MHz) with the
service stopped; then the service was started:

```
INFO: uvk5: broadcast FM is off; the station can hear its own channel
/status → broadcast_fm {"on": false, "hz": 104300000}
```

The `hz` field is corroborating evidence, not decoration: on the earlier clean boot it read
`64000000` (the band's low limit, never tuned) and here it reads `104300000` — the frequency the
receiver actually *was* on. The block is read back off the radio, not fabricated.

**12. `POST /radio/select` timing — the claim holds weakly and fails strongly.**

| switch | duration | `/status` codes during | max `/status` latency | largest sample gap |
|---|---|---|---|---|
| baofeng → uvk5 | **10.473 s** | all **200** (14/14) | **9.752 s** | 9.802 s |
| uvk5 → baofeng | **7.429 s** | all **200** (4/4) | **7.120 s** | 7.170 s |

`asyncio.to_thread` (`holder.py:274`) does keep the server *answering*: no 503, no refused
connection, no dropped request across two switches. But a `/status` that lands inside the switch
window **blocks for essentially the whole switch** — 9.75 s of a 10.47 s rebuild. So "it no longer
blocks" is true if it means "no longer fails" and **false** if it means "answers promptly". A browser
polling `/status` during a backend switch will hang, not error.

The switch also corroborates the capability split (guardrail 3): `uvk5` advertised `scan` and
dropped `set_modulation`/`clear_broadcast_fm`; `baofeng` got them back.

#### Carried findings

**13. `POST /mode {"mode":"AM"}` → HTTP 500**, body `Internal Server Error`. Confirmed empirically.
`/mode` is bandwidth (wide/narrow) and `/modulation` is demodulation; the former has no AM to give
and fails with a 500 rather than a 422.

**14. The preset highlight lies — confirmed on live payloads.** `activePresetName`
(`web/src/components/PresetControl.jsx:27-39`) compares frequency, mode, tone and `tx_frequency`.
It does not compare modulation. Applying "Bench Simplex 445.800" and then setting AM by hand:

| | frequency | mode | tone | tx_frequency | modulation | tx_ok |
|---|---|---|---|---|---|---|
| after apply | 445800000 | FM | null | null | FM | true |
| after AM | 445800000 | FM | null | null | **AM** | **false** |

Every field the highlight compares is identical, so the preset stays highlighted while the radio
demodulates AM with its transmitter disabled. Sharper than ADR 0154 predicted: that preset's own
`honoured` list contains **`"set_modulation"`** — the server tells the browser modulation is part of
what this preset applies, and the highlight ignores it. And the `/presets` payload carries no
`modulation` key at all, so **the fix is not a UI-only change**: the field has to reach the browser
before the browser can compare it.

### The station afterwards

- **`acceptance.py`: 9 of 9 attempted stages PASS** — systemd, web, presets, rx, dtmf, auth, **tx**,
  split, services (the `services` stage keyed a real announcement: 5.8 s, kv4p RMS 3001, speech-band
  0.98). Exit code **1**, entirely from `split-minus`, which **could not be attempted** — `radio.toml`
  has no `Bench Split Minus` preset — and a skipped run exits non-zero by design. That gap predates
  this cycle.
- **`uv run pytest`: 2043 passed, 5 skipped** — identical to master, as it must be, since no
  `radio_server/` code changed. A first run in a bare worktree reported *4 failed*; those four are
  **pre-existing on pristine `origin/master` in the same environment** (verified in a second
  worktree) and are `ModuleNotFoundError: No module named 'opuslib'` — the `kv4p` extra missing.
  Recorded because "4 failed" would otherwise read as a regression. Note `update-radio-server.sh`
  names `hardware`, `tts` and `mumble` but **not** `kv4p`.
- **Restored:** station back on **147.555** (the frequency it was *found* on, not the 445.800
  `server-notes.md` claimed), modulation FM, `tx_ok` true, broadcast FM verified **off**, both units
  active, `radio-secrets.toml` byte-identical to the pre-cycle baseline.
- **`radio.toml` did NOT come back byte-identical, and the cause is a finding.** `POST /radio/select`
  calls `save_settings()` (`api/app.py:1763`), so item 12's backend switch **rewrote the deployment's
  config file**. The delta against the closest pre-cycle snapshot is exactly two lines —
  `uvk5_tune_persist = true` and `uvk5_power = "high"` — previously-implicit defaults materialised at
  their in-effect values. Semantically a no-op; all operative keys, all **41** presets, both
  `[dstar]`/`[dvap]` blocks and all **203** comment lines survive. But a bench cycle that switches
  backends mutates a hand-annotated config file, and nothing warns you.
- **Residue, stated:** the BK1080 is left tuned to **104.3 MHz** (it read `64000000`, the band's low
  limit, on the first clean boot). It is **off**, which is what matters, but it is not where it was.

## Consequences

- **The AM arc is real.** ADRs 0149/0150/0151/0153/0154/0155 were reasoned entirely against a mock;
  every load-bearing claim they made about hardware — the `tx_ok` flag, the PTT refusal, the sticky
  session state, the boot assert — is now measured.
- **The broadcast-FM arc is half real.** The firmware side (0x0879/0x087A, the sentinels, the
  dangerous `on=True`/`tx_ok=True` pair, the OFF leg) is confirmed. The *interlock* is not, because
  it cannot fire from the only path that creates the state at runtime.
- **`server-notes.md` was wrong three times in one day** — firmware level, operating frequency
  (says 445.800, radio was on 147.555) and D-STAR (says disabled; `/status` reports
  `configured: true` with 27 076 RX frames). Corrected in this PR with the measurements above.

### Findings carried forward

1. **The deployed checkout drifted 12 ADRs behind master and nothing noticed.** No cycle checks it,
   and eleven cycles of "no hardware claim" removed the pressure to look. A deploy-state check
   belongs in `acceptance.py` or the cycle contract, not in a human's memory.
2. **`status.rssi` is `null` on the `baofeng` backend — there is no host-side signal-strength read on
   the deployed operating mode at all.** It is a `uvk5`-dock field with no source in
   `aioc_baofeng.py`. This is why the airband hunt had to be done by audio RMS rather than RSSI, and
   why `server-notes.md`'s advice to check `/status | jq .rssi` first does not apply to this station.
   A real gap, not a quirk of this cycle.
3. **There is no host route to clear broadcast FM.** `CLEAR_BROADCAST_FM` is advertised and the
   capability is earned, but the only caller is the boot assert — so the documented operator remedy
   ("press EXIT, or power-cycle it") is the *only* remedy, and the ADR 0158 refusal message says to
   restart the server precisely because of this. ADR 0158's finding 3 predicted the shape; this is it
   from the operating side.
4. **The interlock's unreachability (items 8/9 above) is the successor's real brief.** ADR 0158's
   finding 2 named the latch as the problem; the measured problem is one layer earlier — the gate
   never engages, so there is nothing to un-latch. A pre-key-up re-read (0157's R2) would fix both at
   once, and now has a hardware cost: one `0x0879` round trip, measured at **≤ ~0.1 s**, not the
   3.0 s the ADR feared for firmware that cannot answer.
5. **`/presets` does not carry `modulation`**, so finding 14's fix spans server and browser.
6. **A `/tuning/persist` sweep costs real flash.** 97 tunes were made with `tune_persist` still on
   before it was turned off — each one an EEPROM write plus the radio's six-second transmit lockout,
   visible in the journal as `tuned … and stored it`. Self-inflicted and recorded; a bench sweep
   should turn persistence off *first*.
7. **`POST /radio/select` persists to `radio.toml`.** Not documented anywhere a bench operator would
   look, and it means switching backends to time a switch has a config side effect. Either say so in
   `docs/api.md` or stop writing the file on a live switch.
8. **ADR 0156's "dead keypad" premise is wrong.** The keypad is live in broadcast FM and *repurposed*
   — digits tune the BK1080, `M` saves a broadcast channel. Anywhere that reasoning is relied on
   should be corrected, and the operator-facing risk restated: typing a frequency moves the wrong
   receiver.
9. **`docs/BENCH.md` does not exist in this repo and was not created.** It is the fork's file; a
   same-named twin here would guarantee exactly the cross-repo confusion ADR 0148 is about. The
   fork-side placeholders this cycle settles are listed in the PR body for a follow-up cycle to
   apply, because this cycle is forbidden to touch the fork.

## Out of scope

- **No `radio_server/` code changed**, nothing was fixed, and no firmware was built or flashed.
- **The fork was not touched** — not even a read-modify-write. Another session holds `PROTOCOL.md`.
- `scripts/bench/broadcast_fm_on.py` is the one addition: a test instrument for two frames
  `PROTOCOL.md` already defines and the production codec deliberately cannot emit (`ClearBroadcastFm`
  builds OFF only, by design). No production path reaches it. The "no new code" rule bars *fixes*
  during a bench run — a repair mid-cycle makes it impossible to tell which change produced a result
  — and an instrument that only asks questions is not that. It opens the port through the transport's
  own `_default_serial_factory`, because a plain `serial.Serial(...)` asserts DTR and **DTR is this
  cable's PTT line**.
