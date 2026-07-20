# 0101 — DSRP repeater registration corrected against a real gateway (poll carries the callsign; no outbound 0x0B)

Status: Accepted

## Context

The module-A crossband (reflector → radio-server → DV Dongle decode → RF) never received a single frame
from a real ircDDBGateway: `/dstar/status` `rx_frames` stayed **0** through every bench test, so the
decode/stuck-key re-proof (ADR 0097/0098/0099) could not even begin. Live diagnosis on the bench gateway
pinned the cause to radio-server's own DSRP registration, not hardware, the reflector, or the network:

- radio-server (playing the repeater side) sent a DSRP **register** packet `b"DSRP" + 0x0B + "AE9S   A"
  + 0x00` on startup and every 30 s. The gateway logged **`Unknown packet from the Repeater`** and
  dropped every one — **249× in one afternoon, and 0× for the two working G4KLX `dstarrepeater` modules
  (B/C)** on the same gateway. Because the gateway never accepted module A as a live repeater, it never
  routed the reflector's voice back to it.

The DSRP codec (`radio_server/dstar/dsrp.py`, `client.py`) was written for ADR 0087 as an *independent
implementation of the g4klx wire format "read purely as a specification,"* with the register/poll type
bytes explicitly marked **guardrail-1: "verify against a real gateway."** That verification had never
happened: every prior "proven on hardware" claim (ADR 0087 echo loopback, ADR 0089 `--dstar-browser-echo`,
ADR 0098 decode-only bench) used the gateway **Echo** unit (URCALL=E) or a decode-only script with
radio-server stopped — paths that need **no repeater registration**. So the registration path shipped
unvalidated from its first commit (`62a7c83`); this is guardrail-1 finally coming due, not a regression.

The g4klx source (read as spec) shows the direction was backwards on two counts:

- `ircDDBGateway/Common/HBRepeaterProtocolHandler.cpp` accepts **from** a repeater only `0x0A` POLL,
  `0x20` HEADER, `0x21` AMBE, `0x22/0x23` busy, `0x24` DD. Registration is **implicit**: the gateway keys
  the repeater from the **POLL's 8-char callsign plus the UDP source address**. There is no separate
  register packet.
- `0x0B` (NETWORK_REGISTER) is the **gateway → repeater** direction — `DStarRepeater/Common/
  GatewayProtocolHandler.cpp` *receives* it (`readRegister`). radio-server was sending a gateway→repeater
  packet the wrong way.
- radio-server's `0x0A` poll (which the gateway *does* accept) carried only the bare module letter `"A"`,
  not the callsign the gateway registers on — so even the accepted packet did not register module A.

## Decision

Align radio-server's repeater→gateway keep-alive with the real HB/DSRP protocol:

1. **No outbound `0x0B`.** Remove `dsrp.build_register()`. `0x0B` is inbound-only; the parser still
   recognizes an inbound `0x0B` NETWORK_REGISTER from the gateway (`TYPE_REGISTER` retained, reclassified
   in comments/docstring as gateway → repeater).
2. **The poll is the registration.** `UdpGatewayClient.poll()` now sends `build_poll(self._register_name)`
   — the full 8-char callsign (`format_callsign(callsign, module)` → `"AE9S   A"`,
   bytes `44 53 52 50 0A 41 45 39 53 20 20 20 41 00`) — not the module letter. `register()` is repurposed
   to send that poll; `start()` registers via the first poll. A single poll cadence replaces the old
   register(30 s)+poll(60 s) pair; `DEFAULT_POLL_INTERVAL` drops 60 → **10 s** (marked default, still
   guardrail-1) to stay inside the gateway's repeater-inactivity timeout. An empty `register_name` now
   logs a warning (a callsign-less poll cannot register).
3. **`registered` means the gateway answered.** It was set `True` unconditionally right after the (bogus)
   register send — status always lied. Now it is set only when a well-formed inbound DSRP packet arrives
   (`_handle_packet`), and cleared on `close()`. This is display-only: a full consumer trace
   (`GatewayStatus.registered` → `DStarLinkManager.status()` → `/dstar/status`) confirms **nothing gates
   audio on it** — the crossband path in `bridge.py` gates on `_running`/`_mode`/`_tx_source`/`_tx_slot`.
   `MockGatewayClient` keeps its optimistic (already-accepted peer) semantics so bridge/app tests are
   unaffected; the divergence is documented on the class.

The circular tests (which asserted radio-server sends what radio-server defines) are replaced with tests
that pin the real g4klx wire format and guard against re-introducing either defect: keep-alives are always
`0x0A` (never `0x0B`), the poll carries the full callsign (never the bare module), and `registered` is
false after `start()` until an inbound packet arrives. This supersedes the (never-validated) registration
claim in ADR 0087; the header/data wire format and the DV geometry are unchanged.

## Consequences

- The gateway should stop logging `Unknown packet from the Repeater` for `AE9S   A` and register module A
  from its poll, so reflector voice can finally reach the DV Dongle — unblocking the ADR 0097/0098/0099
  crossband re-proof (which stays gated on the live radios until it passes on a dummy load).
- `/dstar/status` `registered` becomes truthful (false until the gateway replies), which the web panel and
  `doctor` surface.
- **Guardrail-1 items still resolved only against the live gateway** (verify loop, not guesses), each with
  the same acceptance — the gateway log shows the poll **accepted** (no "Unknown packet") and an inbound
  over yields `rx_frames > 0`:
  1. exact `0x0A` poll body (callsign NUL-terminated vs any trailing field) — confirm against
     `HBRepeaterProtocolHandler.cpp` and/or a `tcpdump` of the working B/C poll on UDP 20010;
  2. the poll interval vs the gateway's inactivity timeout — match the observed B/C cadence;
  3. whether the gateway emits any inbound packet on registration before voice (determines whether
     `registered` flips true on a quiet linked reflector — cosmetic only);
  4. the gateway config's module-A callsign + UDP port match radio-server's `"AE9S   A"` on local 20012.
