# 0095 — An ircDDBGateway remote-control client, landed as an isolated seam (unwired)

Status: Accepted

## Context

radio-server today is **gateway module A** — a homebrew-repeater (DSRP) endpoint on the ircDDBGateway
(ADR 0087) — and it links reflectors by injecting an in-band URCALL command through its own audio
stream. That works, but it has two limits the roadmap has flagged for a while:

- **It can only steer its own module.** The two 70cm **DVAPs** on the bench are separate gateway
  endpoints — each an independent `dstarrepeater` process registered as its own module (B = 441.600
  today; C = 441.000 next). radio-server cannot see or control them at all. Kris wants radio-server to
  be the single control panel for the whole rig: pick a reflector for each DVAP, read its state.
- **Link state is "believed", never confirmed.** With no readback, `DStarLinkManager.status()` reports
  the last URCALL we *sent*, not what the gateway actually did (ADR 0088).

The ircDDBGateway has always had the mechanism for both: its **remote-control interface** — a small
UDP protocol (`CRemoteHandler` / `CRemoteProtocolHandler`) that links/unlinks *any* module and reports
each module's confirmed link. Recon on the live gateway confirmed the binary has it compiled in
(`KEY_REMOTE_ENABLED/PORT/PASSWORD`, `CRemoteProtocolHandler::sendRandom`) but it is currently **off**.
Turning it on is a gateway config + restart (an operator/deploy step, not code).

This ADR is the **first, isolated half** of DVAP support: a clean client for that protocol, built and
unit-tested in isolation and **not wired into the app**, exactly as the vocoder seam (ADR 0086) and the
DSRP seam (ADR 0087) were landed before anything consumed them. Wiring — config, a `DvapManager`, API
routes and a DVAP web tab — is a **separate follow-up PR** so this one stays small and reviewable, and
so a bug in a brand-new wire protocol cannot touch the running radios.

## Decision

Add two modules beside the DSRP seam, mirroring `dstar/dsrp.py` (pure codec) + `dstar/client.py`
(transport):

- **`radio_server/dstar/remote_codec.py`** — the frame layer only, no I/O. Builds the client→gateway
  packets and parses the gateway→client replies of the ircDDBGateway remote-control protocol. Every
  packet opens with a **3-byte ASCII tag**; all multi-byte integers are **little-endian on the wire**
  (the wx `*_SWAP_ON_BE` convention — wire is little-endian, and the gateway box is x86/LE):

  - `LIN` — login request (tag only). Gateway replies `RND` + a 4-byte random.
  - `SHA` + 32-byte digest — `SHA256(random_bytes ‖ password_bytes)`, where `random_bytes` are the
    **four random bytes exactly as they arrived on the wire** (the gateway hashes its own native LE
    bytes of that value, which equal the wire bytes). Gateway replies `ACK` or `NAK<text>`.
  - `LNK` + callsign(8) + reconnect(int32 LE) + reflector(8) — link a module to a reflector.
  - `UNL` + callsign(8) + protocol(int32 LE) + reflector(8) — unlink.
  - `GCS` — get callsigns; gateway replies `CAL` with `('R'|'S') + callsign(8)` entries.
  - `GRP` + callsign(8) — get one repeater; gateway replies `RPT` = callsign(8) + reconnect(int32) +
    reflector(8), then zero-or-more 24-byte link records `reflector(8) + protocol(int32) +
    linked(int32) + direction(int32) + dongle(int32)`. This is the **confirmed** link state.

  Callsigns are the 8-char space-padded D-STAR field (`format_callsign` already exists in
  `dstar/header.py`). `RECONNECT`/`DSTAR_PROTOCOL` enum orderings are reproduced as Python enums. Parse
  **never raises** — a malformed reply is `UNKNOWN`, like `dsrp.parse`.

- **`radio_server/dstar/remote_client.py`** — the transport seam, mirroring `client.py`:
  a `RemoteControlClient` **Protocol** (`login` / `link` / `unlink` / `query` / `close`), an in-memory
  **`MockRemoteControlClient`** (records `sent`, `inject()`s replies, scripts the auth handshake) so the
  future `DvapManager` is fully testable with no socket, and a **`UdpRemoteControlClient`** that owns a
  UDP socket and does the request/reply round-trips with a bounded timeout + retry. Auth is performed
  lazily on first command and cached; a `NAK`/timeout surfaces as a typed error.

Nothing imports these yet. Defaults are marked "verify against the live gateway" (guardrail 1):
`DEFAULT_REMOTE_PORT = 10022` (g4klx default), password supplied by the caller (a secret — it will live
in `radio-secrets.toml` when wired, never in `radio.toml` or the repo).

Source of truth: the public g4klx `ircDDBGateway/Common/RemoteProtocolHandler.{h,cpp}` and `Defs.h`
read purely as a **protocol specification** — tags, offsets and enum orders are interop facts, not
ported GPL code (the ADR 0086 stance).

## Consequences

- **Nothing in the running system changes.** No config key, no route, no startup path references these
  modules; `uv run pytest` gains coverage but the app is byte-for-byte unaffected. The DVAP tab,
  `DvapManager`, and the `[dvap]` config arrive in the next PR and consume this seam.
- **Verified on fakes now; bench-verified against the live gateway in the hardware phase.** The codec is
  proven by round-trip and byte-layout tests, and `MockRemoteControlClient` proves the auth handshake +
  command flow. The wire constants (little-endian ints, the exact SHA256 input, port 10022) are
  judge-on-the-chip facts marked for confirmation once remote-control is enabled on the gateway — the
  first live check is a safe `login → GRP → LNK REF0xx → GRP → UNL` round-trip on **module A** (no TX),
  the analog of the DSRP Echo-unit proof (corr 0.999, ADR 0087).
- **Confirmed link state becomes possible everywhere.** Once wired, the same `GRP`/`RPT` readback that
  drives the DVAP tab can retire module A's "believed state" — a nice follow-on, out of scope here.
- **Bounded and defensive.** Parse never raises; the UDP client bounds every wait and never blocks a
  caller; a wrong password fails closed with a typed error rather than hanging.

Cross-refs: ADR 0086 (isolated-seam discipline; spec-not-port), ADR 0087 (the DSRP seam this sits
beside; module A / ports), ADR 0088 (in-band URCALL + "believed state" this can later replace),
ADR 0089 (the DVAP as a separate gateway endpoint we couldn't control — the gap this opens the door to).
