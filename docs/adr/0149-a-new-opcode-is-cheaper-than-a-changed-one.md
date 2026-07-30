# 0149 — A new opcode is cheaper than a changed one

Status: Accepted

Extends the dock wire protocol established in ADR [0119](0119-uvk5-v3-dock-protocol-port.md) and
[0140](0140-the-first-key-is-always-lost.md), and specified for outside readers by ADR
[0148](0148-the-firmware-is-a-product-too.md). Firmware only — no code under `radio_server/` changes
this cycle.

## Context

The dock protocol can tune the radio and cannot change its demodulator. Every path is FM, in two
places and by hand: `App/app/uart.c`'s `Dock_ApplyVfo` wrote `vfo->Modulation = MODULATION_FM` on
every `0x0873`, and `radio_server/backends/uvk5/vfo.py:339` packs `_MODULATION_FM` into the EEPROM
channel image. So AM is unreachable from the server — not gated, not degraded, absent — and the
radio's own AM receive capability is invisible to the API.

Adding it is a wire change, which is the part worth deciding carefully rather than the part worth
typing carefully.

### The opcode was not free, and that is not a new problem

The obvious allocation is `0x0875`/`0x0876`. It is taken. **ADR [0111](0111-uvk5-dock-transport-and-control-path.md):52**,
describing nicsure's classic Dock, records the extended set reachable inside the `0x0870` loop as:

> `0x0872` modulation, **`0x0873/4` backlight, `0x0875/6` AM emulation**

Two things follow, and the second is worse than the first.

**`0x0875/6` is claimed.** nicsure's source is not vendored in this project, so that census cannot be
re-verified here; per guardrail 1 it is therefore treated as **claimed**, not as free-until-proven.
The practical exposure is narrow — `frames.py` is still a dual-target codec pinned to classic
`0.32.21q` (`doctor.py:93`), and the classic extras live inside full control where this fork's
command answers `ERR_BUSY` — but the cost of avoiding it is zero and the cost of being wrong is a
wire change after shipping.

**`0x0873/4` was claimed too, and we took it anyway.** ADR 0140 chose `0x0873` on the reasoning
*"`0x0873` not `0x0872` (the latter is stock `CMD_0872_t`)"*. It checked one opcode. The same census
that would have flagged `0x0875/6` had already recorded `0x0873/4` as the classic's backlight pair,
and nobody read it. That allocation is now in a cut release, on hardware, in `PROTOCOL.md`, and in
186 measured tunes. **It cannot be walked back.**

The cause is the one ADR [0147](0147-the-docs-drifted.md) named, one level up: the opcode census
exists in **three** places that do not agree — `ADR 0111:52`, `ADR 0119:46`, and
`frames.py:113-140` — and nothing reconciles them. A doc that drifts is embarrassing; a wire opcode
that drifts is permanent.

### Three facts read out of the firmware, not assumed

Verified against the tree at `27adbce`, and two of them correct guidance this project was carrying:

1. **The modulation enum's numeric end moves with a build flag.** `ModulationMode_t` is
   `{ FM, AM, USB, [BYP, RAW,] UKNOWN }`, the bracketed pair behind
   `ENABLE_BYP_RAW_DEMODULATORS` — `false` in the `default` preset `Fusion` inherits. So
   `MODULATION_UKNOWN` is 3 or 5 depending on how you built. A wire value derived from that enum
   means different things in two builds of the same protocol.
2. **`RADIO_SetupAGC` takes two arguments** (`listeningAM`, `disable`), memoises on a
   `static lastSettings`, and returns early on a repeat — so a hand-rolled call is a silent no-op
   whenever the state already matches. `RADIO_SetModulation` already calls it.
3. **`0x0873`'s payload is 13 bytes**, not 12. Twelve is the `0x0874` reply.

### A successful set-AM stops the transmitter

`App/radio.c`:

```c
#ifndef ENABLE_TX_WHEN_AM
    else if (gCurrentVfo->Modulation != MODULATION_FM) {
        State = VFO_STATE_TX_DISABLE;
    }
#endif
```

`ENABLE_TX_WHEN_AM` is `false` in the preset `Fusion` inherits. That refusal sits in
`RADIO_PrepareTX`, and the call chain into it was traced rather than assumed:
`GENERIC_Key_PTT` → `generic.c:192 gFlagPrepareTX = true` → `app.c:2612 RADIO_PrepareTX()`. **That is
the path the radio's PTT pin drives** — which is where the `baofeng` backend keys from, by asserting
the AIOC's DTR line into that pin (`[baofeng] ptt_line = "dtr"`, bench-confirmed). So on that station
a successful set-AM **stops the transmitter outright**.

The dock's own `Dock_ForceTx` REG_30 keying never enters `RADIO_PrepareTX` and is unaffected. The
same firmware state therefore means *"cannot transmit"* on one backend and *"transmits normally"* on
another, and a host cannot see a build flag from the far end of a serial cable.

## Decision

**A new opcode — `0x0877` set-modulation, `0x0878` reply — rather than a field appended to `0x0873`.**

A field on `0x0873` changes the bytes of a frame radio-server already sends, and it breaks in **both**
directions, **silently**:

- *Old host, new firmware:* the 13-byte frame is refused `DOCK_VFO_ERR_SHORT`, which the host reads
  as a tuning failure rather than a version mismatch.
- *New host, old firmware:* the 14-byte frame decodes cleanly, the extra byte is ignored, and the
  radio tunes on a modulation nobody set.

Nothing would catch either. ADR 0148 closed by naming exactly this gap — *"the two repositories can
now drift from each other, and nothing checks that"* — and this cycle does not close it either; it
declines to widen it.

A new opcode is additive in both directions. An old host never sends it. A new host sending it to a
pre-F7 firmware falls through `dock_dispatch`'s `default:` to silence, which is the same signal the
F-level probe already reads. **An empty `0x0877` becomes the F7 probe**, safe by the same construction
ADR 0148 established for `0x0873`: the length check is the *first* branch, so the frame is refused
before a field is decoded, before the binding is called, and with the reply naming no modulation.

**`0x0877/0x0878`, not `0x0875/0x0876`** — checked against the census before allocating, because
`0x0873` is the standing proof of what skipping that check costs.

### The modulation is sticky for the session, and `0x0873` applies it

`0x0873` must write *some* modulation into the VFO it applies. It wrote FM literally, so "set AM,
then tune" put the radio back on FM with nothing on the wire saying so — the fault class `0x0874`
exists to kill, reappearing in the one field `0x0874` does not report.

The repair is not "send the two commands in the right order." **ADR
[0131](0131-dock-link-robustness.md) established that this link drops frames**: the firmware is
single-threaded and anything arriving while it is busy is discarded, not queued. Two frames means a
real failure mode, not an inconvenient one — the tune lands, the set-modulation is dropped, and the
radio sits on the right channel in the wrong demodulator, reporting success. ADR 0131 also already
ruled out the general fix (an ack/retry layer breaks the byte-compatibility invariant the fork rests
on).

So `dock_ctx_t` carries the modulation and `0x0873` applies it. One successful `0x0877` and every
later tune keeps it; a dropped frame becomes a retryable `0x0877` instead of a mis-configured radio.
Three properties make it safe:

- **Seeded from a constant, never from the radio.** Reading the radio's current modulation into the
  context would be the "adopt whatever state you find" fault ADR
  [0132](0132-dock-band-and-the-register-model.md) removed, and it would tune a repeater channel in
  whatever the front panel was last left on. Seeded FM — so a host that never sends `0x0877` gets
  behaviour byte-identical to F6, which is asserted as a test rather than argued.
- **Only a modulation the radio actually took is committed.** A refusal — `ERR_BUSY` above all —
  must not move it. Committing during decode would let a *refused* request become what every later
  tune applies: the same bug in mirror image, and invisible precisely because the refusal itself was
  reported correctly.
- **It is session state, not connection state.** `Dock_EnsureInit` runs once per radio power cycle,
  so it outlives a host process. `PROTOCOL.md` states as a requirement on the host — not a note —
  that a reconnecting client asserts the modulation it wants rather than assuming FM.

`0x0873`'s **wire bytes are untouched**. The only change to that path is one line: a literal became a
table lookup whose default is that literal.

### The wire names its own values

`DOCK_MOD_FM 0`, `DOCK_MOD_AM 1`, and `DOCK_MOD_USB 2` **reserved but refused**; anything above is
refused. Not `ModulationMode_t`, for fact 1 above and because `dock.c` may not include a firmware
header (guardrail 4). `uart.c` maps both ways: in, a designated-initialiser table with a
`_Static_assert` that it covers exactly the accepted values; out, a `switch` with a `default`, because
an array indexed by that enum reads off its own end on exactly the build where the extra members
exist. This is the `DOCK_POWER_MAP` lesson — wire "high" landing on `OUTPUT_POWER_LOW2` — applied
before it bites rather than after.

USB is refused rather than accepted because nobody has put this radio on USB at a bench, it cannot
transmit in it on this build anyway, and a refusal never moves the radio. Fixing the *number* costs
nothing and accepting the *value* later is purely additive. Shipping it now would be a confident
answer nobody has checked (guardrail 1).

### `0x0878` reports what the radio is on, and whether it can still transmit

Four bytes: `status`, `modulation` read back out of the radio's VFO after applying, `raw` (the
radio's own enum value, diagnostic only), and `flags`. Status codes reuse `0x0874`'s numbers **holes
and all** — 3, 6 and 7 cannot arise and are left unused — so one table decodes both commands.

**On any non-zero status, `modulation` and `raw` are `0xFF`, not `0`.** `0x0874` blanks its
frequencies to zero because 0 Hz is obviously not a channel. Copying that here would blank modulation
to `0`, which **is FM** — a refusal shipping a plausible claim about where the radio is. Same
principle, different sentinel, and the one place a literal copy of the existing code would have
produced a shipped bug. `0xFF` doubles as "on something this wire cannot name".

**`flags` bit 0 reports whether the radio will key its own transmit path.** Per the finding above,
that is the difference between a working station and a dead transmitter on the deployed `baofeng`
configuration, it is decided by a build flag the host cannot see, and it is invisible until somebody
notices nothing is going out. `0x0874` exists because the wire's power scale and the radio's silently
disagreed; this is the same doctrine applied to a build flag.

**`ENABLE_TX_WHEN_AM` stays off. AM is receive-only.** The flag *reports* the condition; it does not
remove it, and this cycle does not remove it. Enabling AM transmit is not a thing this fork does.

## Acceptance

**Fail-first, run against the bug, twice — because there were two bugs to fail against.**

- Against a stub whose `0x0877` case echoes the request, always reports `APPLIED`, and commits the
  sticky value unconditionally: **98 checks, 15 failures**.
- Against a build with the whole command implemented correctly but `0x0873` keeping its
  `MODULATION_FM` hardcode: **98 checks, 2 failures** — precisely the two stickiness cases, and
  nothing else. That second run is the one that matters: it fails only the thing it is supposed to
  fail, so a green result on the real build means something.
- Restored: **98 checks, 0 failures** (66 before this cycle).

**The `0x0873`-is-untouched gate is a test, not an assurance.** `test_dock.c`'s byte-exact `0x0874`
golden vector must stay green; if the wire moved, it goes red.

**Golden vectors have an independent oracle.** Both new frames were generated by a separately written
framer implemented from `PROTOCOL.md`'s spec, validated by reproducing the two published vectors
(`0x0951` and `0x0874`) byte-for-byte first. A vector produced by the implementation it is meant to
check is a transcript, not an oracle — ADR 0148's point, applied to its own artifact.

**Two tests exist to say green is not evidence.** Test 31 ("reaches the binding exactly once") and
test 33 ("F6 backward compatibility") stay green against the always-succeeds stub. They discriminate
against different wrong implementations — a binding never called or called twice, and a wrong
`dock_init` seed — and their comments say so, so nobody later cites them as proof the refusals work.

**The six-second TX lockout is not armed**, confirmed rather than assumed:
`gSerialConfigCountDown_500ms` is set at exactly four sites (`CMD_0514`, `CMD_051B`, `CMD_051D`,
`CMD_052F`), none reachable from the dock dispatch, and it appears nowhere else in `App/` but its
declaration, definition, the scheduler decrement, and `SerialConfigInProgress()`. The only
post-switch statement in `UART_HandleCommand` is `gUART_LockK5Viewer`, which suppresses telemetry.

**The image builds clean: FLASH 105,576 B of 118 KB (87.37%)**, +240 B over F6's 105,336, 15,256 B
headroom. RAM unchanged at 13,088 B (79.88%).

`uv run pytest` 1884 passed / 5 skipped — no `radio_server/` code changed.

## Consequences

- The radio can be put on AM from the server, once the host side is built.
- **A tune can no longer silently change the demodulator**, which was true before anyone could set
  one and would have become a live bug the moment they could.
- The F-level probe gains a step, so *"why won't it receive AM"* has a one-line answer.
- **A host that sends nothing new sees nothing new.** F6 behaviour is preserved by construction and
  asserted by test.
- **`frames.py` and the firmware are now further apart than they have ever been**, and still nothing
  checks it. The host side is a later cycle, and it inherits a name collision:
  `DockCommand.SET_MODULATION = 0x0872` and `class SetModulation` already exist for the classic
  Dock's never-dispatched command, so one of the two must be renamed.
- **The opcode census is still in three places and still does not agree.** This cycle read it and
  allocated around it; it did not reconcile it. The next allocation is one incomplete check away from
  repeating `0x0873`.

## Out of scope

- **The host side entirely** — `frames.py`, the `SetModulation` rename, a `Capability`, an endpoint,
  the UI. Its own cycle, against a firmware that exists.
- **`vfo.py:339`'s `_MODULATION_FM` hardcode.** The `eeprom` tuner has the same defect the firmware
  had, in the other repository, on a path that does not use `0x0877` at all.
- **Reconciling the opcode census** into one source, per Consequences. It wants nicsure's source read
  and one table to own the answer.
- **Enabling `ENABLE_TX_WHEN_AM`.** Deliberately not done.
- **Any hardware claim.** The firmware is **not flashed** and nothing has been measured. AM audio
  over the AIOC and the PTT refusal are both new `⚠ CONFIRM AT BENCH` items in `BENCH.md`, left as
  placeholders (guardrail 1). The F7 pre-release says so in its `PROVENANCE.md` rather than carrying
  F6's measured-on-hardware paragraph forward.
