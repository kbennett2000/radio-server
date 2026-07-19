# 0089 — D-STAR folded into the real radios: one shared DV Dongle, crossband + browser together

Status: Accepted

## Context

ADR 0088 shipped a **standalone** browser D-STAR instance (its own port + user service, a MockRadio
backend, `dstar.operator_tx` on): pick a reflector, talk/listen with the PC mic/speakers through the
DV Dongle. In real use it fell short of what the operator actually wanted:

- **No RF.** MockRadio meant a linked reflector produced no audio on any frequency. The goal is: link
  a reflector and hear it on **both** the browser **and** a real FM radio's frequency, and talk onto
  the reflector from **either** the PC mic **or** that FM radio.
- **No HTTPS.** The standalone instance served plain HTTP on a LAN IP, so the browser blocked the mic
  and audio ("this page isn't secure"). The two real instances (AIOC 8090, kv4p 8091) already serve
  HTTPS.

The operator's model: **don't run a separate D-STAR node.** Fold D-STAR into the two existing real
instances. Connect a reflector from whichever instance's web UI you like; **that** instance
crossbands the reflector to **its** radio, so the instance you pick decides the FM frequency. Only one
DV Dongle exists, so the two instances **share** it — whichever is actively bridging holds it.

ADR 0088 deliberately made crossband (RF→reflector) and the browser mic mutually exclusive
(`rx_to_reflector = not operator_tx`) because both feed the single outbound DSRP session with no
arbitration and would corrupt it. This ADR removes that limitation so both run at once, and makes the
DV Dongle a shared, on-demand resource.

## Decision

Four changes on the ADR 0087/0088 seam; nothing new on the gateway (module `AE9S A` already exists).

- **TX-owner latch (`dstar/bridge.py`).** A `_tx_source` (`None | "rf" | "op"`) records who opened the
  current outbound over. Both TX sources — the crossband RF pump (`_rf_to_reflector`) and the browser
  mic (`send_operator_audio`) — open the over only when idle, feed only while they own it, and drop
  (counted) while the other owns it. `_end_tx(source)` and `end_operator_over`/the pump's silence
  timeout are ownership-guarded, so one source's close can't tear down the other's live over. This
  lets a folded instance run `tx_to_rf` **and** `rx_to_reflector` **and** the browser mic together —
  one talker at a time, no interleaving into a single DSRP session (the ADR 0086 hazard).
- **The DV Dongle is acquired on link, released on unlink (the sharing arbiter).** The bridge no
  longer holds the dongle while idle. `DStarLinkManager.connect` calls `bridge.start()`, which creates
  the vocoder from a **factory** — opening the FTDI port **exclusively** (`pyserial exclusive=True`) —
  then registers the gateway endpoint and launches the tasks; `disconnect` calls `bridge.stop()`,
  which closes the vocoder and releases the port. The vocoder is created **first** in `start`, so a
  dongle already held by the other instance fails the exclusive open before anything else opens;
  `VocoderUnavailable` surfaces as **503 "the DV Dongle is unavailable / in use by the other radio."**
  The **OS serial exclusive lock is the sole cross-process arbiter** — no lockfile or IPC. The bridge
  is built at boot but not started; `start`/`stop` now follow the reflector link, not the app lifespan.
- **Browser audio follows link state (`web/`).** `dstarMode` is derived from `state.dstar.active` (a
  reflector is linked), not a static config flag: linked → Monitor/Transmit target `/audio/dstar/{rx,
  tx}`; unlinked → back on RF. One UI serves both RF and D-STAR by link state — no dedicated node, no
  posture flag. `dstar.operator_tx` is **removed** (canary 75→74); crossband + browser are always both
  live when `[dstar]` is configured, arbitrated by the latch.
- **Activity log (`bridge → WS → web`).** Every inbound over already carries the sender's callsign
  (MYCALL) in its D-STAR header; it was parsed and discarded. The bridge now fires an `on_activity`
  callback on each inbound header (MYCALL, "rx") and on each of our own overs (our callsign, "tx"). The
  API enriches it with the believed reflector, keeps a 30-entry ring on `/dstar/status`, and pushes an
  `activity` WS event; a new `DStarActivityLog` card shows who's on the reflector (and it lands in the
  raw event log for free).

### Deployment

`[dstar]` is enabled on **both** 8090 (AIOC) and 8091 (kv4p), configured identically (module A,
`local_port` 20012, the DV Dongle by-id path) — safe because acquisition is on-demand and exclusive.
The standalone ADR 0088 instance (8092) is **disabled** so it stops holding the dongle + module A
continuously. HTTPS is inherited from the existing instances, so the browser mic/speakers work with no
new cert work. **No gateway change** — module A already exists; the DVAP (module B) and gateway are
untouched.

### Why not broaden it

The **DVAP** (module B) is a separate gateway endpoint we can't see or control from our module A. A
DVAP reflector picker + confirmed link status needs the ircDDBGateway remote-control interface (off,
no code today) — that is the **next** cycle, along with gateway-confirmed link state for module A. Not
in scope here: DTMF reflector control, a D-STAR ↔ Mumble bridge, full-duplex.

## Consequences

- **One shared dongle, two radios, chosen at connect time.** The instance you link a reflector from
  wins the dongle and crossbands to its radio; a second connect while the first holds it returns a
  clean 503, not a crash or a corrupted stream. Disconnect releases it for the other radio.
- **Believed link state, still.** With the gateway remote-control interface off there is no readback;
  `active` is what we last sent (the UI says so). Confirmed state rides with the DVAP cycle.
- **`operator_tx` removed.** Existing deployments that set it will ignore it (unknown keys are
  tolerated); the folded posture (crossband + browser both live) is the only D-STAR mode now.
- **Off by default.** No `dstar.callsign` ⇒ no bridge, no hub, no dongle opened — unrelated
  deployments and the default hardware-free test suite are unaffected. The DV Dongle and live gateway
  are touched only by the opt-in doctor self-test and a linked instance.
- **Verified.** Fake-based unit tests cover the TX-owner latch (no interleave), lazy exclusive
  acquire/release + the busy-dongle 503, and the MYCALL activity path. Hardware proof on the live
  gateway is recorded in the PR.

Cross-refs: ADR 0088 (the browser reflector seam this folds into the real radios and supersedes the
`operator_tx` mutual-exclusion of), ADR 0087 (the D-STAR link + header findings), ADR 0086 (the
vocoder seam + the no-interleave hazard the latch respects), ADR 0050/0042 (the Mumble browser-client
shapes mirrored).
