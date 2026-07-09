# Hardware wiring & bring-up guide

**Status: pending. Not yet written.**

This guide is deliberately empty until the hardware bring-up cycle. Both real backends
(`SignaLinkV71`, `AiocBaofeng`) are `NotImplementedError` stubs today — see the
[project status](../README.md#status--read-this-first). Writing wiring pinouts, the Hamlib rig
model number, `rigctl` serial speed, `multimon-ng` flags, or the AIOC's PTT line (RTS vs DTR)
now would mean fabricating verify-on-hardware facts (guardrail 1), so this file stays a
placeholder rather than stating guesses as confirmed.

It will be written during the bench bring-up phase, whose acceptance is empirical: "plug it in,
it keys up clean." Until then, everything runs against the mock backend
([architecture.md](architecture.md#backends)).
