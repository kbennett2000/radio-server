# 0068 — kv4p bring-up: two silent-failure detections, shipped with the user docs

Status: Accepted

## Context

With the backend (ADR 0061–0066) and the extras taxonomy (ADR 0067) in place, `uv sync --extra kv4p`
gives a kv4p node everything it needs with **no system library at all**. What was missing was the
human layer — and two of the bench's worst dead ends were *silent* hardware-config failures a
first-time operator cannot diagnose:

1. **Pre-KISS firmware.** A board flashed with firmware older than the KISS protocol (ADR 0064) fails
   the connect handshake with **no error at all** — the `param_len`-gated frame is simply dropped, the
   board never acks, and the operator sees a generic timeout with no hint that *firmware* is the cause.
   The two firmwares even share `FIRMWARE_VER = 17`, so the version number can't discriminate them.
2. **Wrong/missing hwconfig NVS.** The firmware reads `RF_MODULE_TYPE` from NVS and falls back to a
   compiled **VHF** default. The protocol alone cannot tell "NVS says VHF" from "NVS empty → defaulted
   to VHF" — so a UHF board whose board-config was wiped looks, on the wire, like a genuine VHF board,
   and every UHF frequency is rejected as out-of-band for no visible reason.

The NVS wipe is easy to cause: the firmware image is a merged blob spanning `0x0–0xeafff`, which covers
the NVS partition at `0x9000`, so flashing firmware **erases** the board-config. Flashing firmware
without re-flashing the board-config is the common trap.

Docs alone don't fix this — an operator staring at a silent timeout doesn't know which doc to read. So
the detection and the prose are the **same fact**, and they ship together: doctor prints the one line
that names the problem, and the setup guide has the steps to fix it.

## Decision

Add two detections to the kv4p connect probe (`doctor.py::_kv4p_connect_probe`) and write the user docs
they point at.

### Detection 1 — pre-KISS firmware sniff
On a failed handshake, `_sniff_pre_kiss_firmware(port)` re-opens the port (DTR/RTS held low) and reads a
short window. Opening resets the ESP32 (reset-on-open, ADR 0066), so a pre-KISS board dumps its
old-protocol boot frames right there. We report pre-KISS **only on a positive tell**: the old
delimiter **`de ad be ef`** present, **and** no KISS `FEND` (`0xC0`), **and** no `KV4P` vendor prefix
anywhere in the window. On a hit doctor prints *"this board is running pre-KISS firmware — flash v17"*
pointing at `docs/kv4p-setup.md`; otherwise it keeps the generic handshake-failure line.

- `de ad be ef` is a **new marked constant** (`_PRE_KISS_DELIMITER`) — it existed nowhere in the tree;
  it's a wire fact from the bench (guardrail 1).
- The **boot banner is deliberately not used** as the tell — it exists in both firmwares.
- Before sniffing, the probe closes the transport it owns so the sniff can reopen the port.

### Detection 2 — band mismatch (wrong/missing hwconfig NVS)
When a HELLO is present, compare its reported band (`RfModuleType`) against the operator's configured
`kv4p.module_type` (normalised via `module_type_from_band()`). On disagreement doctor emits a **WARN**
(not a FAIL — the HELLO parsed fine): *"band mismatch: board reports VHF, you configured UHF — the
hwconfig NVS is probably missing or wrong; reflash the board-config"*. This is the check that catches a
wiped/never-written board-config, which is otherwise invisible on the protocol.

### The docs
- **New `docs/kv4p-setup.md`** — the flashing/first-run guide: the two-writes-in-order rule and why
  firmware wipes the board-config; the six board-config images and reading the PCB revision (v2.0e uses
  the v2.0d config); the web flasher's port-lock (fully quit Chrome) with the `esptool` terminal
  escape; use the by-id path, never `/dev/ttyUSB0`; run doctor first; set `kv4p.frequency` (no knob, no
  invented default); and the awkward-but-normal facts (reset-on-open, the ADR-0066 flag loss on a
  reports-off board, and that DTMF over kv4p is an **open bench item**, not a promised feature).
- **Integrated, not bolted on:** `install.md` forks by radio (the kv4p path is `uv sync --extra kv4p`
  with **no** PortAudio/sound-card steps — the easier radio); `troubleshooting.md` gets an early kv4p
  fork (no volume knob, no capture level); `configuration.md` gains the `[kv4p]` section and owns the
  `kv4p.squelch` (SA818 level 0–8) vs `audio.squelch` (gate mode) name collision, noting
  `audio.squelch = "cat"` is valid on kv4p (real carrier-detect pin) and rejected on baofeng;
  `README.md`/`architecture.md`/`deployment.md`/`hardware-bringup.md` move kv4p from "planned" to
  supported. `hardware-bringup.md` stays the AIOC bench reference — not merged.
- The stale "TM-V71A only" note on `audio.squelch` (spec.py + radio.toml.example) is corrected to
  "TM-V71A and kv4p".

## Consequences

- A pre-KISS board and a wiped board-config each become **one line of doctor output** instead of a
  multi-hour silent dead end.
- Both detections are pure logic over injected wire data (a `sniff=`/`_open=` seam and the existing
  `transport=` seam), so they're unit-tested with **no hardware and no keying** — the whole cycle was
  built and verified against the mock/fake seams.
- The `de ad be ef` constant and the flash offsets/PCB list in the setup guide are bench facts recorded
  from this cycle's brief; they're marked as such (guardrail 1) rather than asserted as repo-derived.

## Non-goals (deferred, per ADR 0067)

The kv4p **installer path** (`scripts/install.sh`/`.ps1` have no kv4p option) and making the
`check_mumble_importable()` "earn the banner" gate conditional on the mumble extra remain open for a
later cycle. This cycle documents the manual `uv sync --extra kv4p` path; it does not add an installer
flag.
