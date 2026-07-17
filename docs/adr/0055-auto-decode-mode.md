# 0055 — Auto-resolve the DTMF decode mode by multimon-ng availability

Status: Accepted

## Context

ADR 0054 added `GoertzelStream`, an in-process DTMF decoder (`dtmf.decode_mode = native`) with no
`multimon-ng` binary — built specifically to unlock a native Windows install and to give CI a decode
path. But nothing selects it. The default is still `streaming`, and `streaming` needs `multimon-ng`:
on any box without it on PATH, the first RX write raises

> `multimon-ng binary 'multimon-ng' not found; install multimon-ng or set RADIO_MULTIMON_BIN`

(`MultimonStream._spawn`). `multimon-ng` has no official Windows build — the exact dead end 0054 exists
to remove — so the new decoder is unreachable without hand-editing `radio.toml`, which the target
audience (non-technical operators; ADR 0053) will not do.

Constraints found in the tree:

- **The decode mode is a single validated setting.** `dtmf.decode_mode` is coerced against the
  `DECODE_MODES` tuple (`config/spec.py`); adding a value there makes it accepted everywhere. Both
  dispatch seams — the live controller (`build_controller`) and the `doctor --dtmf` diagnostic — are
  already explicit branches over the mode, kept in lockstep (ADR 0054).
- **Binary presence is directly checkable.** `load_multimon_bin(settings)` yields the configured binary
  (default `multimon-ng`, or an absolute path); `shutil.which` resolves either form on PATH.
- **The two decoders are not equal in confidence.** `streaming` is bench-verified on real RF (ADR
  0038, multimon 1.3.1). `native` is verified only against synthetic tones in CI — its real-RF talk-off
  and weak-signal behaviour is an open guardrail-1 item (ADR 0054).

## Decision

Add `DECODE_MODE_AUTO = "auto"` to `DECODE_MODES` and make it the new `DEFAULT_DTMF_DECODE_MODE`.
`auto` resolves once, at controller construction, to a concrete mode:

- **`multimon-ng` on PATH → `streaming`**; **otherwise → `native`.**

Explicit `streaming` / `buffered` / `native` pass through unchanged and behave exactly as before.

- **Resolution keys on binary presence — `shutil.which(load_multimon_bin(settings))`, not
  `sys.platform`.** A Linux operator who skipped `apt install multimon-ng` has the identical failure a
  Windows user has; a Windows user who *built* a binary should get the RF-verified path. Platform is a
  proxy; the binary being present is the actual fact the choice depends on. `which` also honours an
  absolute `dtmf.multimon_bin`, so a custom build is detected the same way.

- **`streaming` stays preferred when the binary is present.** It is bench-verified on RF; `native` is
  not yet. So `auto` only routes to `native` where the alternative is *no working decoder at all*.
  `auto` is the designated landing site for the future `streaming`-vs-`native` A/B (ADR 0054): when
  recorded-RF testing settles which is better, flipping the preference is a **one-line change to a
  single constant in the resolver**, not a redesign.

- **A shared resolver, one source of truth.** `resolve_decode_mode(mode, multimon_bin) -> (resolved,
  reason)` lives beside the mode constants in `dtmf.py`. `build_controller` uses `resolved`; `doctor`
  uses both, so it can *say which decoder is live* — `decode mode: auto -> native (no multimon-ng on
  PATH)` / `auto -> streaming (multimon-ng found)`. Without that line a user had no way to tell which
  decoder `auto` picked; with `auto` in play that opacity would be worse, so surfacing it is part of
  the decision.

- **Guardrail 1: `auto` changes behaviour only in a state that previously raised.** When `multimon-ng`
  is present, `auto` resolves to `streaming` — byte-for-byte the old default. It diverges only when the
  binary is absent, where the old default did not run at all but crashed. This is asserted as a test
  (`auto` and `streaming` wire the same decoder when the binary is present; `streaming` raises but
  `auto` builds a working `GoertzelStream` when it is absent), not left as a comment.

### Alternative considered — key on `sys.platform`

"Native on Windows, streaming elsewhere" is simpler to read but wrong on both ends: it strands a Linux
user who never installed `multimon-ng` (streaming, still raises) and denies the RF-verified path to a
Windows user who built one. The failure is about the binary, so the check is about the binary.

### Alternative considered — flip the preference to `native`

Rejected this cycle. `native`'s real-RF robustness is unproven (ADR 0054). `auto` deliberately prefers
the verified decoder and leaves the preference as the single constant the pending A/B will set.

## Consequences

- **The native decoder is now reachable with zero configuration**, and the one-command install works on
  a fresh box with no `multimon-ng` (native Windows included) instead of crashing on first RX. The
  bench-proven `streaming` path is unchanged wherever the binary exists.
- **An explicit mode is a contract, `auto` is a fallback.** `dtmf.decode_mode = streaming` with no
  binary still raises the original install error — a test pins this — so an operator who asked for
  multimon is told it is missing rather than silently downgraded.
- **`doctor --dtmf` now reports the resolved decoder and the reason**, printed before the backend opens
  (so it shows even with no radio attached, and is `capsys`-testable). New tests inject `shutil.which`
  (no `skipif`), so the resolution is covered on every machine.
- **The default in `radio.toml.example` becomes `decode_mode = "auto"`** (regenerated from the spec);
  the one test asserting the old default is updated.
- **Deferred, recorded so scope stays small:** the docs still describe `multimon-ng` as required and
  route Windows through WSL2 (`docs/install.md`) — that rewrite is a separate cycle and cannot be
  written honestly until this one lands. No preference flip (needs the A/B); no opus/pymumble work.
