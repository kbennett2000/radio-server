# Handoff

## Current state

Cycle 1 complete: the `Radio` protocol surface and a full `MockRadio` are in place,
with the hardware backends stubbed and wired into a factory.

- `radio_server/backends/base.py` — `Radio` + `CatRadio` protocols, `Capability`
  StrEnum, `SHARED_CAPS`/`CAT_CAPS`/`FULL_CAPS`, `RadioStatus`, `UnsupportedCapability`,
  `AudioFrame = bytes`.
- `radio_server/backends/mock.py` — `MockRadio`. Records TX to `tx_log`, serves
  canned RX, fakes status/busy. `supports_cat=True` (default) = full caps; `False`
  models an audio-only radio (CAT methods raise `UnsupportedCapability`).
- `radio_server/backends/{signalink_v71,aioc_baofeng}.py` — stubs; constructors raise
  `NotImplementedError`. No sounddevice/pyserial/rigctld imports yet.
- `radio_server/backends/factory.py` — `create_radio(name, **kw)` + `available_backends()`.
- `tests/` — 27 tests, all green. `uv run pytest`.
- ADR 0002 records the concrete protocol shape (two-tier protocol, frozenset caps,
  bytes audio placeholder).

## Next up

- Audio I/O layer, or DTMF decode (`multimon-ng -a DTMF`) against MockRadio's RX.
- Revisit `AudioFrame = bytes` before real audio lands (numpy sample array?).
- CAT method signatures (tone type, scan params/return) are minimal — refine via ADR
  when the CAT layer is built.

## Open questions / blocked

(none)
