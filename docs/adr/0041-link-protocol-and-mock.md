# 0041 — The Link protocol and mock-first build

Status: Accepted

## Context

radio-server is about to grow a **second port** next to `Radio`: a `Link` — a
peer on the audio bus that is *not* the antenna. Where `Radio` is the RF side
(SignaLink/AIOC + optional CAT), a `Link` is a network peer: an M17 reflector, an
AllStar node, and — through AllStar — EchoLink.

The strategy that worked for `Radio` (ADR 0001/0002) is repeated deliberately:
define one protocol, ship a mock, build and test the whole stack against the mock,
and bring up the real transport last. Hardware/transport is not the gating
concern for correctness — the abstraction is.

Planned backends, **not built in this cycle**: M17 via mrefd (first), AllStar via
`chan_usrp` + AMI (later, and the door to EchoLink). Native EchoLink is
**permanently out of scope**: this project is MIT and EchoLib is GPL.

This cycle is **pure**: protocol + mock + factory + tests. Nothing is wired to
`api/`, `arbiter/`, `rx/`, `tx/`, `AudioHub`, or `TxSlot`; there are no sockets,
no mrefd/USRP/AMI, no Codec2, no ctypes, no UI. This ADR refines the ADR-0001/0002
reasoning onto the network port; it does not contradict it.

## Decision

- **A single `Link` protocol, mock-first, mirroring `Radio`.** `Link` is a
  `@runtime_checkable` `typing.Protocol` (Protocol-only base, house style) with
  the shared surface `connect` / `disconnect` / `status` / `transmit` / `stream` /
  `receive` / `capabilities`, plus `enable` (below) and the two gated operations.
  (`stream` was added in the ADR-0044 amendment — see below.) `MockLink`
  is a first-class implementation the whole stack is built against; real
  transports come last, against the network. Types are frozen dataclasses
  (`LinkStatus`, `Station`), capabilities a `StrEnum`, exactly as `backends/base.py`.

- **The direction convention — the single easiest thing to get backwards.**
  `Radio.transmit` means "out the antenna"; `Link.transmit` means "out to the
  network." So a bridge (a later cycle) feeds radio RX into `link.transmit` (local
  RF → internet) and `link.receive` into `radio.transmit` (internet → out the
  antenna). Everything arriving on a Link is third-party traffic the station puts
  on the air under the licensee's callsign. This is stated in the protocol
  docstrings and here because it is the one thing an implementer will invert.

- **Capability split (guardrail 3).** `SHARED_CAPS` = connect/disconnect/status/
  transmit/receive is universal. Two capabilities are **not** universal, and these
  are real backend differences, not hypotheticals:
  - `DIRECTORY` — a central user/peer database. EchoLink and AllStar have one; M17
    has **no** central directory (your callsign is your ID).
  - `LISTEN_ONLY` — a protocol-level listen mode (mrefd `LSTN`). It is what makes a
    listen-before-you-talk tier possible with zero credentials; EchoLink has no
    such mode. A first-class capability, not a UI flag.
  `capabilities()` reports the implemented subset, and the two optional operations
  (`directory()`, `set_listen_only()`) raise `UnsupportedLinkCapability` — carrying
  the attempted capability — instead of silently no-op'ing, so a future API cycle
  can 501 **by name**.

- **Flat protocol, not two-tier — a considered divergence from ADR 0002.** `Radio`
  split into `Radio` + `CatRadio` because the five CAT methods form one coherent
  superset a backend either has or lacks. Link's two options are **orthogonal** — a
  backend may have either, both, or neither (M17 = listen-only, no directory;
  EchoLink = directory, no listen-only; AllStar = directory) — so they cannot form
  a single derived tier. `Link` is therefore one flat protocol whose optional
  methods **self-gate** by capability. As with `Radio`, `runtime_checkable` only
  checks method presence; the true "unsupported here" contract is the *raising*,
  not `isinstance`.

- **`MockLink` with a toggleable capability set.** Two orthogonal bools
  (`directory`, `listen_only`, both default `True` = the maximal mock) select the
  advertised set, so the directory-vs-no-directory and listen-only splits are both
  exercisable with no network. It records TX to an inspectable `tx_log` (the
  `MockRadio.tx_log` mirror, with a fail-loud format guard before append), serves
  scripted RX then `None` (idle), and fakes peers/talkers. A `create_link(name)`
  factory mirrors `backends/factory.py`; only `mock` is registered now.

- **The enable safety property — pinned now, while it is free.** `enabled` is
  **not** sticky. A Link comes up disabled and requires a deliberate `enable(True)`.
  The reason is a composition: `controller.autostart` (ADR 0037) already defaults
  on; autostart **plus** a sticky link-enable equals "your transmitter is on the
  internet, unattended, from power-on" — a control-operator posture nobody chose,
  that would simply emerge from two independently reasonable defaults. Everything
  on a Link is third-party traffic under the licensee's callsign, so this is
  forbidden at the leaf: `MockLink` has **no** born-enabled construction path and
  never loads `enabled` from persistence — the only route to `True` is an explicit
  `enable(True)`. There is **no config key** this cycle; this ADR fixes the rule
  the wiring cycle must obey — it must never auto-enable a Link, and the arbiter/tx
  must consult `enabled` before routing audio to it.

- **Amendment (Cycle 49, ADR 0044): stream boundaries via `stream(on)`.** The
  outbound-audio cycle found that `transmit(AudioFrame)` alone cannot express a
  *transmission boundary* — a real network protocol frames each stream (M17 sends
  an LSF at the start and an EOT at the end), and inferring those edges from gaps
  between frames is guesswork. So `Link` gains `stream(on: bool)`: the network
  mirror of `Radio.ptt(on)` (a boundary, separate from the per-frame `transmit`),
  where `stream(True)` opens the stream (LSF) and `stream(False)` ends it (EOT). It
  is part of the `TRANSMIT` surface — **not** a new `LinkCapability` — so the
  capability partition is unchanged; a backend that transmits also streams.
  `MockLink` records the edges inline in `tx_log` (a `StreamEdge.START`/`END`
  marker) so a test sees the frames bracketed by one open/close pair. This amends,
  and does not supersede, the protocol surface above.

## Consequences

- The whole Link stack is unit-testable offline against `MockLink`, and guardrail
  3 has coverage from this cycle: the gated operations raise by name with no
  network.
- Adding M17 or AllStar is one backend class plus one registry entry; nothing
  above the link layer changes, because consumers depend only on `Link` +
  `capabilities()`.
- The enable safety rule is pinned **before any wiring exists**, so the dangerous
  autostart×sticky-enable composition can never silently emerge as the network
  cycles land — the rule predates the code that could violate it.
- `link/` is a clean leaf: its only `radio_server` import is `..audio`, enforced
  by a test that parses the package's imports — so nothing above it can leak into
  the protocol.
- **Trade-off:** the mock cannot model real transport — packet loss, jitter, the
  codec, authentication, reflector protocol quirks — so a dedicated real-transport
  bring-up phase is still required, the same caveat ADR 0001 recorded for the
  radios. And because the protocol is flat, `isinstance(x, Link)` only proves
  method presence; the "unsupported here" contract lives in the raising, exactly as
  ADR 0002 noted for `CatRadio`.
