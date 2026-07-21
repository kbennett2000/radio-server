# 0114 — UV-K5 (Quansheng Dock): config block, factory, doctor, and setup docs

Status: Accepted

## Context

Cycles 3–4 (ADR 0112/0113) made `Uvk5Radio` control/keying/audio-complete against fakes, but the
backend was not yet *selectable* (`server.backend = "uvk5"` was not in the factory), *diagnosable*
(`doctor` could not route to it), or *documented*. This cycle makes the UV-K5 a first-class backend —
the kv4p flash-day set (ADR 0068), applied to the UV-K5: a `[uvk5]` config block + factory
registration, `doctor` wiring, a minimal constructor extension, and `docs/uvk5-setup.md`. No baofeng
or kv4p behaviour changes; no bench claims (division of labour: fakes only; Kris keys).

## Decision 1 — the `[uvk5]` config block: two REQUIRED, fail-loud fields

The block mirrors `[kv4p]`, with defaults imported from the backend modules. Two fields are
**REQUIRED** (no invented default) — the kickoff's "fail loud beats inventing a default", made
concrete:

- **`uvk5.serial_port`** — the AIOC enumerates as an ambiguous `/dev/ttyACM*`, and a bench with more
  than one ACM adapter makes a bare `ttyACM0` wrong. A guessed default would point at the wrong device,
  so it is REQUIRED (a stable `by-id` path). New coercer `coerce_required_str` was reused.
- **`uvk5.frequency`** — in full-control (XVFO) mode the host owns tuning and there is **no radio-side
  value to preserve**: kv4p's NVS-preserve rationale (an unset frequency keeps the device's last one)
  does *not* transfer, so an unset value is an error, not a made-up frequency on the air. New coercer
  `coerce_required_int` (mirrors `coerce_required_str`).

The rest carry marked verify-on-bench defaults: `tone` (optional, new `coerce_optional_float`), `mode`
(`FM`), `tx_allowed` (`true`), the AIOC `input_device`/`output_device`/`blocksize`, `tx_lead_seconds`
(`0.5`, inherited from the AIOC/UV-5R bench — this radio earns its own number), and
`squelch_threshold` (`40`, the reg-0x67 RSSI busy gate ADR 0112's `status().busy` reads).

`audio.squelch = "cat"` is **valid** for the UV-K5 (unlike the Baofeng: it has a real RSSI busy line),
guarded like kv4p's — `cat` with a `0` threshold is rejected (RSSI ≥ 0 reads busy forever). The factory
(`REGISTRY` + `backend_kwargs`) threads every setting; `create_radio("uvk5", …)` builds it.

### The latent presence-fabrication bug the REQUIRED fields surfaced

`Uvk5Radio` is the first backend with REQUIRED config keys, which exposed a pre-existing bug: two sites
reconstruct a config from *every `is_set` key* — `save_settings` (writes the file) and the
`/radio/select` `base` patch (ADR 0026). A backend's *optional* keys all resolve to defaults and read
as `is_set`, so both sites **fabricated presence** for an unconfigured backend (ADR 0074's
presence-based `configured_backends`). Harmless for baofeng/kv4p (no required keys); for uvk5 the
fabricated-but-incomplete block (its REQUIRED `serial_port` unwritten) crashed the backend enumeration
on an unset value. Fixed at both sites: `save_settings` skips a backend group with any unset REQUIRED
key (never persists an unbuildable block), and the select `base` carries only *actually-configured*
backends' keys. Both are correct independently of uvk5; regression-tested.

## Decision 2 — `doctor --backend uvk5` (mirrors kv4p) and the stock-vs-dock tell

Routing (`_resolve_doctor_backend` → uvk5; `_build_backend`/`_uvk5_config` reading the real
`radio.toml` via the fixed `_doctor_settings`), a register-keying `--key-test` (confirmed by the ADR
0112 read-back, reusing the exact non-TTY/CONFIRM RF guards), and `--rx-level`/`--rx-capture`/`--dtmf`
riding the shared canonical `receive()` path — with a real-sound-card true-rate print (the AIOC gets no
free pass; a new `_format_soundcard_rx_rate` since kv4p's is Opus-packet-specific).

**The connect probe** replaces the AIOC sound-card check: a `ReadRegisters(0x30)` elicit proves dock
firmware is alive; on that success a best-effort HELLO reads the version and warns on a
dock-but-wrong-version vs the pinned `0.32.21q`.

**The silent-failure tell** — the pre-KISS-sniff analog — is **derived from the pinned firmware tree**
(`nicsure/quansheng-dock-fw@4375c3e`, `app/uart.c`), not memory:

- `0x0514` HELLO → `0x0515` version reply is **unguarded** — a **stock** UV-K5 answers it.
- `0x0851` ReadRegisters (and the other `0x08xx`) are `#ifdef ENABLE_DOCK` — **dock-only**; a stock
  radio ignores them silently.
- So: a register-read timeout **plus** a HELLO answer = **stock firmware — flash the Dock firmware**
  (a positive tell); neither answering = off/asleep/wrong-baud/wrong-port.

**Obfuscation asymmetry (also derived from `uart.c`):** `bIsEncrypted` defaults *true* at reset, but
the firmware reads the opcode *before* deobfuscation and `obf(0x0514) == 0x6902` (the enable-encryption
sentinel) — so a HELLO must be sent **plaintext** (raw `0x0514`); receiving it clears encryption, so
the reply is plaintext too. The probe is therefore **plaintext-out / plaintext-in**, run over a
short-lived `Uvk5Transport(obfuscate=False)` — reusing the tested transport, touching neither
`connect()` nor any existing method, opened only after the (obfuscated) register-probe transport is
closed. (An earlier note had the send direction inverted; the codebase's own `frames.build_frame`
docstring and cycle-2 fake settle it: HELLO is plaintext.)

## Decision 3 — a minimal, RF-safe `Uvk5Radio` constructor extension

The config supplies `tone`/`mode`/`tx_allowed`, which the constructor did not accept. Added
behaviour-preserving: `tone`/`mode` call the existing `set_tone`/`set_mode` at init (fail loud out of
range); **`tx_allowed`** is a software refuse-to-key — unlike kv4p's firmware NVS gate, full-control
keying is a direct register write, so `tx_allowed=False` raises `Uvk5KeyingError` at the **top of
`_key_on`**, before the playout stream opens and before any register write. It composes with the ADR
0112 read-back-confirm (same exception type: a policy refusal and a silent no-key are both "never dead
air") and does not weaken the open-stream-first / drop-line-first ordering or touch the soundcard seam.

## Consequences

- New tests (all hardware-free): the `[uvk5]` config resolution/coercion + REQUIRED fail-loud +
  round-trip (`test_config`), factory wiring + cat guard (`test_backend_wiring`), the constructor
  tone/mode/tx_allowed cases (`test_uvk5_radio`), and the full doctor set (`test_doctor`) — routing,
  connect-probe dock/stock/dead/wrong-version, the plaintext HELLO probe over a `dock`/stock/dead
  extension of `FirmwareFakeSerial`, key-test pass/refuse, and the rate helper. The settings-API canary
  moved 79 → 89 (the 10-key block). Full suite green, incl. the unchanged baofeng/kv4p suites.
- Extras isolation (ADR 0067): the `uvk5` extra already existed (Cycle 4); this cycle adds no deps —
  `hardware`/`kv4p`/`mumble` closures stay byte-identical, the deployed box untouched.

### Verify on hardware (guardrail 1 — no bench numbers fabricated)

- **THE acceptance gate (carried from ADR 0112/0113):** whether register-keying in full-control mode
  actually transmits the AIOC-injected K1 mic audio — unsettleable offline; the whole UV-K5 TX path
  must pass it on the bench before it is trusted.
- The `0.5 s` TX lead-in, the AIOC device name / true sample rate, the real HELLO obfuscation +
  reset-on-open timing (the probe closes immediately after; that a dock tolerates the encryption-off
  HELLO mid-diagnostic is bench-only — the DOCK-branch version read is explicitly best-effort), and the
  Dock firmware flash recipe (documented against the pinned repo's own instructions, not fabricated).

### Out of scope (named; built here: none)

Server-side presets, the web UI, the stuck-key **watchdog/TOT** arc (tracked from ADR 0112 — the
full-control loop has no time-out; a hard `SIGKILL` mid-key leaves the radio keyed, warned prominently
in `docs/uvk5-setup.md`), and any bench numbers.
