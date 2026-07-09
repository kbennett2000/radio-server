# 0002 — Radio protocol shape

Status: Accepted

## Context

ADR 0001 decided on "a single Radio protocol" with shared methods (transmit,
receive, ptt, status) and CAT-only methods (set_frequency, set_channel, set_tone,
set_mode, scan) "implemented only by the TM-V71A backend", with differences surfaced
via `capabilities()`. Cycle 1 implements that protocol and the MockRadio, which
forces the abstract shape to become concrete. A few shaping decisions refine (but do
not contradict) 0001, so they are recorded here.

## Decision

- **Two-tier Protocol, not one flat protocol.** `Radio` is the shared surface
  (`transmit`, `receive`, `ptt`, `status`, `capabilities`). `CatRadio(Radio)` adds
  the five CAT tuning methods. Audio-only backends satisfy `Radio` only. This keeps
  the type of a CAT method off the shared surface entirely, so nothing above the
  radio layer can accidentally depend on tuning. Both are `@runtime_checkable`.
- **`capabilities()` returns `frozenset[Capability]`**, where `Capability` is a
  `StrEnum` of the nine operations. Constants `SHARED_CAPS`, `CAT_CAPS`, and
  `FULL_CAPS` name the partitions. The API layer checks membership before dispatch.
- **CAT calls on a non-CAT backend raise `UnsupportedCapability`** (carrying the
  attempted `Capability`) — never a silent no-op (guardrail 3). `runtime_checkable`
  Protocols only check method presence, so this exception, not `isinstance`, is the
  real contract for "unsupported here".
- **Audio is `bytes` (PCM)** via an `AudioFrame` alias, to keep cycle 1
  dependency-free and its buffers trivially inspectable. A later audio-I/O cycle may
  swap `AudioFrame` for a numpy sample array; callers use the alias.
- **`RadioStatus` is a frozen dataclass** with shared fields (`backend`,
  `transmitting`, `busy`) plus optional CAT fields (`frequency`, `channel`, `tone`,
  `mode`) that stay `None` on audio-only backends.
- **`MockRadio(supports_cat=...)`.** Default `True` reports full capabilities (per
  0001). `supports_cat=False` models an audio-only radio so the capability split is
  testable entirely against the mock, with no hardware. CAT method signatures are
  minimal and may be refined (tone type, scan parameters) in a future ADR.

## Consequences

- Type-checkers distinguish "any radio" (`Radio`) from "a radio I can tune"
  (`CatRadio`) at function boundaries.
- The unsupported-capability path is exercised without hardware, so guardrail 3 has
  test coverage from cycle 1.
- Switching the audio representation later is a one-alias change plus the audio
  layer, not a protocol rewrite.
- The `bytes` audio type is a placeholder; the hardware/audio cycle must revisit it
  before real I/O lands.
