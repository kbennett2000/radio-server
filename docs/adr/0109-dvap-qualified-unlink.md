# 0109 — DVAP Disconnect: a blank LNK drops a fixed link; UNL cannot

Date: 2026-07-20
Status: accepted

## Context

The DVAP card's Disconnect button never unlinked a module — the link stayed up ("Linked ·
REF030 C") until the next Connect replaced it. Bench-reported with a screenshot.

Two layers, both found live against the gateway:

1. `DvapManager.unlink()` sent the remote-control `UNL` command with its defaults
   (`protocol=UNKNOWN`, reflector blank). The gateway's `UNL` drops the link MATCHING the
   command's protocol+reflector (multi-link modules exist, so the command names its target) —
   UNKNOWN + blank matches nothing. Silent no-op, no NAK.
2. Sending a fully-qualified `UNL` (live reflector + protocol from the `RPT` status reply) got
   further but was refused: **"Cannot unlink REF030 C because it is fixed."** Every link this
   manager makes is `Reconnect.FIXED` (deliberately — links must survive), and the gateway
   protects fixed links from `UNL` entirely.

The verb that actually drops a fixed link is `LNK` **to a blank reflector** — the same
drop-and-switch a Connect performs, switched to nothing. Bench-proven: the gateway logged
"Removing outgoing D-Plus link AE9S C, REF030 C" and the reflector acked the disconnect.

## Decision

`unlink()` reads the module's confirmed state (`client.status`, as `refresh()` does); if any
link is live it sends `link(callsign, "", Reconnect.NEVER)`. With no live link there is nothing
to drop — no wire command, and the state notification still fires so the UI settles. A dead
gateway during the pre-read maps to the existing `DvapUnavailable`. `UNL` is not used at all.

## Consequences

- Disconnect actually disconnects (bench-verified against the live gateway on both a D-Plus and
  a DCS fixed link), and is idempotent on an already-unlinked module.
- One extra `GRP` round-trip per unlink — negligible on the LAN control path.
- If a future caller ever creates non-fixed links, the blank `LNK` still drops them — this path
  does not depend on the fixed-ness that defeated `UNL`.
