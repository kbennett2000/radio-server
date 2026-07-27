# 0148 — The firmware is a product too

Status: Accepted

Follows the documentation audit in ADR [0147](0147-the-docs-drifted.md). Corrects the firmware
guidance in `docs/uvk5-setup.md`, which predates ADR [0118](0118-uvk5-v3-firmware-fork.md).

## Context

The operator asked a one-line question after the docs audit: *is the firmware we flashed in this repo
too?* It is not — and the answer turned up two defects worse than anything ADR 0147 found.

**`docs/uvk5-setup.md` sent UV-K5 V3 owners at firmware that cannot run on their radio.** It pinned
`nicsure/quansheng-dock-fw` `0.32.21q`, which targets the **DP32G030**. A V3 is a **PY32F071**. ADR
0118 established this eight cycles earlier — *"the nicsure Dock firmware can never run on it"* — and
the setup guide was never told: zero mentions of the V3, the fork, F4HWN, or how to tell which MCU you
have. ADR 0147's locks could not catch it, exactly as that ADR predicted: the page named every
endpoint correctly and every link resolved. It was simply false.

**The firmware we wrote was invisible.** Measured, not assumed:

- `kbennett2000/uv-k1-k5v3-firmware-custom` `main` was **still pristine upstream at `3bd3ebb`**. All
  ten dock commits sat on stacked feature branches; PR #1 merged F6 into `f5-dock-force-tx`, never
  into `main`. **Cloning the repo got you none of the work.**
- Its `README.md` was **byte-identical to upstream's**. Nothing said a dock mode existed.
- There was **no F6 release**, though F6 is what is flashed and what `setvfo`/`hybrid`/power require.
- The wire protocol existed as comments in `dock.h` and as this repo's ADRs. The *operational*
  knowledge — the six-second TX lockout, that `0x0870` kills the PTT button, that a dock transmit
  leaves the receive front-end mis-tuned — existed **only here**, in a repository a firmware user has
  no reason to read.

### The setup guide was also wrong about something bigger

It assumed flashing is mandatory. It is not, and has not been since ADR 0141: the `eeprom` tuner
drives a UV-K5 with **stock-dispatched** `0x051B`/`0x051D`/`0x05DD`. **There are three ways to run this
radio and the guide documented one.** A reader with a working stock UV-K5 was being told to flash
custom firmware to get a capability they already had.

## Decision

**Document the firmware where a firmware user will look, and treat the seam between the two
repositories as a thing to specify rather than a dependency to mention.**

### radio-server

`docs/uvk5-setup.md` is restructured around three paths — `baofeng`+`eeprom` (no flash, any UV-K5),
`baofeng`+`hybrid` (F6 fork, instant, the repeater path), and the `uvk5` backend (`scan`, a real busy
line, no power control). **Path A leads**, because it is the safe default. The firmware section splits
by MCU and states only what is confirmed about telling a V3 from a classic: the V3 enumerates for USB
DFU where a classic uses the PTT+flashlight serial bootloader, and the bench radio reports bootloader
7.00.07. Anything more specific is one radio and one data point, so it is marked verify-on-hardware
rather than invented (guardrail 1).

### The fork

`main` is fast-forwarded to F6 — ten commits, verified as a clean fast-forward with zero divergence,
so no history is rewritten. A `README` section above F4HWN's own (which stays intact) says what the
fork adds and why `0x0873` had to exist. A new `PROTOCOL.md` specifies the wire protocol well enough
to implement against without reading radio-server, and the F6 release is cut.

### The F-level probe: a refusal is a capability check

The doctor could prove "dock alive" and nothing more — it could not distinguish F2 from F6, so *"why
won't `setvfo` tune?"* cost a bench session per guess. The tell is unavailable by construction: this
fork answers no version string (always-encrypted, ADR 0119), and a pre-F6 dispatch drops an unknown
opcode in silence.

So the doctor now asks the **command**, by sending one the firmware must refuse: an **empty**
`0x0873`. That is safe because of how `dock.c` is written — the length check is the *first* branch of
that case, so `ERR_SHORT` is returned before a field is decoded, before the VFO binding is called, and
with every frequency in the reply blanked. Any `0x0874` answers the question; `ERR_BUSY` proves the
opcode exists as well as `ERR_SHORT` does.

**This only works because the firmware validates before acting.** A design that clamped instead of
refusing — which is what `FREQUENCY_GetBand()` does, and what ADR 0142 had to work around — could not
be probed this way without tuning the radio. The refusal *is* the affordance.

## Acceptance

**Fail-first, run against the bug.** The gate must catch the failure mode that matters here, which is
not "no probe" but "a probe that always says F6". Against a build whose probe reports success
regardless of what came back, **2 of 3 new tests failed**, including the pre-F6 WARN. Restored: green.

**The protocol spec is cross-checked against two independent implementations.** Every golden vector in
`PROTOCOL.md` was decoded by both this firmware's `dock.c` and radio-server's separately-written
`frames.py`, and the K0PRA request built here is byte-identical to the vector in the firmware's own
`tests/host/test_dock.c`. A spec verified by one implementation is a transcript of that
implementation.

**The release binary is the one that was flashed, and the build reproduces it.** A clean rebuild from
`181cb4c` differs in exactly **5 bytes of 105,336** — the embedded build timestamp (`13:36:15 Jul 26`
vs `02:26:18 Jul 27`), same size, same embedded commit stamp, nothing else. That is a better result
than ADR 0118 got trying to reproduce upstream's own artifact, where `-flto=auto` scattered
differences across 8.56% of the image; it is recorded in the release's `PROVENANCE.md` rather than
claimed as bit-for-bit determinism.

`uv run pytest` 1884 passed / 5 skipped; `npm test` 71 passed; the fork's host tests 66 checks, 0
failures (no firmware code changed).

## Consequences

- A UV-K5 owner is told which firmware fits their radio, or that they need none.
- `doctor --backend uvk5` answers "which firmware level" in one line, so the three symptoms that look
  like hardware faults — dock connects and receives silence (pre-F3), keys cleanly and radiates
  nothing (pre-F5), tuning silently fails (pre-F6) — have a first thing to check. Each of those cost a
  diagnostic cycle to find the first time.
- The firmware is usable by somebody who has never heard of this project.
- **The two repositories can now drift from each other**, and nothing checks that. `PROTOCOL.md` and
  `frames.py` agree today because they were cross-checked by hand. That is the ADR 0147 problem one
  level up, and this cycle does not solve it — a shared vector file that both test suites read would.

## Out of scope

- **`BENCH.md`'s five `⚠ CONFIRM AT BENCH` placeholders** — the DFU key combo, USB VID:PID, cable,
  uvtools2 version and calibration-dump command. They need a person at the radio. Filling them from
  inference is the guessed-default failure guardrail 1 exists to prevent, so they stay flagged.
- **Identifying a V3 more precisely than the flash-mode tell.** One radio, one data point.
- **A cross-repo protocol conformance test**, per Consequences.
- **`_UVK5_PINNED_VERSION`** stays `0.32.21q`: it is the classic-Dock pin and still correct for that
  radio. The fork is version-silent by design, which the probe now covers.
