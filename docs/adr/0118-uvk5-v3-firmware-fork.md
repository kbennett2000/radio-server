# 0118 — UV-K5 V3 firmware fork: pin the base and prove the build (F1, the no-change gate)

Status: Accepted

## Context

The entire UV-K5 backend arc (ADRs 0110–0114, 0117; `radio_server/backends/uvk5/`) was
derived against **nicsure's "Quansheng Dock" firmware**, pinned at tag `0.32.21q`. That
firmware targets the **DP32G030** MCU. The radio actually on the bench is a **Quansheng
UV-K5 V3** — a **PY32F071** MCU (bootloader 7.00.07) — so the nicsure Dock firmware
**can never run on it**. The V3 keeps the same **BK4819** RF chip, and radio-server only
ever drives four dock operations (enter/exit full-control, register read/write, and the
register reply). The plan for this arc is therefore to **add a dock control mode to the V3
community firmware ourselves**, keeping the wire protocol **byte-identical** to the classic
Dock at our existing pin so that radio-server's codec/transport/radio-class and its whole
test suite change **zero**.

This ADR tracks the arc and records **cycle F1 only: the build gate.** F1 ports no protocol
code. It forks the V3 community firmware, pins it to the flashed release, proves the
`build → flash → boot` pipeline with an **unmodified** build, and writes the flash runbook
and license/NOTICE scaffolding. The protocol port is **F2**; the bench loop is **F3**.

Small cycles hold: **F1 build gate → F2 protocol port → F3 bench loop.**

### Source of truth (the pin).

Same discipline as ADR 0110: pin to the exact release that is flashed, record **tag AND
commit SHA**, keep the fork public and Apache-2.0 (upstream asks forks stay open).

- **Fork base — [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom)**,
  a fork of **`armel/uv-k1-k5v3-firmware-custom`** (the F4HWN "Fusion" edition; F4HWN =
  Armel), tag **`v5.7.0`**, commit **`3bd3ebba2ceb553edc88c3f087ce0c7f420433b2`**
  (Apache-2.0). This is the tag matching the flashed `f4hwn.fusion.v5.7.0.bin`. The target
  build is the **`Fusion`** CMake preset (`TARGET = f4hwn.fusion`, `VERSION_STRING_2 =
  v5.7.0`).
- **Dock protocol source (unchanged; F2 ports from it, not built here) —**
  `nicsure/quansheng-dock-fw` @ `0.32.21q` / `4375c3e9604ee4c14ec4bdae67af077879a96f34`
  (`app/uart.c`, `driver/crc.c`); client `nicsure/QuanshengDock` @ `0.32.21q` /
  `851efa955740db9251811cc90195e927b52ba68c` (spec-only). These are the ADR 0110 pins and
  are **restated here for F2's reference only** — F1 touches none of it.

### Pin discrepancy recorded (the flashed asset ≠ the tag commit).

Exactly the situation ADR 0110 flags as the value of pinning. The official
`f4hwn.fusion.v5.7.0.bin` is **not** a from-tag CI build — it is a **committed artifact**:
it lives at `archive/f4hwn.fusion.v5.7.0.bin` in the tagged tree (byte-identical to the
GitHub download, sha256 `66cd1777…`) and its embedded `BUILD_COMMIT` string is
**`0567f01`** — an ancestor **2 commits before** the `v5.7.0` tag. Those 2 commits
(`39cbbd7` "Prepare new version v5.7.0", `3bd3ebb` the merge) change only
`CMakePresets.json` (`VERSION_STRING_2 v5.6.1→v5.7.0`, `…RXTX_LOG_K5VIEWER false→true`)
plus the archived `.bin`s. So the release was built from a pre-tag working tree that had
the v5.7.0 preset edits already applied — **the compile config matches the tag; only the
embedded commit string differs.** We pin the fork base to the **tag `v5.7.0` / `3bd3ebb`**
(it carries the release's compile config and contains the flashed artifact) and record the
`0567f01` provenance here rather than silently trusting the tag.

### Opcode-precision note (the codebase is the authority, not the kickoff summary).

The F1 kickoff described the dock surface as "0x0870 enter / 0x0871 exit full-control,
0x085X register read/write + 0x0851 reply." The in-code truth (`radio_server/backends/uvk5/
frames.py`, ADRs 0110/0112) is: **`0x0870`** EnterHwMode / **`0x0871`** ExitHwMode,
**`0x0850`** WriteRegisters, **`0x0851`** ReadRegisters (the read *request*), and
**`0x0951`** `RegisterInfo` (the read *reply*). The "0x0851 reply" in the summary is the
`0x0851`→`0x0951` round-trip. F2 must reproduce these exact opcodes on the V3 wire.

## Decision 1 — fork + pin, unmodified.

Fork `armel/uv-k1-k5v3-firmware-custom` under `kbennett2000`, public and Apache-2.0. Base
work at tag **`v5.7.0` / `3bd3ebb`**. F1 makes **no change to the firmware source tree** —
only `BENCH.md` (new) and `NOTICE` (attribution appended). Fork branch:
`f1-dock-fork-scaffold`.

## Decision 2 — reproducible headless build (record the byte-compare honestly).

The unmodified pinned tree builds through the repo's own Docker image (ARM GNU Toolchain
**13.3.Rel1**, `arm-none-eabi-gcc 13.3.1 20240614`). The repo's `compile-with-docker.sh
Fusion` does this but its `docker run` uses `-it`, which fails without a TTY; the headless
invocation drops `-it` and is otherwise identical:

```bash
# repo root, at commit 3bd3ebb
docker build -t uvk1-uvk5v3 .
docker run --rm -u $(id -u):$(id -g) -v "$PWD":/src -w /src uvk1-uvk5v3 \
  bash -c "cmake --preset Fusion && cmake --build --preset Fusion -j"
# -> build/Fusion/f4hwn.fusion.bin
```

Result: `f4hwn.fusion.bin`, **103544 bytes**, sha256 `651d057f…`, embedded strings
`UV-K5 Firmware, EGZUMER+F4HWN v5.7.0` / build-commit `3bd3ebb`.

**Byte-identity is not achievable, and it is not merely timestamps:**
1. The official asset embeds a **different** commit (`0567f01`, above) and its own
   `__DATE__`/`__TIME__`.
2. With **identical source + config**, our build still differs from the official asset in
   **8867 / 103544 bytes (8.56%), scattered across the code region** (0x4CD…0x19CDC). A
   same-source build differing only in a commit string + date would differ in <0.1% of
   bytes at a few fixed spots; the pervasive scatter is the signature of **`-flto=auto`
   link-time codegen + a different release build environment** — LTO builds are well known
   to be non-reproducible across environments.

**What was verified instead:** clean build (exit 0) of the unmodified pin; valid
`.bin`/`.elf`/`.hex`; size within 4 bytes of the official asset; identical embedded
version/author/edition strings. **F1's real gate is behavioral** (Decision 5), not
bit-identity. Full provenance is in the fork pre-release
[`radio-server-f1-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f1-v5.7.0),
which carries the F1 bin + `SHA256SUMS`.

## Decision 3 — BENCH.md flash runbook (bench facts flagged, not asserted).

`BENCH.md` in the fork captures the uvtools2 flash path for the V3: DFU entry, the FTDI
cable, the tab-conflict gotcha, and calib-dump-after-first-boot. Guardrail 1 (verify
hardware facts on the hardware) applies: the four radio-specific specifics are written
best-understood and marked **`⚠ CONFIRM AT BENCH`** for Kris to confirm/correct on the first
real flash — none is asserted as confirmed. The V3 flashes over **USB DFU**, not the
classic DP32G030 serial bootloader.

## Decision 4 — license / NOTICE (porting is permitted, with attribution).

Both trees are Apache-2.0: armel's firmware **and** nicsure's `quansheng-dock-fw`. Unlike
the GPL-2.0 `QuanshengDock` C# client (spec-only, per ADR 0110/0002), F2 may **actually port
code** from nicsure's `app/uart.c` into this fork **with attribution**. The fork's `NOTICE`
now records this: dock-mode code derived from nicsure's Apache-2.0 firmware is used with
attribution and modified-file notices; the GPL client is read only as a wire spec and never
copied. `LICENSE` stays Apache-2.0, unchanged.

## Decision 5 — acceptance is the bench flash (out of band).

F1 green-lights F2 when Kris flashes the F1-built bin
(`f4hwn.fusion.v5.7.0.f1.bin`, sha256 `651d057f…`, from the pre-release) over the release
Fusion and the radio **boots, receives, and the keypad works — identical behavior.** Same
source + same config → functionally equivalent firmware; the flash is what confirms it.

## Consequences

- **Zero change to radio-server code.** No file under `radio_server/` is touched; no ADR
  0110–0117 pin moves. `radio_server/backends/uvk5/{frames,transport,radio}.py` are
  untouched — the whole reason to hold the V3 wire protocol byte-identical. **`uv run
  pytest`: 1487 passed, 5 skipped** (unchanged from ADR 0117 — F1 adds no tests because it
  adds no radio-server code).
- **This ADR + `docs/adr/README.md` index row + a `docs/HANDOFF.md` entry** are the only
  radio-server changes in the F1 PR.
- **External deliverables (fork `kbennett2000/uv-k1-k5v3-firmware-custom`):** the fork
  itself (public, Apache-2.0, pinned to `v5.7.0`/`3bd3ebb`); branch `f1-dock-fork-scaffold`
  with `BENCH.md` + `NOTICE`; pre-release `radio-server-f1-v5.7.0` carrying the F1 bin +
  `SHA256SUMS` + build provenance.

## Out of scope (named; built here: none)

Any protocol code, any V3 UART/dock derivation, any reading of nicsure's `uart.c` beyond
what the build required, and any radio-server change — **all F2.** No claim is made here
about V3 UART or dock behavior; that is F2's derivation work against the pins above. The
bench flash acceptance and any V3-specific flash surprises are **F3** (and feed back into
`BENCH.md`).
