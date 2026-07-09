# Deployment / production guide

**Status: pending. Not yet written.**

There is no production deployment story yet: no working hardware backend, and production
concerns (TLS/reverse proxy, process supervision, the out-of-band tools — Hamlib `rigctld`,
`multimon-ng`, Piper TTS) are brought up alongside the hardware. Documenting them now would mean
inventing specifics, so this file stays a placeholder.

Today's supported way to run the server is the mock backend via `python -m radio_server` — see
the [Quickstart](../README.md#quickstart-against-the-mock). This guide will be filled in during
the hardware/deployment phase, together with [hardware-bringup.md](hardware-bringup.md).
