# 0103 — Remote-control client re-authenticates after gateway session loss

Status: Accepted

## Context

`UdpRemoteControlClient` (ADR 0095) performs the ircDDBGateway remote-control login
(`LIN` → `RND` → `SHA` → `ACK`) lazily and caches `_authed = True` **forever** — nothing but
`close()` ever clears it. The gateway, however, keeps a single live login session, and a gateway
**restart drops it**: afterwards the gateway ignores unauthenticated queries (observed live as a
`sendto err 22` flood to a zeroed client address) and discards unauthenticated commands.

Observed consequences on the bench (2026-07-20, twice in one day):

- `link()`/`unlink()` are fire-and-forget, so after a gateway restart they became **silent no-ops
  that still returned HTTP 200** — the DVAP panel said "linked" while the gateway did nothing.
- `status()` timed out into `reachable: false` and stayed there; the only recovery was restarting
  radio-server (or a manual out-of-band login), because no code path ever invalidated `_authed`.

## Decision

Treat a reply failure on an authenticated request as **session loss** and heal it in the client:

1. `status()`/`callsigns()` (the round-trip commands): on `RemoteTimeout` (after the normal
   retries) or `RemoteAuthError` (a NAK on the query), clear `_authed`, run **one** fresh login,
   and retry the request once. A failure of the re-login or the retried request propagates exactly
   as before (`DvapManager.refresh` maps it to `reachable: false`).
2. `link()`/`unlink()` stay fire-and-forget on the wire (matching the protocol shape), but they go
   through `_ensure_auth()`, so once any round-trip has detected the session loss they log in fresh
   before sending. In practice the DVAP panel's status poll runs continuously, so the session heals
   within one poll interval of a gateway restart and subsequent link/unlink commands are
   authenticated again — no radio-server restart.
3. An initial-login failure (bad password NAK) still raises immediately — re-auth applies to a
   *lost* session, not a rejected credential.

## Consequences

- A gateway restart degrades the DVAP panel for at most one poll cycle instead of until the next
  radio-server restart; the two live incidents' manual workaround (an out-of-band scripted login)
  is obsolete.
- One retry, not a loop: a gateway that is actually down fails fast exactly as today.
- The test fake gains a stateful gateway model (login session + restart + silent-when-unauthed),
  pinning the failure mode the flat scripted-reply fake could not represent.
