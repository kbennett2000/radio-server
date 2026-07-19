# 0087 — The D-STAR link: radio-server as a homebrew-repeater endpoint on an ircDDBGateway

Status: Accepted

## Context

ADR 0086 shipped the vocoder seam — PCM ⇄ AMBE over the DV Dongle — **isolated and unwired**, on the
explicit promise that "the framing/socket/backend work that follows is ordinary plumbing over a
known-good codec." This ADR is that plumbing: it makes radio-server the **first live consumer** of the
vocoder, wiring it to a running **G4KLX ircDDBGateway** so the station can talk and listen on D-STAR
reflectors (REF/XRF/DCS/XLX) exactly the way a hotspot does.

The gateway already runs persistently on the bench, driven by a DVAP on module **B** (callsign AE9S,
registered, REF linking proven). radio-server joins it as a **second homebrew-repeater endpoint** on
its own module, so the two coexist: the DVAP is the operator's handheld hotspot; radio-server is a
gateway-attached endpoint that bridges reflector audio to and from the rest of the stack (RF backends,
Mumble, services) through the shared audio plane.

The one thing that could not be proven by inspection is whether the **DSRP gateway protocol plus the
DV Dongle AMBE2000** actually carry intelligible D-STAR end to end. The gateway has a built-in **Echo**
unit (on by default): a stream addressed to `URCALL = "       E"` is recorded and replayed to the same
module. That gives a **deterministic, fully-local loopback** — encode → DSRP → gateway → echo → DSRP →
decode — with no remote reflector, no second operator, and no registration, which is the hardware
acceptance for this cycle.

## Decision

Add a `radio_server/dstar/` package — a sibling to the Mumble link (`radio_server/link/`), the same
"network peer, not a `Radio` backend" shape (ADR 0041) — that speaks the gateway's repeater protocol
and bridges its audio through the ADR 0086 vocoder. **Off by default**; existing deployments and the
two running bench instances are untouched.

- **A pure, I/O-free DSRP wire codec** (`dstar/dsrp.py`), the same split as `vocoder/frames.py` and
  `backends/kv4p/frames.py`. It builds and parses the `"DSRP"`-tagged UDP packets the G4KLX
  DStarRepeater ↔ ircDDBGateway link uses: **register** (`0x0B`, name), **poll** (`0x0A`, keep-alive),
  **header** (`0x20`, the 41-byte radio header + 16-bit session id), and **data** (`0x21`, seqNo with
  the `0x40` end-bit, the 12-byte DV frame). Reimplemented clean from g4klx
  `DStarRepeater/Common/RepeaterProtocolHandler.cpp` (GPL-2) read **purely as a protocol
  specification** — the byte layout is an interop fact, not ported code (the ADR 0086 / kv4p stance:
  talking to a peer over a wire is not a derivative work).

- **The 41-byte D-STAR radio header** (`dstar/header.py`): three flags then the four 8-char callsigns
  RPT1/RPT2/UR/MY1 and the 4-char MY2, closed by a 2-byte **CRC-16/X-25** (reflected CCITT, init
  `0xFFFF`, xorout `0xFFFF`) — a standard CRC, implemented from the algorithm. The gateway accepts
  `FF FF` as a "skip checksum" sentinel, so a correct CRC is belt-and-suspenders, not load-bearing;
  we compute it anyway and honour the sentinel on parse.

- **A transport seam** (`dstar/client.py`), the `link/client.py` pattern: a `GatewayClient` `Protocol`
  (register/poll, `send_header`/`send_data`, `on_header`/`on_data` sinks, `status`, `close`) with an
  in-memory `MockGatewayClient` for tests and a `UdpGatewayClient` that owns the UDP socket, a daemon
  reader thread, and the register/poll timers. pyserial-free (plain `socket`); a `_socket_factory` /
  `_clock` test seam keeps the whole bridge unit-testable with no network — the mock-first discipline.

- **A half-duplex bridge** (`dstar/bridge.py`), the `link/bridge.py` state machine adapted to D-STAR's
  one-talker-at-a-time reality. A **mode latch** (IDLE / RX / TX) guards the single vocoder chip so
  encode and decode are **never interleaved per frame** — the ADR 0086 pipeline hazard — which is also
  physically correct: the RF side is simplex, and a D-STAR stream is half-duplex. RX (reflector → the
  stack): decode inbound AMBE, resample 8k→48k at the edge (`audio.resample.to_canonical`), key the
  radio through the shared `TxSlot`/`TxSession`/`station_id` (Part 97 auto-ID, ADR 0041) and publish to
  the `AudioHub`. TX (the stack → reflector): pull RF via the `acquire_rx` demand + `AudioHub`,
  resample 48k→8k, encode, and emit one header then the data frames, closing with the `0x40` end frame
  after a tx-hang of silence. The 48k⇄8k resample lives only at this backend edge, never in the vocoder
  (ADR 0086).

- **A distinct module and port** from the DVAP. radio-server defaults to module **A** on its own local
  UDP port (`DEFAULT_LOCAL_PORT = 20012`), so both endpoints register with the one gateway without
  colliding on the DVAP's module B / port 20011.

- **Config `[dstar]`, off by default** (`config/spec.py`): `callsign`, `gateway_host`, `gateway_port`,
  `local_port`, `module`, `reflector` (optional startup link), `vocoder_port` (the DV Dongle by-id
  path), and an advanced `tx_hang`. The link is inert until `callsign` **and** `gateway_host` are set —
  the same "no destinations ⇒ no link" gate as Mumble — so `create_app` builds the bridge only when
  configured. The config canary and `radio.toml.example` are updated in step (the ADR 0086 deferral of
  the `[vocoder]`/`[dstar]` group ends here, as that ADR foretold).

- **A `doctor --dstar-echo` hardware self-test** (`doctor.py`), the `--vocoder-loopback` sibling and
  the acceptance for this cycle: open the DV Dongle vocoder and a `UdpGatewayClient`, register the
  module, send the reused staircase stream addressed to `URCALL = E`, receive the echoed DSRP data,
  decode, resample to canonical, write a WAV, and report the reused `staircase_pitch_metrics` (lag-
  aligned pitch correlation, ≥ 0.8 PASS). Backend-independent, like `--vocoder-loopback` and
  `--analyze-wav`; it drives the vocoder + a socket, never a radio.

### Why not broaden it

No new reflector-linking UI, no DTMF reflector control, no second gateway, no full-duplex crossband,
no AMBE3000. This cycle proves one endpoint end-to-end and wires it off-by-default. Linking reflectors
from DTMF, surfacing D-STAR state in the web UI, and bridging D-STAR ↔ Mumble are follow-ons over this
now-known-good seam.

## Consequences

- radio-server can register on the bench gateway as its own module and round-trip audio through the
  real AMBE2000: the echo loopback proves encode → DSRP → gateway → echo → DSRP → decode with a
  pitch-faithful result, the same objective metric ADR 0086 used, now across the full protocol stack
  rather than a bare serial loopback.
- The half-duplex latch means radio-server is a one-talker endpoint: while a reflector stream is
  inbound it does not simultaneously encode RF to the reflector, matching how D-STAR and the simplex RF
  backends already behave. Full-duplex crossband would need two vocoders (or the proven-but-hazardous
  single-chip duplex) and is deliberately out of scope.
- Off by default: no `[dstar]` config ⇒ no bridge, no socket, no vocoder open — the two running bench
  instances (AIOC 8090, kv4p 8091) and every existing deployment are byte-for-byte unaffected. The
  default test suite stays hardware-free (fakes + fake clock); the DV Dongle and a live gateway are
  touched only by the opt-in doctor self-test.
- **Bench-verified (this cycle):** the acceptance is the echo loopback against a throwaway echo-only
  gateway bound to `127.0.0.2` (isolated from the production gateway on `127.0.0.1`, so nothing is
  disturbed). Wiring radio-server to the **live** DVAP gateway is one operator step — add a second
  repeater module and restart ircDDBGateway (a brief DVAP reconnect) — left to the operator per the
  bench "do not disturb the running services" constraint, not done autonomously.
- **Still ahead:** a live reflector QSO (real remote audio both ways), DTMF/web reflector control, and
  the D-STAR ↔ Mumble bridge — all now ordinary wiring over this seam.

Cross-refs: ADR 0086 (the vocoder seam this consumes; the pipeline no-interleave hazard the latch
respects), ADR 0041/0042 (the Mumble link shape, `TxSlot`/`station_id`/`AudioHub` this reuses), ADR
0006 (canonical audio + fail-loud `AudioFrame` + the edge-resample rule), ADR 0061 (the kv4p transport
+ pure frame-codec split this mirrors), ADR 0029 (the doctor self-test + missing-extra error shape).
