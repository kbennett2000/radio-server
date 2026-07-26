# 0144 — Instant and persistent

Status: Accepted; **its lockout arming rule is corrected by [ADR 0145](0145-instant-by-default.md)**

Builds on ADR [0142](0142-the-server-picks-the-repeater.md) and [0143](0143-a-tune-must-know-it-has-a-session.md).
Corrects a false statement in 0142's tuner docstring.

## Context

A channel change took **~14 seconds**, and almost all of it was a soft reset whose only job was
making the firmware *load* the record just written to EEPROM.

F6 is now confirmed on the bench radio — `0x0873` answered with a matching `0x0874` read-back — so
the reset is avoidable. `Dock_SetVfo` (`App/app/uart.c`) ends with:

```c
RADIO_SelectVfos();
RADIO_SetupRegisters(true);
```

The synthesiser is retuned in place. But it writes `gEeprom.VfoInfo[]` — **RAM**. Switch the radio
off and the channel is gone.

### The enabling fact, read out of the firmware rather than assumed

`gSerialConfigCountDown_500ms = 12` (six seconds) is armed at exactly four sites: `uart.c:355`
(HELLO), `393` (EEPROM **read**), `447` (EEPROM write), `586` (`CMD_052F`). **The dock opcodes do
not arm it.** `SerialConfigInProgress()` both refuses a key-up and *terminates an over already in
progress* (`app/app.c:1111, 1146, 1191`), so this is not advisory.

So `0x0873` is free and EEPROM traffic costs six seconds of mute. The two mechanisms fail in
opposite directions, and the operator needs neither failure.

### A correction to ADR 0142

Its tuner docstring said *"a `setvfo` tune does not survive the operator power-cycling the radio, so
the server re-applies on connect."* **That re-apply does not exist** — `apply_preset` has one call
site, the HTTP route. It described behaviour that was never built. Persistence here comes from
storage, not from a re-apply that does not run.

## Decision

**`HybridTuner` — `0x0873` for the radio, EEPROM for tomorrow.** `apply()` is two steps and no
reboot: set the VFO (RF follows in milliseconds, confirmed by the `0x0874` read-back of the radio's
own state), then write the same record to storage so it survives. The reset disappears because
`0x0873` has already done the only thing it achieved.

RF goes first deliberately: it fails fast on pre-F6 firmware, and finding that out *before*
rewriting storage is the difference between a refusal and a half-migrated radio.

**The lockout is published, not slept through.** Blocking six seconds inside a tune would throw away
the instant part for a listener who is not about to transmit. `HybridTuner` exposes `tx_ready_at`;
`AiocBaofeng._key_on()` waits out whatever remains before the line goes high. The wait did not
disappear — it moved to the only caller that cares. Returning without doing *either* is exactly what
ADR 0142 got wrong.

Re-selecting a channel the radio already stores writes nothing, so it costs neither flash nor
lockout: measured at **7.6 ms**.

**The UI stops inviting a press that will do nothing.** `RadioStatus.tx_ready_in` (relative, never
an absolute deadline that could go stale into a lie) and a `TalkControl` button that disables itself
with a countdown ticking locally — nothing pushes another status event while the radio sits muted,
so a button waiting for permission would stay dead. Mumble and D-STAR are untouched: a muted UV-K5
says nothing about a path that never keys RF. The server enforces the wait regardless; a browser
must never be the thing keeping RF correct.

**`POST /diagnostics/reboot-radio`** is a narrow bench affordance — a reset, not a tune, refused
mid-transmission and 501 without a tuner. It exists because no unattended test can otherwise reach
the one state the persistence claim is about.

## Consequences

| | before (`eeprom`) | after (`hybrid`) |
|---|---|---|
| channel change | ~14 s | **1.1 s** |
| re-selecting the same channel | ~14 s | **7.6 ms** |
| listening | after the reboot | immediately |
| transmitting | after the reboot | after ~6.5 s, shown and counted down |
| survives a power cycle | yes | yes |

`eeprom` is unchanged and remains the only mode that works on stock firmware. Default stays `off`.

## The gate caught itself, which is the point

The persistence row was written to fail under `setvfo` — a tuner that writes RAM cannot persist
anything. Run against `setvfo`, **it passed 2/2**.

It passed because the radio's storage already held that channel from an earlier hybrid run. "The
tune persisted" and "the radio happened to already be there" produced identical evidence. A
single-channel persistence test cannot tell those apart, ever, and this one had been about to be
reported as proof.

So the row now runs two phases: move the radio to a decoy channel and verify *that* survived a
reboot, then move it to the test channel and verify it survived **and left the decoy**. A mode that
cannot persist fails phase 1; if stale storage happens to be the decoy, phase 2 catches it instead.
Something has to actually be written for both to pass.

This is ADR 0143's rule doing its job one level up: a gate that cannot fail the way the claim could
fail is not a gate — and checking is not optional just because the gate was written with the failure
in mind.

## Acceptance — measured on hardware

| Gate | Mode | Result |
|---|---|---|
| `tune_follows_preset.py` — 8 carrier/silence/RX rows | hybrid | **16/16** — proves RF follows `0x0873`, which the frame-level probe did not |
| persistence, 3 rows × 2 | hybrid | **6/6** |
| persistence, same rows | **setvfo** | **0/6**, carrier 0.00 s — the radio reverted to stale storage |
| `tune_survives_a_reboot.py` cold + recovery | eeprom | **4/4** — no regression from extracting `write_channel` |
| apply latency | hybrid | 1.06–1.18 s; 7.6 ms unchanged |

`uv run pytest` 1828 passed / 5 skipped; `npm test` 58 passed.

## Out of scope

Re-asserting the channel immediately before every key-up — worth having for pure `setvfo`, but
hybrid persists, so it buys nothing here, and putting a dock round-trip inside the RF key path
(ADR 0093/0102 territory) is not a change to make without a reason.

**2 m TX remains unverified**: 36 of 38 repeater presets are 145/144 MHz and the witness is
UHF-only. The 147.555 tune lands and reads back, but no carrier has been measured on 2 m.
