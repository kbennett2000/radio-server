# 0060 — Resolve `auto` to `native`; multimon-ng becomes optional

Status: Accepted

## Context

ADR 0054 added an in-process Goertzel DTMF decoder (`native`) and left exactly one open
verify-on-hardware item under guardrail 1: native's real-RF robustness — decode quality and talk-off —
versus the multimon-ng `streaming` path was **unproven** (synthetic tones are not RF). ADR 0055 then
made `auto` the default but deliberately preferred `streaming` whenever the multimon-ng binary was on
PATH, routing to `native` only where the alternative was *no working decoder at all*. It named the
tuning surface precisely: "flipping the preference is a **one-line change to a single constant in the
resolver**," and marked `auto` as "the designated landing site for the future `streaming`-vs-`native`
A/B."

Constraints found in the tree:

- **The resolver already isolates the decision.** `resolve_decode_mode(mode, multimon_bin) ->
  (resolved, reason)` (`radio_server/audio/dtmf.py`) is the one place `auto` is interpreted; its only
  consumer is `build_controller` (`radio_server/controller/engine.py:738-748`), and `doctor._dtmf`
  (`radio_server/doctor.py:606-627`) reuses it to *report* which decoder went live. The `shutil.which`
  branch was the single constant ADR 0055 pointed at.
- **The resolver never raises.** The "binary missing" `RuntimeError` fires later, at decoder
  construction/first write in `MultimonStream._spawn()` (`dtmf.py:534-538`) and
  `MultimonDtmfDecoder.decode()` (`dtmf.py:305-307`). So an *explicit* `streaming`/`buffered` is a
  contract that still fails loud with no binary — flipping `auto` cannot weaken that.
- **The docs still required multimon-ng.** `docs/install.md`'s extras table listed it, the apt/brew
  lines installed it, and the Windows section routed real-radio users through WSL2 *because*
  multimon-ng has no Windows build (`install.md`, `scripts/install.ps1`). ADR 0055 recorded this as
  a known gap; ADR 0057 further noted that with opus now a bundled wheel dependency, that extras table
  "can collapse to PortAudio + a voice," but deferred the rewrite.

The bench A/B is now settled. On the reference station (AIOC + UV-5R), `native` decodes noticeably
**better** than multimon-ng on real RF. That closes ADR 0054's open guardrail-1 item for decode and
sets the constant ADR 0055 left pending.

## Decision

**`auto` resolves to `native` unconditionally, and multimon-ng stops being a required dependency.**

- **The one-line flip.** `resolve_decode_mode` drops the `shutil.which(multimon_bin)` branch: `auto`
  returns `(native, "bench-verified, ADR 0060")` whether or not the binary is present. The binary's
  presence no longer decides anything. `multimon_bin` stays in the signature for call-site stability
  (and is still read by the explicit modes). The four sites that *described* the old contract — the
  `DECODE_MODES` and `DEFAULT_DTMF_DECODE_MODE` comments in `dtmf.py`, the `build_controller` comment,
  and the `dtmf.decode_mode` help text in `config/spec.py` — are updated in lockstep so nothing lies.
- **`streaming` and `buffered` stay, unchanged, as explicit escape hatches — and still raise.** An
  explicit mode is a contract, not a preference: asking for multimon and not having it must fail loud
  (the raise in `MultimonStream`/`MultimonDtmfDecoder` is untouched), never silently downgrade to
  native. This is why the flip is confined to `auto` — it changes only the mode whose whole job was to
  choose.
- **multimon-ng leaves the install story.** `docs/install.md`'s extras table collapses to exactly
  **PortAudio + a voice** (dropping the multimon-ng row and — finishing ADR 0057's deferred note — the
  Opus row, since opus now rides in the `mumble` wheel extra); the apt/brew lines drop `multimon-ng`,
  `libopus0`, and `opus`; and the Windows section drops the "no Windows build → use WSL2 for DTMF"
  rationale, because `native` decodes in-process on Windows. `dtmf.multimon_bin` and the
  `radio.toml.example` block stay, re-scoped to "only needed for the explicit streaming/buffered modes."
- **`doctor` reports the flip.** `decode mode: auto -> native (bench-verified, ADR 0060)`, with the
  same reason whether or not the binary is on PATH.

### Alternative considered — keep preferring `streaming` when multimon is present

This was ADR 0055's deliberate position, correct *until the A/B settled*: prefer the RF-verified
decoder, route to `native` only to avoid no-decoder-at-all. The bench inverted the premise — `native`
is now the RF-verified-better decoder — so keeping the preference would route the reference station's
own hardware to the *worse* path and keep a dependency (and the Windows/WSL2 detour) that no longer
earns its place. Rejected.

## Consequences

- The default install shrinks: PortAudio + a voice, no multimon-ng, no separate libopus. Native Windows
  decodes over-the-air DTMF with nothing extra — the WSL2 detour was a multimon artifact.
- multimon-ng is still fully supported for anyone who wants it — `dtmf.decode_mode = "streaming"` (or
  `"buffered"`) — and still fails loud if the binary is absent. No capability was removed, only the
  default preference and the dependency.
- **Open item, recorded — the bench proved decode, not talk-off.** False digits on *voice* (talk-off)
  were **not** exercised by the bench. `NATIVE_ONSET_BLOCKS = 1` is where that hides: a single ~25.6 ms
  block emits a digit, whereas ITU-T Q.24 wants ≥40 ms (≥2 blocks) to accept. It is pinned to `1` by
  ADR 0038's "two 9s @ 30 ms gap → 99" acceptance row, which encodes multimon's *measured* behaviour
  rather than the Q.24 spec. The failure mode is quiet: it surfaces only as a spurious combo firing,
  and because `98#` (link-off) is ungated (ADR 0043), the visible symptom would be a **Mumble link
  dropping on its own**. If that appears, `NATIVE_ONSET_BLOCKS` (and re-examining that acceptance row)
  is the lever. This cycle records the lever; it does not pull it.
- **Verify on hardware, still open beyond talk-off:** weak-signal / HT-flutter robustness and the
  level/AGC-dependent `NATIVE_ENERGY_FLOOR` remain the marked tuning surface from ADR 0054.
