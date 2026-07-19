# 0088 — D-STAR reflector control + browser talk/listen: a hotspot you drive from a web page

Status: Accepted

## Context

ADR 0087 shipped the `radio_server/dstar/` link — radio-server as a homebrew-repeater endpoint on a
G4KLX **ircDDBGateway**, bridging reflector audio through the DV Dongle vocoder — and hardware-proved
it against an *isolated throwaway* gateway's Echo unit (pitch correlation 0.999). It deliberately
stopped one operator step short of usable: it had **no connection to the live gateway**, **no way to
pick or link a reflector**, and **no browser audio path** — the bridge only crossbanded a *physical
radio's RF* to a reflector.

This ADR closes that gap to the stated goal: **an operator opens a web page, picks a reflector
(REF/XRF/DCS/XLX), hits connect, and then talks and listens with their computer mic and speakers — no
D-STAR radio, no radio menus.** It is the D-STAR analogue of ADR 0050 (the web UI as a Mumble client),
built over the ADR 0087 seam and the same shared audio plane.

The gateway's remote-control interface is off (and we keep it off), so reflector linking goes the
standard D-STAR way — a **URCALL routing command** injected through the bridge, exactly as a hotspot
or a handheld does.

## Decision

Three additions on the ADR 0087 seam, each mirroring the Mumble stack (ADR 0042/0050), plus the live
wiring and a dedicated instance.

- **Bridge (`dstar/bridge.py`).** Three capabilities on `DStarBridge`, reusing its encode→DSRP
  pipeline and the half-duplex `_mode` latch:
  - `send_link_command(urcall)` — a **synchronous, idle-gated** DSRP burst (a header carrying the
    routing URCALL, then the terminator; only `NULL_AMBE`, so it never touches the vocoder chip). It
    is synchronous with no `await` on purpose: the latch is mutated only at synchronous points on the
    loop, so an atomic burst can neither corrupt nor be corrupted by an in-flight over. Returns
    `False` if the bridge isn't idle (the API surfaces "busy").
  - `send_operator_audio(pcm48)` / `end_operator_over()` — the browser-mic TX source (the D-STAR twin
    of `MumbleBridge.send_operator_audio`), driving the same `_open_tx`/`_feed_rf`/`_end_tx` encode
    path from WebSocket frames instead of the RF `AudioHub`. The endpoint owns the over: the first
    frame opens it, a WS idle-timeout/disconnect closes it. Drops while an inbound reflector stream
    holds the RX latch (one talker; the vocoder is busy decoding).
  - A **keepalive** (see Consequences) that keeps the AMBE2000 warm while idle.
- **`dstar/manager.py` — `DStarLinkManager`.** A thin single-bridge command/state layer (the gateway
  has one endpoint, one bridge — *not* a per-entry factory like the Mumble `LinkManager`). `connect`
  parses a reflector (`"REF001 C"` → URCALL `name.ljust(6)+module+"L"` = `REF001CL`; REF/XRF/DCS/XLX
  differ only by the name prefix the gateway routes on), `disconnect` sends the module-wide unlink
  (`"       U"`), and `status` reports **believed** state — what we last *sent*, since the gateway
  gives no readback (surfaced honestly in the UI). A `dstar` WS event mirrors the `link` event.
- **API (`api/app.py`).** `WS /audio/dstar/rx` (fans out a dedicated `dstar_rx_hub`) and
  `WS /audio/dstar/tx` (its own `dstar_talk_slot`, distinct from the RF and Mumble slots, keying the
  reflector not RF) — the twins of `/audio/mumble/{rx,tx}`. `POST /dstar/link`, `POST /dstar/unlink`,
  `GET /dstar/status`, and a `dstar` block on `/status`. All wired in `create_app` behind the existing
  `dstar.callsign` gate; `rx_to_reflector=(not dstar.operator_tx)`.
- **Config.** One new key, `dstar.operator_tx` (default off): on for a **browser-operator** instance
  (the operator's mic/speakers are the audio, so the RF→reflector pump is disabled and can't fight the
  browser-TX path for the single TX latch); off keeps the ADR 0087 crossband posture. `dstar.reflector`
  (previously informational) becomes the boot-time auto-link. canary 74→75; `radio.toml.example`
  regenerated.
- **Web UI.** A `DStarPanel` reflector picker (free-text + a couple of presets, Connect/Disconnect, a
  believed "Linked" pill), and a `dstarMode` (from `dstar.operator_tx`) that points the existing
  Monitor/Transmit controls at `/audio/dstar/{rx,tx}` — reusing the already-`path`-parameterized
  `useRxAudio`/`useTxAudio` hooks, exactly as ADR 0050 did for Mumble.
- **Doctor.** `--dstar-browser-echo` — the browser acceptance: it drives the real
  `send_operator_audio` (TALK) through the gateway Echo unit and receives the echo through the real
  in-bridge decode → `dstar_rx_hub` (LISTEN), reusing the staircase pitch metric.
- **Deployment.** A **dedicated** radio-server instance (MockRadio backend, its own port + user
  service, `dstar.operator_tx` on) serves the browser, so the two production instances (AIOC 8090,
  kv4p 8091) are untouched and the DV Dongle has a single owner.

### Why not broaden it

No DTMF reflector control, no gateway-confirmed link state (the readback channel is a follow-on — the
DSRP `TEXT`/`STATUS` packets the parser already decodes), no D-STAR ↔ Mumble bridge, no full-duplex.
This cycle makes the browser hotspot usable end to end; the rest is ordinary wiring over the seam.

## Consequences

- **Bench-verified on the LIVE gateway (this cycle), nothing else disturbed.** The production
  ircDDBGateway got the one approved change — a second homebrew module `AE9S A` on `127.0.0.1:20012`
  alongside the DVAP's module B — and a single restart; the DVAP re-registered and its `REF001 C` link
  returned. Throughout, the two production radio-server instances and the `dstarrepeater` (DVAP) kept
  their **baseline PIDs with `NRestarts=0`** — untouched.
  - **Talk + listen (browser round trip):** `send_operator_audio` → DSRP → gateway Echo → in-bridge
    decode → `dstar_rx_hub` round-tripped the nine-tone staircase at **pitch correlation 0.88** (heard
    195 frames on the hub; a bare-AMBE tap variant scored 1.000). The browser talks and listens on
    D-STAR through the DV Dongle.
  - **Reflector control:** `POST /dstar/link {"reflector":"REF030 C"}` made the gateway log
    `Link command from AE9S A to REF030 C issued via UR Call` → `D-Plus ACK ... received` →
    `D-Plus link to REF030 C established`. So a **header-only** URCALL command links a live reflector —
    the central verify-on-hardware unknown resolved: `command_frames` defaults to 0, no fallback ladder
    needed. Believed state matched the gateway.
  - **WS endpoints** accept `?token=` auth and carry canonical frames; the SPA serves the reflector
    picker and the D-STAR audio paths.
- **The AMBE2000 idle-sleep bug — found and fixed here.** ADR 0087 assumed "a real bridge never has
  the record-then-replay idle gap." **Wrong for a listener:** a browser endpoint sitting idle waiting
  for inbound reflector audio lets the chip go unresponsive after ~2–3 s (bench-measured: OK at 2 s
  idle, timeout at 3 s), so the *first* inbound over's decode timed out and the whole over was lost.
  Fix: an **idle-gated keepalive** that decodes `NULL_AMBE` every ~1.2 s while the bridge is IDLE,
  keeping the chip primed. It is gated to idle so it never interleaves with a live encode/decode stream
  (the ADR 0086 hazard); a real over's own frames keep the chip warm. Post-fix, no decode timeouts.
- **Off by default.** No `dstar.callsign` ⇒ no bridge, no hub, no slot, no manager, no vocoder opened —
  existing deployments and the two production instances are byte-for-byte unaffected. The default test
  suite stays hardware-free (fakes + fake clock); the DV Dongle and the live gateway are touched only
  by the opt-in doctor self-test and the dedicated instance.
- **Believed link state can diverge** from the gateway (no readback with remote-control off); the UI
  says so plainly. The `TEXT`/`STATUS` confirmation channel is the eventual fix, noted not built.
- **Still ahead:** gateway-confirmed link state, DTMF reflector control, and the D-STAR ↔ Mumble
  bridge — all ordinary wiring over this now-usable seam.

Cross-refs: ADR 0087 (the D-STAR link this extends; the header order + chip-idle findings), ADR 0086
(the vocoder seam + the no-interleave hazard the keepalive respects), ADR 0050/0042 (the Mumble
browser-client + link shapes mirrored: `send_operator_audio`, a dedicated rx hub, a distinct talk
slot, the `LinkManager`/`link` event), ADR 0006 (canonical audio + the edge-resample rule).
