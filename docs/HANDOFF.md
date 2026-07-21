# Handoff

## UV-K5 (Quansheng Dock) backend, cycle 1: the wire codec (ADR 0110) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-wire-codec`) off `d81807a` after #167 merged — not stacked.**
Kicked off the third backend: a Quansheng UV-K6 on nicsure's "Quansheng Dock" custom firmware, wired via
AIOC (serial + audio through the K1 jack, same pattern as `baofeng`). Multi-cycle goal is browser-selectable
channel switching for repeater monitoring. **Radio ordered, not yet on the bench** — pure offline protocol
work, mirroring the kv4p arc (codec first, ADR 0061 precedent).

**Pinned spec-only** (cloned + read at the exact SHA, cited `file:line`, nothing copied/ported): firmware
`nicsure/quansheng-dock-fw` **0.32.21q** = `4375c3e9604ee4c14ec4bdae67af077879a96f34` (Apache-2.0); client
`nicsure/QuanshengDock` **0.32.21q** = `851efa955740db9251811cc90195e927b52ba68c` (GPL-2.0).

**What shipped (code, PR #168):** `radio_server/backends/uvk5/frames.py` — a pure, stdlib-only codec
(imports nothing from `radio_server.*`, no I/O). Framing `[AB CD][Size][obf(payload + CRC16)][DC BA]`,
`Size+8` total: `crc16` (CRC-16/XMODEM), `obfuscate` (self-inverse XOR), `build_frame` (mirrors the client
`SendCommand2` byte-for-byte), `Uvk5Decoder` (streaming deframer modelled on the client `ByteIn`:
drop-and-resync, never raises, optional CRC validation), frozen-dataclass struct codecs for every dock
command/reply (calcsize-asserted), and `parse_frame` dispatch. **Tests — `uv run pytest`: 1355 passed,
5 skipped** (26 new in `tests/test_uvk5_frames.py`).

**Two findings from reading the pin (both recorded in ADR 0110):**
- **Reply-CRC asymmetry** — host→radio *commands* carry a real CRC (firmware validates, uart.c:1037-1039);
  radio→host *replies* carry `obf(0xFF 0xFF)`, a dummy the client's own decoder ignores (Comms.cs:181-186).
  So `Uvk5Decoder` defaults to `validate_crc=False`; the transport cycle keeps that. **Verify live replies
  decode on the bench.**
- **Pin discrepancy** — the kickoff listed `0x0872` set-modulation, but at 0.32.21q `CMD_0872_t` is defined
  and **not in the dispatch switch** (uart.c:1098-1137 has `0x0870` instead). Codec keeps `SetModulation`
  with a "verify before use" note.

**⚠ Next / gated (all out of scope this cycle, named in the ADR):** serial transport + AIOC wiring (38400
baud — verify on hardware), the HELLO/session handshake (send `0x0514` → plaintext, or stay obfuscated like
the shipped client), the `Radio`/`CatRadio` class (PTT over the AIOC serial line, never CAT), `[uvk5]`
config + factory registration + settings-API canary + `radio.toml.example` + `doctor` + backend-select UI.
**Open control-path decision for the next ADR:** (a) keypress-sim driving the radio's own memory channels +
screen readback vs (b) XVFO register-write tuning with channels as presets — the codec covers both. BK4819
freq→register mapping (regs 0x38/0x39 = low/high 16 bits of `freq_hz/10`, 0x33 band, 0x30 tuning) recorded
in the ADR for that cycle. **No instruction issue existed for this cycle** (delivered as a prompt), so the
end-of-cycle issue relabel was N/A; PR #168 is the deliverable.

## DVAP autoheal: restart-until-the-dongle-opens, in user space (ADR 0100) (2026-07-20, overnight)

**Branched fresh from `origin/master` (`dvap-autoheal-usb-wedge`) after #154 merged — not stacked.** Kris's
DVAP (441.6, A602RQT5, `dstarrepeater`) kept going **deaf** (reflector dashboard never updates); he can't
always be home to power-cycle it. Root-caused on the live bench (dummy loads):

- **The DV Access Point Dongle open-wedges** — first open after any abrupt close fails (`The DVAP is not
  responding with its serial number` → `Cannot open the D-Star modem` → dummy controller, deaf). And it
  **ALTERNATES** good/bad on each successive open (bench: `WEDGED,OK,WEDGED,OK,WEDGED`). So the old
  `dvap-autoheal.sh`'s **single unverified `systemctl restart`** healed only ~50% — and could wedge a
  *healthy* dongle. (A manual "kick" this session wedged one that had been decoding fine.)
- **No USB reset available:** no passwordless sudo, USB node is root-only, so `usbreset`/unbind need root we
  don't have. A reboot recovers both dongles but isn't an auto-remedy.

**Fix (ADR 0100, PR #<pending>):** rewrote `scripts/dvap-autoheal.sh` to **restart-until-the-log-confirms-
the-dongle-opened** (retry ≤4, verify each open via the dstarrepeater log), detecting both deaf modes —
Mode 2 open-wedge (log shows `Cannot open`) and Mode 1 re-enum stale-fd (open ttyUSB ≠ by-id target) — plus
a 60 s backoff on a truly-dead dongle. Pure user space, no root. Versioned with its `--user` unit
(`scripts/dvap-autoheal.service`). **Proven on hardware:** a wedged A602RQT5 recovered to OK in ~5 s
(`healing … (open wedge): restart 1/4` → `healed … after 1 restart(s)`), 0 restarts of a healthy dongle
(no flapping), both dongles OK after. **Deployed live** to `/home/kb/applications/dvap-autoheal.sh` (old one
backed up `.pre-usbwedge.bak`); `dvap-autoheal.service` restarted onto it.

**NB the deaf DVAP was NOT a radio-server bug** — the crossband decode path (ADRs 0097-0099) is fine. The
2nd re-proof "nothing heard" was (a) the DVAP wedging and (b) a routing mismatch (module B was linked to
REF001 C while radio-server module A / Kris watched XLX999 A). Optional future upgrade: a udev event trigger
+ `sudoers` USB-reset (needs a one-time root install) for the silent same-ttyUSB re-enum case.

## Crossband fail-safe when the DV Dongle wedges (ADR 0099) + the 2nd dummy-load re-proof (2026-07-20)

**Branched fresh from `origin/master` (`dstar-vocoder-wedge-failsafe`) after #152 + #153 merged — not
stacked.** Deployed master to the live 8090 (it was stale at #151) and ran the joint re-proof with Kris:

- **Phase 1 PASSED (mock backend, zero RF):** linked module A, keyed the ID-51A, and the DV Dongle decoded
  **intelligible voice in the browser** → **ADR 0098 decode fix confirmed by ear.** (There is no real-backend
  listen-only mode; the guaranteed-no-PTT proof runs `backend="mock"` — MockRadio.ptt drives no line.)
- **Phase 2 FAILED (baofeng, dummy load):** dead air + the transmitter stayed keyed, and `systemctl stop`
  hung ~15 s before PTT dropped. Root cause: a **wedged DV Dongle** (left bad by the Phase 1→2 restart) and,
  worse, a crossband that **did not fail safe** around it. Radio confirmed safe/down; Kris chose "fix in
  code, no more RF tonight."

**Three defects (one concern — traced, sourced), fixed here:**
- **`_recover()` reader race** — reassigned `_serial`/`_stop`/`_reader` after a 1.5 s join, so a **zombie
  reader** read the closed port → `TypeError('NoneType'…integer)` → then every exchange raised. Fixed:
  each reader is **generation-tagged** and bound to its own `serial`/`stop`; `_dispatch`/`_fail` shed a
  superseded reader (`dvdongle._read_loop`/`_spawn_reader`/`_fail`).
- **Streaming decode never recovered a wedge** (unlike legacy `_exchange`) → 1 s write-timeout **every
  frame** = dead air + parked the drain. Fixed: `_DvDongleDecodeStream` **latches wedged** and fails the
  over FAST; the dongle is healed at the **next** `open_decode_stream` (not mid-over).
- **Teardown blocked the event loop** — `_teardown` called `vocoder.close()` **synchronously before**
  `_force_unkey()`; `close()` waited ~15 s for `_io_lock` held by a live `_recover`, starving the unkey.
  Fixed: `_force_unkey()` runs **FIRST**, then `close()` runs **off-loop, bounded** (`run_in_executor` +
  `wait_for`); `DVDongleVocoder.close()` no longer blocks on a contended lock (skips the courtesy REQ_STOP).

**What shipped:** `docs/adr/0099-*.md`; `dstar/bridge.py` (`_teardown` reorder + off-loop close);
`vocoder/dvdongle.py` (reader generations, non-blocking `close`, `open_decode_stream` recover-if-failed,
`_DvDongleDecodeStream` wedge latch). Tests (+6, `uv run pytest` **1291 passed**, 5 skipped): bridge —
wedged stream ends the over & unkeys via the watchdog, teardown drops PTT before a slow close; driver —
decode-stream fail-fast latch, stale-generation `_fail` ignored, `open_decode_stream` recovers a
prior-over wedge, `close()` non-blocking under a held lock.

**Still gated:** crossband stays **disabled on the live radios**. The re-proof is **not done** — re-run it
(Phase 1 mock listen → Phase 2 dummy-load TX, Kris watching) **from a COLD-BOOTED dongle** (unplug/replug
`ttyUSB1`; never reuse it across a restart). kv4p (8091) leg still deferred behind the same gate. Live 8090
left stopped, `backend="baofeng"`, `reflector=""` (a `radio.toml.reproof-bak` backup sits beside it).

## Fix the garbled crossband decode: ordered streaming decode over the pipelined AMBE2000 (ADR 0098) (2026-07-20)

**Branched fresh from `origin/master` (`dstar-decode-pipeline-align`) after PR #152 merged — not stacked.**
The correctness half of the crossband bring-up (the safety half was ADR 0097 / PR #152). The module-A
decode came out as **garbage**; a sourced G4KLX review proved the byte path is correct (DVAP firmware
already de-scrambles; the 9 AMBE bytes go to the AMBE2000 verbatim). The real cause: the **AMBE2000 decode
is pipelined** and the per-frame `DVDongleVocoder.decode` (single-value reply slots) **dropped/mis-ordered**
frames when keyed straight onto RF.

**Bench measurement (decode-only, no keying, free dongle — recorded in ADR 0098):** `NULL_AMBE` → silence
(interface correct); latency **L ≈ 5 frames (100 ms), range 4–6**; the **dominant fault is frame DROPOUTS**
(exact-zero holes mid-tone), not the lag. A `STOP/START` reset is fragile; `_recover` is clean.

**What shipped (code):**
- `vocoder/base.py` — new optional `DecodeStream` + `StreamingVocoder` protocols (feature-detected).
- `vocoder/dvdongle.py` — `open_decode_stream()` → a `_DvDongleDecodeStream` backed by an **ordered FIFO**
  (the reader appends decoded PCM in order instead of a single-value slot); fixed prime/flush of
  `DEFAULT_DECODE_LATENCY_FRAMES=8` (≥ observed max L; marked tunable); legacy `decode()`/`encode()` kept
  so `--vocoder-loopback`/`--dstar-echo` are unchanged. No fragile per-over session reset.
- `dstar/bridge.py` — opens a fresh decode stream per over (on HEADER), feeds each AMBE through it
  (`_play_ambe` → 0..n ordered frames → the existing `to_canonical`→hub→**rx-gate (ADR 0097 preserved)**→
  `session.feed` path), and drains `flush()` on the clean end-bit (`_flush_and_end_rx`); closes the stream
  on `_end_rx`/`_force_unkey`.
- Tests (+4, `uv run pytest` 1285 passed): pipelined `FakeDongle` proves the real driver FIFO returns
  frames in order with none dropped + tail flushed; `PipelinedFakeVocoder` bridge end-to-end proves every
  frame of an over is keyed in order (the regression that would have caught the incident).

**Fast-follow (documented, not in this PR):** fold the bench script into a versioned `doctor
--vocoder-latency` subcommand + pure `latency_metrics` helper (guardrail-1 re-measurement).

**Still gated:** crossband stays **disabled on the live radios**. RE-ENABLE needs BOTH ADR 0097 (merged) and
this to land, THEN a joint dummy-load re-proof (Kris watching) — now with an added no-keying step first:
confirm **intelligible audio through the decode→`dstar_rx_hub` browser listen path** before any TX. kv4p
leg still deferred behind the same gate. NB the DV Dongle wedges after an abrupt process kill — cold-open
retries (or unplug/replug) before re-testing.

## Module-A crossband bring-up: stuck-key on the AIOC → the content-liveness + over-cap fix (ADR 0097) (2026-07-20)

**Branched fresh from `origin/master` (`dstar-crossband-deadair-cap`) — not stacked.** First supervised
dummy-load bring-up of the module-A DV-Dongle crossband on the live AIOC. Two real defects + one operator
error surfaced (full detail in memory [[dstar-stuck-key-incident]]):

- **Bug A — garbage decode (STILL OPEN).** Real off-air D-STAR AMBE from an ID-51A (via DVAP-B → XLX999 A
  → module A) decoded to **noise, not voice**, in the browser AND the FM out → the fault is the decode. A
  sourced investigation (G4KLX DummyRepeater `DVDongleController.cpp` / DV3000 / DVAPController) proved our
  byte path is **correct** — the DVAP firmware already de-scrambles/de-interleaves, and the 9 AMBE bytes go
  to the AMBE2000 **verbatim** (adding a transform would be WRONG). Prime suspect is now the **per-frame
  decode driving** in `vocoder/dvdongle.py` (each `decode()` writes a decode-AMBE *and* a dummy encode-audio
  packet, then reads single-value reply slots → pipeline/reply mis-pairing that the whole-stream loopback
  self-test never exercised). NEXT (no keying, safe): a bench diagnostic on the free DV Dongle — decode the
  standard `NULL_AMBE` frame (should be silence) and decode captured ID-51A frames as one whole stream vs
  the per-frame path — to localise driving-vs-interface, then a targeted `dvdongle.py` fix. Its own ADR/PR.
- **Bug B — stuck key (FIXED here, ADR 0097).** The over held PTT on dead air past the 180 s TOT because
  reflector→RF liveness was measured by frame *arrival*, not decoded *content* — a continuous stream reset
  the idle deadline every frame. Fix: a content `rx_gate` (AudioLevelGate on the decoded audio — dead air
  no longer counts as activity, so the over idles out in ~`tx_hang`) + a hard per-over ceiling
  `dstar.max_over_seconds` (default 60 s, < TOT; content-independent backstop for loud garbage). `bridge.py`
  + `config/spec.py` + `app.py`; `tests/test_dstar_bridge.py` +3 (28 pass); `uv run pytest` green.
- **Operator/agent error (recorded so it never repeats):** probing the AIOC PTT serial port with bare
  `pyserial` to "inspect" it **re-keyed the radio** (pyserial asserts DTR on open = PTT). Never open a PTT
  line to diagnose a stuck key. Safe-stop = `systemctl --user stop radio-server.service` + unplug the AIOC.

**State: radio-server left STOPPED on 8090; DV Dongle free; gateway/DVAPs untouched; UV-5R safe.** The
crossband stays **disabled on the live radios** — this ADR only ensures a non-terminating over can't strand
the key. **RE-ENABLE gated on: Bug A fixed AND a fresh joint dummy-load re-proof (Kris watching).** The
kv4p leg (enable `[dstar]` on 8091) is deferred behind the same gate.

## DVAP support, PR 3: the DvapPanel web card (completes ADR 0096) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-web-panel`) after PR #150 (ADR 0096 backend) merged — not
stacked.** The visible half of the DVAP tab: a card with one row per configured DVAP module (label +
frequency + **confirmed** link pill + a reflector picker + Connect/Disconnect), self-hiding when no DVAP
is configured (the `state.dvap == null` → render null pattern, same as `DStarPanel`).

**What shipped (web only, no server change).**
- `web/src/components/DvapPanel.jsx` — modelled on `DStarPanel.jsx`: mount-seed GET, 2s poll, folds the
  pushed `dvap` WS event; per-module rows keyed by letter with independent reflector inputs; module state
  pill = Linked·<reflector> / Not linked / **Unreachable** (from the confirmed `reachable`/`linked`/
  `reflector` fields). Placeholder `XLX999 A` (the private test reflector).
- `web/src/api.js` — `dvapStatus` / `dvapLink(module, reflector)` / `dvapUnlink(module)`.
- `web/src/useEvents.js` — a `dvap` case in `reduceStatus` folding `{configured, remote, modules}`.
- `web/src/components/ControlPanel.jsx` — import + slot next to `DStarPanel` (passes `state.dvap`).
- `web/src/styles.css` — `.dvap-module` row divider/spacing.

**Tests — vitest 25 passed (7 files); `npm run build` clean.** `DvapPanel.test.jsx` (5): hides when
unconfigured, renders a row per module with frequency + confirmed pill, marks an unreachable module,
Connect links the module by letter with the typed reflector, Disconnect unlinks a linked module. Added
`dvapStatus` to the `ControlPanel.test.jsx` mock client (DvapPanel now mounts inside it).

**⚠ NEXT = the operator deploy step (with Kris) — no more code until then.** All three DVAP PRs (#149,
#150, this) get the tab working end-to-end only once the gateway side is set up:
1. Enable gateway remote-control: stop `ircddbgateway.service` → add `remoteEnabled=1`, `remotePort=10022`,
   `remotePassword=<secret>` → start (stop-edit-start; one restart blips the live A/B links). Loopback → no ufw.
2. Stand up DVAP #2 as module C: new `dstarrepeater2` (localPort 20013, DVAP `A602RQXT`, `dvapFrequency=441000000`),
   gateway `repeaterCall3=AE9S/Band3=C/Port3=20013`. Add `XLX999`→104.168.125.41 to the gateway XLX/DExtra host list.
3. On 8090/8091: add `[dvap]` block + `[[dvap.modules]]` (B@441.6, C@441.0), `dvap_remote_password` in
   `radio-secrets.toml`, restart.
4. Bench-verify the ADR 0095 wire protocol against the live gateway (safe `login→GRP` read-back, no TX).
5. THE TEST: link DVAP-B/C **and** D-STAR module A to **`XLX999 A`** (private reflector, key-ups fine),
   key a D-STAR HT on 441.600 → verify it comes out DVAP-C (441.000) AND the module-A FM crossband (the DV
   Dongle decode — dummy load first per the stuck-key guardrails). See plan + [[hardware-bench]].

## DVAP support, PR 2: the control surface — config + a cached manager + `/dvap/*` (ADR 0096) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-control-surface`) after PR #149 (ADR 0095) merged — not
stacked.** Wires the remote-control client into the app: radio-server now links/unlinks/monitors the DVAP
gateway modules (B = 441.600, C = 441.000). Pure control-plane — no vocoder, no bridge, no PTT — reading
**confirmed** link state over the gateway remote-control interface. Off by default; D-STAR module A untouched.

**What shipped (code).**
- **Config** — `[dvap]` scalars `host` (127.0.0.1) + `port` (10022), both advanced (`config/spec.py`,
  +2 → settings canary 75→77 in `test_settings_api.py`). Array-of-tables `[[dvap.modules]]`
  (`module`/`label`/`frequency_hz`) modelled on `[[mumble.servers]]`: `DVAP_MODULES_KEY` + `_flatten` skip
  + `load_dvap_modules` (`config/settings.py`), `resolve_dvap_modules` fail-loud validation
  (`dstar/dvap_manager.py`). Secret `dvap_remote_password` (`config/secrets.py`). `radio.toml.example`
  regenerated (commented `[dvap]` + `[[dvap.modules]]` demo; `_add_dvap_modules_example` in `save.py`).
- **`DvapManager`** (`dstar/dvap_manager.py`) — caches confirmed state: `status()` is I/O-free (so
  `/status` never blocks), `refresh()` does the bounded UDP round-trips and marks an unanswered module
  `reachable:false` (never fails the snapshot). Errors `DvapUnknownModule` / `DvapUnavailable`.
- **API** — `/dvap/{status,link,unlink}` beside `/dstar/*`, blocking client run off the loop via
  `asyncio.to_thread`; link/unlink publish the confirmed post-refresh block as a `dvap` WS event; 404 /
  422 / 503 mapping; `dvap` embedded in `/status`, self-null when unconfigured. `create_app` gains
  `dvap_*` params; `build_app` reads modules/host/port/secret and passes a lazy `UdpRemoteControlClient`
  factory gated on `dvap_modules`; client closed on lifespan shutdown. `dvap` added to `EVENT_TYPES`.

**Tests — `uv run pytest` 1278 passed, 5 skipped.** `tests/test_dstar_dvap.py` (manager + resolver) and
`tests/test_dvap_app.py` (routes: off-by-default null, status lists modules, link→unlink round-trip with
the `AE9S   B` callsign field, 404/422/503, `/status` embed from cache, graceful degrade when unreachable).

**⚠ Next (PR 3, branch fresh from master after this merges):** the web **DvapPanel** card (`web/src/`:
`DvapPanel.jsx` modelled on `DStarPanel.jsx`, `api.js` `dvapStatus/dvapLink/dvapUnlink`, a `dvap` case in
`useEvents.js`, slotted into `ControlPanel.jsx`; vitest). Then the **operator deploy step (with Kris)**:
enable gateway remote-control (`remoteEnabled=1`/`remotePort=10022`/`remotePassword` — stop-edit-start,
one restart), stand up DVAP #2 as module C (new `dstarrepeater2`, 20013, 441.000). Then bench-verify the
ADR 0095 wire protocol against the live gateway (safe `login→GRP` read-back = confirmed state, no TX).

**TEST REFLECTOR (Kris, 2026-07-19):** a **private XLX reflector** is up for testing — **`XLX999 A`** at
**104.168.125.41** (only module A, "Test"; nobody else on it, so key-ups / dead air are fine). Ports:
DExtra 30001, DPlus 20001, xlxcore (DSRP) 10001. This is the empty-reflector target: link DVAP-B/C **and**
D-STAR module A to `XLX999 A`, key a D-STAR HT, verify the crossband FM out (the DV Dongle decode). No code
change needed — `parse_reflector("XLX999 A")` → family XLX already works; gateway needs XLX999→VPS in its
XLX/DExtra host list (deploy step). See the plan `.claude/plans/cycle-1-dv-zippy-thacker.md` and [[hardware-bench]].

## DVAP support, PR 1 of 2: an ircDDBGateway remote-control client, isolated + unwired (ADR 0095) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-gateway-remote-client`) after PR #148 merged — not stacked.**
First half of DVAP support (the "DVAP tab" roadmap item). Goal: let radio-server link/unlink/monitor the
DVAP gateway modules — which are separate `dstarrepeater` endpoints it can't see over DSRP — via the
gateway's **remote-control** UDP interface, and get *confirmed* link state (retiring module A's "believed
state" later). This PR lands only the protocol client, isolated and consuming nothing, exactly as the
vocoder (ADR 0086) and DSRP (ADR 0087) seams landed first.

**Verified server ground truth this cycle (read-only recon, `kb@192.168.1.62`):** BOTH DVAPs are present
(`A602RQT5` = module **B**, 441.600, **already a working standalone node** linked to REF001 C, passing
reflector traffic to RF right now; `A602RQXT` = **unconfigured**, becomes 441.000). The old
"DVAP not configured" note was **stale** — corrected in memory. The gateway binary has the remote-control
interface compiled in (`CRemoteHandler`, `KEY_REMOTE_ENABLED/PORT/PASSWORD`, `sendRandom`) but it is
**off** (no `remote*` keys, no listener on :10022). Module A is on 127.0.0.1:20012; DVAP-B on 20011.

**What shipped (code) — nothing wired, no config/route/startup change.**
- `radio_server/dstar/remote_codec.py` — pure wire codec (the `dsrp.py` analogue). 3-byte ASCII tags,
  all ints **little-endian** (wx `SWAP_ON_BE`). Build: `LIN` login, `SHA`+32-byte `SHA256(random_bytes ‖
  password)`, `LNK`/`UNL` (callsign8 + int32 + reflector8), `GCS`, `GRP`. Parse (never raises): `RND`,
  `ACK`, `NAK`, `CAL`, `RPT` (repeater + reconnect + reflector + N link records = confirmed state).
  `Reconnect`/`Protocol`/`Direction` enums reproduced from g4klx `Defs.h`/`DStarDefines.h` declared order.
- `radio_server/dstar/remote_client.py` — transport seam (the `client.py` analogue): `RemoteControlClient`
  Protocol; `MockRemoteControlClient` (models a tiny gateway — per-module link map so link→status→unlink
  reads back; `fail_auth` toggle); `UdpRemoteControlClient` (lazy `LIN→RND→SHA→ACK/NAK` login cached,
  bounded timeout + retries, one lock serialising the socket). Errors: `RemoteAuthError`, `RemoteTimeout`.
- ADR 0095. Source-of-truth = public g4klx `RemoteProtocolHandler.{h,cpp}` read as a spec (ADR 0086 stance).

**Tests — `uv run pytest` 1253 passed, 5 skipped.** `tests/test_dstar_remote.py` (19): codec byte-layouts
(link/unlink/hash/parse RPT/CAL/malformed), enum order, Mock link/status/unlink round-trip + fail-auth,
and the Udp client over a fake connected socket (login handshake asserts the exact SHA over the injected
random; auth caching; NAK→`RemoteAuthError`; silence→`RemoteTimeout` with the resend; close→`LOG`).

**Judge-on-the-chip, verify against the live gateway (guardrail 1):** the LE-int wire convention, the
exact `SHA256(random_bytes ‖ password)` input, and port 10022. First live check (hardware phase, after
remote-control is enabled) = a safe `login → GRP → LNK REF0xx → GRP → UNL` round-trip on **module A**
(no TX), the analog of the DSRP Echo-unit proof (corr 0.999).

**Next (PR 2, branch fresh from master after this merges):** the DVAP control surface — `[dvap]` config
(modules B@441.600 + C@441.000; remote password in `radio-secrets.toml`), a `DvapManager`, `/dvap/*`
routes + `dvap` WS event, and a `DvapPanel` web card. Then the **server deploy step (with Kris)**: enable
gateway remote-control (stop-edit-start, one restart), stand up DVAP #2 as module C (new `dstarrepeater2`,
20013, 441.000). See the plan at `.claude/plans/cycle-1-dv-zippy-thacker.md` and [[hardware-bench]].

## The AIOC transmitter can never be stranded keyed: drop the line first, unconditionally, atomically (ADR 0093) (2026-07-19)

**Branched fresh from `origin/master` (`aioc-panic-unkey`) after PR #146 (ADR 0092) merged — not
stacked.** ADR 0090/0091/0092 hardened the D-STAR *bridge*, but the crossband STILL stuck-keyed the AIOC
on real hardware (3rd time) during the dummy-load test. A **controlled carrier key-test** (bare
`pyserial` DTR toggle on the dummy load, Kris watching, no audio/dongle/bridge — `/tmp/keyd.py` stepper)
proved the decisive fact: **`dtr=False` with the serial port still open cleanly UNKEYS this AIOC.** So
the hardware is fine; the stuck-key is a SOFTWARE failure to reach `dtr=False`.

**Root causes (all in `backends/aioc_baofeng.py`, the stranding class):**
1. `ptt(False)` was guarded on `self._keyed` → a desynced flag made the watchdog's / teardown's / REST
   `/ptt off`'s safety lever **no-op**. Journal proof: `/ptt off`→200, `status.transmitting`=False, but
   the carrier stayed up until the port CLOSED (SIGKILL).
2. `_key_on` asserted the line THEN wrote the TX lead-in; a lead-in write that raised propagated with
   the line asserted but `_keyed` never set True → stranded under (1).
3. `_key_off` DRAINED the stream (`stream.stop()`) before dropping the line → a `stop()` that
   blocks/raises on an xrun'd/starved stream (the DV Dongle write-timeout wedge was failing every decode
   — `SerialTimeoutException: Write timeout`, the PR #145 `_WRITE_TIMEOUT`) kept the line asserted.

**What shipped (code) — no config/schema/canary change.**
- `_drop_line()` — a single unconditional un-key primitive: bare `setattr(line, False)` + `_transmitting
  = False`, never guarded, no drain/teardown, can't block or raise. Every un-key route ends here.
- `_key_off` drops the line FIRST via `_drop_line()`, THEN `stop()`/`close()`s the stream inside
  `contextlib.suppress` (the RF-safety inversion of ADR 0029's drain-then-drop; costs a few ms tail clip).
- `_key_on` atomic: once the line is up, the lead-in write is guarded — any failure drops the line
  (+ tears the stream down) before re-raising.
- `ptt(False)` unconditional: always calls `_key_off()` (the `if self._keyed` guard is gone).
- ADR 0093; README row.

**Tests (model the failure) — `uv run pytest` 1230 passed, 5 skipped.** In `test_aioc_baofeng.py`:
lead-in write raises → line dropped, not stranded, ptt(False) still safe; `stop()` raises → line still
dropped; the line is proven LOW before the stream is stopped (recording fake stop()); ptt(False) forces
the line low when `_keyed` is desynced. Verified the two `_key_off` tests FAIL on the old drain-then-drop.

**⚠ Does NOT re-enable D-STAR.** 8090/8091 stay `[dstar] callsign=""`. Crossband re-enable is gated on a
**JOINT dummy-load re-proof with Kris watching** — never an autonomous run (I stuck the TX 3× tonight;
`/ptt off` and `/dstar/unlink` did not physically unkey until this fix). After merge, deploy `master` to
`/home/kb/applications/radio-server{,-kv4p}` (stash local `dtmf.py`; `uv` rebuilds on start). Two open
follow-ups before crossband is trustworthy: (a) the **DV Dongle write-timeout wedge** (why FTDI writes
stall under sustained decode load — the trigger; consider not fail-hard, or pacing the feed); (b) decide
**crossband vs browser-only** posture (browser listen/talk `tx_to_rf=False` never keys the TX and already
proved out at corr 0.985). See `dstar-stuck-key-incident` memory + ADR 0090/0091/0092/0093.
## The DV Dongle recovers itself from the idle-sleep wedge (ADR 0094) (2026-07-19)

**Branched fresh from `origin/master` (`dvdongle-sleep-recover`) from `2fed662` — not stacked.** (PR #147
/ ADR 0093 — the AIOC panic-unkey — was open but not merged when this was cut; this branch is independent.
Both are needed; merge order doesn't matter as they touch different files.) The crossband stuck-keys were
*triggered* by the dongle wedging (`SerialTimeoutException: Write timeout`); ADR 0092/0093 closed the
SAFETY half, this closes the RELIABILITY half.

**No-RF bench characterisation (safe, dongle free) pinned the wedge:**
- **Sustained decode is rock-solid**: `voc_stress.py` — 3000 back-to-back decodes, **0 slow**, ~36/s.
- **The wedge is the AMBE2000 idle-sleep**: after ~2-3 s idle the chip sleeps, the next decode times out
  (`VocoderTimeout`), and it **does NOT self-wake** (all 10 post-idle gaps failed). As the bridge keeps
  feeding, the FTDI TX buffer fills → `Write timeout` (the incident signature).
- **`voc_recover.py`: close+reopen+re-handshake RECOVERS it** (idle 4s → decode fails → reopen → decode OK).

**What shipped (code) — no config/schema/canary change.** `radio_server/vocoder/dvdongle.py`:
- `_exchange` split into a recover-and-retry wrapper + the `_exchange_once` primitive. On `VocoderTimeout`
  it calls a new `_recover()` **once** and retries the frame; a second failure propagates.
- `_recover()` rebuilds the transport+session under the (now **`RLock`**) io lock: signals stop, closes
  the old port, **joins the old reader BEFORE reassigning** `self._serial`/`self._stop`/`self._reader`
  (the reader reads them by reference — a live reader would race the swap), then reopens + re-handshakes,
  retrying the flaky first open `_RECOVER_HANDSHAKE_ATTEMPTS` (3) times; a dead dongle → `VocoderUnavailable`.
- Saved `_serial_factory`/`_port`/`_baud` in `__init__` for the reopen.
- ADR 0094; README row.

**Tests — `uv run pytest` 1230 passed** (1226 master + 4 new in `test_vocoder_dvdongle.py`): a `FakeDongle`
that handshakes-but-never-answers, reopened healthy → recover-and-complete; a flaky reopen (start drops) →
handshake retry; a permanently wedged pair → timeout still propagates after one recover; an un-openable
dongle → `VocoderUnavailable`. Verified all 4 FAIL without the recover wrapper. The old
`test_exchange_times_out_when_no_reply` repointed at `_exchange_once` (so recovery doesn't consume its fake clock).

**⚠ Does NOT change the D-STAR posture.** Crossband stays disabled on 8090/8091. This only makes the
vocoder robust for whenever D-STAR runs (crossband OR browser-only). Remaining before crossband is
trustworthy: **the posture decision** (crossband-vs-browser-only — Kris's call) and a **JOINT dummy-load
re-proof** (never autonomous — the TX stuck 3× tonight). Bench scripts on the server: `/tmp/voc_stress.py`,
`/tmp/voc_recover.py`, `/tmp/aioc_keytest.py`, `/tmp/keyd.py`. See ADR 0090-0094 + `dstar-stuck-key-incident` memory.

## The parked decode can no longer hold PTT: independent watchdog + re-key guard + unconditional unkey (ADR 0092) (2026-07-19)

**Branched fresh from `origin/master` (`dstar-decode-park-fix`) after PR #145 (ADR 0091) merged — not
stacked.** ADR 0091 was verified on fakes and merged, but the **first dummy-load reflector→RF test on
the real DV Dongle stuck-keyed anyway** (`mode=rx`, `rx_frames` frozen, PTT held ~40 s to the TOT).
Two hardware realities the FakeVocoder/MockRadio can't reproduce were behind it; this PR fixes both.

**Root causes (found on the dummy load, 2026-07-19).**
1. **The idle watchdog was inline in a loop that parks.** ADR 0091 put the RX watchdog at the *top of
   the `_reflector_to_rf` loop, but that loop `await`s each decode in the single-worker executor and a
   **wedged DV Dongle decode parks the loop there** — so the loop-top check never runs and the over
   never closes. (`asyncio.wait_for` around `run_in_executor` does **not** help: on timeout it awaits
   the uncancellable executor thread to finish anyway.)
2. **`close()` could skip the unkey; a resumed decode could re-key.** `unlink` returned in ~1 s (0091's
   teardown fix held) but PTT stayed up: `TxSession.close()` transmits a Part-97 sign-off **ID before**
   `ptt(False)`, that transmit **raised** on the wedged backend, and `_force_unkey`'s `suppress`
   swallowed it — skipping the unkey. Separately a decode resuming after the watchdog closed the over
   fed a late frame and **re-keyed**.

**What shipped (code) — no config/schema/canary change.**
- `dstar/bridge.py` — new **`_rx_watchdog` task** (started beside `_reflector_to_rf` when `tx_to_rf`)
  that only ever `await`s `asyncio.sleep`, never the executor, so it drops PTT even while the decode
  loop is parked. `_play_ambe` now **drops its frame if `mode != "rx"`** (no re-key of a closed over).
  `_force_unkey` now calls `radio.ptt(False)` **directly** as a path-independent backstop.
- `tx/session.py` — `TxSession.close()` wraps the sign-off ID `transmit` in try/except so a raise can
  **never** skip the `ptt(False)` / arbiter release beneath it. Hardens **every** streaming keyer
  (browser TX + Mumble bridge too), not just D-STAR.
- ADR 0092; README row.

**Tests (now model the hardware failure) — `uv run pytest` 1226 passed, 5 skipped.**
- `test_parked_decode_still_drops_ptt_via_the_independent_watchdog` — a `_BlockingVocoder` parks the
  decode in the executor; the over closes **on its own** (no teardown) while it's still parked. Proven
  to FAIL if the watchdog task is removed.
- `test_late_decode_does_not_rekey_a_closed_over` — a decode released after the over closed does not
  re-key (rx_frames unchanged, mode stays idle).
- `test_txsession_close_drops_ptt_even_if_signoff_id_transmit_raises` — a spy radio that raises on the
  sign-off transmit; `ptt_log == [True, False]` (unkey still lands).

**⚠ Re-enable is STILL gated + still a post-merge deploy step.** D-STAR remains **disabled** on both
live radios (`[dstar] callsign=""`). After this merges: deploy `master` to `/home/kb/applications/
radio-server{,-kv4p}` (preserve local `dtmf.py` edits via `git stash`; `uv` rebuilds on start), then
re-run the **dummy-load reflector→RF** test on 8090 (power-cycle the DV Dongle first — it wedges after
abrupt kills) and watch the over close + PTT drop after each over before going back on the antenna.
The 8090 checkout was already moved to `cab1d89` (PR A+B) during the incident; it needs this PR's
commit too. See `dstar-stuck-key-incident` memory + ADR 0090/0091/0092.

## D-STAR folded into the real radios: shared DV Dongle, crossband + browser, activity log (ADR 0089) (2026-07-19)

**Branched fresh from `origin/master` (`dstar-folded-shared-dongle`) after PR #142 (ADR 0088) merged —
not stacked.** Moves the ADR 0088 browser reflector seam off its standalone MockRadio node (8092) onto
the two real instances (AIOC 8090, kv4p 8091). Link a reflector from an instance's own HTTPS web UI →
that instance crossbands it to **its** radio's frequency **and** the browser; talk onto the reflector
from **either** the PC mic **or** that FM radio. Both tabs show a live **activity log** of who's heard.

**What shipped (code).**
- `dstar/bridge.py` — a **TX-owner latch** (`_tx_source` `rf`/`op`) so the crossband RF pump and the
  browser mic coexist (one talker owns the over, the other drops; no interleave into one DSRP session)
  — supersedes ADR 0088's `rx_to_reflector = not operator_tx` exclusion. **Lazy exclusive acquire**:
  `start()` creates the vocoder from a **factory** (opens the DV Dongle with `pyserial exclusive=True`)
  *first*, then registers the gateway + launches tasks; `stop()` closes the vocoder + releases the port.
  Start/stop now follow the reflector link, not the app lifespan. New `on_activity` callback fires on
  each inbound header (parsed MYCALL) and each of our own overs.
- `dstar/manager.py` — `connect` calls `bridge.start()` (a busy dongle → `VocoderUnavailable` →
  `DStarUnavailable`/**503**), `disconnect` sends the unlink then `bridge.stop()` (releases the dongle).
- `vocoder/dvdongle.py` — `_default_serial_factory` opens `exclusive=True` (the cross-process arbiter).
- `api/app.py` — bridge built at boot but **not started**; `_on_dstar_activity` → an `activity` WS event
  + a 30-entry ring on `/dstar/status`; `rx_to_reflector=True` always; `dstar.operator_tx` removed.
- Web: `dstarMode` from `state.dstar.active` (link state, not a flag); new `DStarActivityLog` card;
  `useEvents` folds an `activity` case. `web/dist` rebuilt.
- Config: `dstar.operator_tx` removed (**canary 75→74**); `radio.toml.example` regenerated.
- ADR 0089; `doctor --dstar-browser-echo` updated for the factory constructor.

**No gateway change this cycle** — module `AE9S A` (127.0.0.1:20012) already exists from ADR 0088, so the
DVAP (module B) and the gateway are untouched.

**Hardware-proven on the LIVE gateway (2026-07-19), from a throwaway branch checkout, production
untouched.** `radio-server-dstar` (8092) was stopped only briefly to free the DV Dongle, then restarted
(NRestarts=0). Two objective proofs on `kb@192.168.1.62`:
- **Browser round trip (the refactored bridge):** `doctor --dstar-browser-echo` — `send_operator_audio`
  → DSRP → live gateway Echo → in-bridge decode → `dstar_rx_hub`, staircase **pitch correlation 0.985**
  (194 frames talked, 195 heard). Exercises the factory-created vocoder, the exclusive open, lazy
  `start()`, the `op` TX-owner path, the keepalive, and the decode→hub listen path on real hardware.
- **Shared-dongle arbiter (plan risk #1):** two exclusive `DVDongleVocoder` opens of the real dongle —
  first opens + handshakes, **second is REJECTED** (`VocoderUnavailable`, EBUSY), re-open after release
  works. This is exactly the 503 "in use by the other radio" the manager surfaces; the OS serial
  exclusive lock is a reliable cross-process arbiter on this FTDI.
- After the proofs: 8090 (PID 1851), 8091 (1850), gateway (1832), DVAP/`dstarrepeater` (1833) all at
  baseline PIDs, NRestarts=0; 8092 back up.

**⚠ Rollout to 8090/8091 is a POST-MERGE deploy step, deliberately NOT done headlessly.** Discovered:
**both production instances are on STALE master (`87407ae`, PR #139 — pre-vocoder/pre-D-STAR) with
uncommitted local edits** (8090: `dtmf.py`; 8091: `dtmf.py`, `entries.py`, `update-radio-server.sh`).
They have no `[dstar]` support at all yet. Folding D-STAR in means upgrading each checkout across the
merged 0086/0087/0088 PRs **plus this one**, and reconciling those local edits — I won't clobber
uncommitted production changes or run production on an unmerged branch. **After this PR merges:** for
each of `/home/kb/applications/radio-server{,-kv4p}` — preserve the local edits (`git stash`), update to
`master`, `npm --prefix web run build`, add a `[dstar]` block (`callsign=AE9S`, `module=A`,
`gateway_host=127.0.0.1`, `gateway_port=20010`, `local_port=20012`,
`vocoder_port=/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_A602RQNI-if00-port0`), restart, then
**disable** `radio-server-dstar.service` (8092) so it stops holding the dongle + module A. HTTPS is
already configured on both (`/home/kb/applications/radio-server/tls/radio-{cert,key}.pem`), so the
browser mic works with no cert work. Only one instance holds the dongle at a time; a second connect
returns the clean 503.

**Next cycle:** the **DVAP tab** — its own gateway module (B), needs the ircDDBGateway **remote-control
interface** enabled (config + restart + a new protocol client) for a reflector picker + confirmed link
status (which also fixes ADR 0088's believed-state guess for module A).

**Unit tests green** (`uv run pytest`: 1207 passed; web vitest: 19 passed): TX-owner latch (no
interleave), lazy exclusive acquire/release + busy-dongle 503, the MYCALL activity path, `dstarMode`.

---

## D-STAR in the browser: reflector picker + talk/listen with mic/speakers (ADR 0088) (2026-07-19)

**Live-gateway-proven. Answer: YES — you can open a web page, pick a reflector, hit connect, and talk +
listen in the browser, no D-STAR radio.** Branched fresh from `origin/master` (`dstar-reflector-browser`),
after PR #141 (ADR 0087) merged — not stacked. Extends the ADR 0087 link to the usable goal (the
D-STAR analogue of ADR 0050's web-UI-as-Mumble-client).

**What shipped.**
- `dstar/bridge.py` — `send_link_command(urcall)` (synchronous, idle-gated URCALL burst; `NULL_AMBE`
  only, no vocoder) for reflector link/unlink; `send_operator_audio`/`end_operator_over` (the
  browser-mic TX seam, the `MumbleBridge.send_operator_audio` twin); a shared `_alloc_session_id`; and
  a **vocoder keepalive** (see the bug below).
- `dstar/manager.py` — `DStarLinkManager`: `"REF001 C"` → URCALL `REF001CL` (REF/XRF/DCS/XLX differ
  only by prefix; unlink = `"       U"`), **believed** link state (no gateway readback), a `dstar` WS
  event. One bridge, not a per-entry factory.
- `api/app.py` — `WS /audio/dstar/{rx,tx}` (`dstar_rx_hub` + a distinct `dstar_talk_slot`) and
  `POST /dstar/{link,unlink}` + `GET /dstar/status` + a `/status` `dstar` block; all behind the
  `dstar.callsign` gate; `rx_to_reflector = not dstar.operator_tx`.
- Config: one new key `dstar.operator_tx` (browser-operator posture); `dstar.reflector` is now the boot
  auto-link. **canary 74→75**; `radio.toml.example` regenerated.
- Web UI: `DStarPanel` reflector picker (free-text + presets) + a `dstarMode` pointing Monitor/Transmit
  at `/audio/dstar/*` (reuses the `path`-parameterized `useRxAudio`/`useTxAudio`).
- `doctor --dstar-browser-echo` — the browser acceptance (real `send_operator_audio` → gateway Echo →
  in-bridge decode → `dstar_rx_hub`, staircase pitch metric).

**The live-gateway proof (this cycle), nothing else disturbed.** The production ircDDBGateway got the
one approved change — a second homebrew module `AE9S A` on `127.0.0.1:20012` (config
`/home/kb/applications/dstar-gateway/ircddbgateway`, backed up to `*.pre-dstar-cycle3.bak`) — and a
single restart; the DVAP re-registered, `REF001 C` link returned. Throughout, `radio-server` (8090),
`radio-server-kv4p` (8091), and `dstarrepeater` (the DVAP) kept **baseline PIDs, `NRestarts=0`**.
- **Reflector link:** `POST /dstar/link {"reflector":"REF030 C"}` → gateway logged `Link command from
  AE9S A to REF030 C issued via UR Call` → `D-Plus ACK received` → `D-Plus link to REF030 C
  established`. A **header-only** URCALL links a live reflector (`command_frames` default 0 — no
  fallback ladder needed; the central verify-on-hardware unknown, resolved).
- **Browser talk+listen:** the round-trip doctor tracked the nine-tone staircase at **pitch
  correlation 0.88** through the real decode → `dstar_rx_hub` (a bare-AMBE-tap variant: 1.000).
- **WS endpoints** serve with `?token=` auth; the SPA serves the reflector picker.

**Bug found + fixed here — AMBE2000 idle sleep.** ADR 0087 wrongly assumed a live bridge never has the
record-then-replay idle gap. A browser *listener* sitting idle lets the chip go unresponsive after
~2–3 s (bench: OK at 2 s, timeout at 3 s), so the first inbound over's decode timed out and the whole
over was lost. Fix: an **idle-gated keepalive** decoding `NULL_AMBE` every ~1.2 s while IDLE (gated to
idle so it never interleaves with a live stream — the ADR 0086 hazard). Post-fix: no decode timeouts.

**The dedicated instance (how Kris uses it).** A new user service **`radio-server-dstar.service`** runs
a checkout at `/home/kb/applications/radio-server-dstar` (branch `dstar-reflector-browser`), MockRadio
backend, **port 8092**, `dstar.operator_tx=true`, DV Dongle vocoder on `ttyUSB1`. **Open
`http://192.168.1.62:8092`, log in with the token in that dir's `radio-secrets.toml`, use the "D-STAR
reflector" card to Connect a reflector, then Listen / Talk.** It owns the DV Dongle — stop it before
running `doctor --dstar-*` (both want the dongle). Left unlinked and idle for Kris.

**Follow-ons (noted in ADR 0088):** gateway-confirmed link state (wire the DSRP `TEXT`/`STATUS`
packets — the parser already decodes them), DTMF reflector control, D-STAR ↔ Mumble bridge. The
per-over staircase glitch on 2/9 steps (corr 0.88) is a minor pipeline/keepalive artifact on sharp
tone edges — benign for speech; worth a look if a cleaner metric is wanted.

## D-STAR link: radio-server ⇄ ircDDBGateway through the DV Dongle vocoder (ADR 0087) (2026-07-19)

**Hardware-proven. Answer: YES — radio-server talks and listens on D-STAR through the DV Dongle.**
PR #140 (ADR 0086 acceptance) is merged (`origin/master` tip `cd376f7`); branched fresh (`dstar-link`),
not stacked. The first live consumer of the vocoder seam.

**What shipped.** A new `radio_server/dstar/` package (sibling to the Mumble link):
- `dsrp.py` — pure, I/O-free DSRP repeater↔gateway wire codec (register/poll/header/data + parser).
- `header.py` — the 41-byte D-STAR radio header + CRC-16/X-25 (verified against the g4klx table).
- `client.py` — a `GatewayClient` seam: `MockGatewayClient` + a `UdpGatewayClient` (socket + daemon
  reader + register/poll timers, `_socket_factory`/`_clock` seams).
- `bridge.py` — a half-duplex reflector↔RF state machine. A **mode latch (IDLE/RX/TX)** drives the
  single AMBE2000 one direction at a time (D-STAR's one-talker reality + the ADR 0086 no-interleave
  rule); keys RF via the shared `TxSession`/`TxSlot`/`station_id`, pulls RF via the pump demand +
  `AudioHub`; resample lives at the bridge edge, never in the vocoder.
- Config `[dstar]` group **OFF by default** (inert until `dstar.callsign` is set); wired into
  `create_app`/`build_app` behind that gate. **canary 66→74**; `radio.toml.example` regenerated.
- `doctor --dstar-echo` — the hardware self-test (RF PCM → AMBE → DSRP → gateway Echo → AMBE → PCM),
  reusing the vocoder staircase pitch metric.

**The bench proof (this cycle).** On the real DV Dongle against a throwaway echo-only ircDDBGateway (a
named second instance, isolated on loopback so the production gateway/DVAP were never touched —
`NRestarts=0` throughout): 194 frames sent, 195 echoed AMBE frames decoded back, **pitch correlation
0.999** across the nine-tone staircase, aligned at +11-frame latency. WAV captured. Two facts the
hardware pinned (guardrail 1), both now encoded + tested:
1. **On-wire header order** is RPT2 (gateway) in slot 1 (offset 3), RPT1 (module) in slot 2 (offset
   11) — the gateway matches the incoming repeater by RPT1; the reversed order logs "Header received
   from unknown repeater". (`test_dstar_header::test_on_wire_field_order_*` pins the raw offsets.)
2. **The AMBE2000 stops responding after a short idle** — the gateway's record-then-replay leaves a
   gap between the last encode and first decode, so `doctor --dstar-echo` **reopens the vocoder** for
   the decode phase (fresh handshake wakes the chip). A live bridge never has this gap (RX/TX are
   separate live streams). The `--vocoder-loopback` (back-to-back) stays correct and green.

**Bench setup note (for reproducing the proof).** ircDDBGateway has a single-instance lock keyed by
its optional positional *name*; a throwaway runs as `ircddbgatewayd -confdir <dir> -logdir <dir> NAME`
reading an INI `[NAME]` section from `<dir>/ircddbgateway_NAME`, with `gatewayAddress` on a spare
loopback IP + all reflector/ircddb protocols disabled + `hbPort` on a free port, so it never collides
with (nor disturbs) the production gateway. The HB (homebrew) repeater protocol on port 20010 **is**
DSRP. The gateway's Echo unit is on by default; UR=`"       E"` triggers it. No registration needed.

**Full suite: 1175 passed, 5 skipped.** Next: wire radio-server to the **live** DVAP gateway (operator
step — add a second repeater module + restart ircDDBGateway, a brief DVAP blip), then DTMF/web
reflector control and a D-STAR↔Mumble bridge over this now-known-good seam.

---

## DV Dongle vocoder — hardware acceptance + loopback fix (ADR 0086) (2026-07-19)

**Verified on the real DV Dongle. Answer: YES — it encodes/decodes working audio.** PR #139 (the seam)
is merged (`origin/master` tip `87407ae`); branched fresh for this acceptance cycle, not stacked.

**What the bench proved.** On the deployment dongle
(`/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_A602RQNI-if00-port0`, 230400 8N1): the handshake and the
AMBE2000 D-STAR config bytes are correct **as reimplemented — no protocol byte needed changing**. A
streaming loopback of a 9-tone staircase (300–1500 Hz) round-trips with **pitch correlation 1.00,
median error ~8 Hz**, reproducibly across runs. Pure ~300 Hz is the one weak tone (near AMBE's low
speech-model edge) but recovers within tolerance.

**The real find (a genuine bug the hardware exposed).** The AMBE2000 is a **pipelined, full-duplex**
chip: encode and decode must each be driven as a *continuous stream*. The shipped loopback interleaved
`decode(encode(frame))` per frame, which scrambles time-varying audio (pitch correlation ~0, gross
errors like 450 Hz → 3217 Hz). A single steady 600 Hz tone is invariant to that scrambling, which is
exactly why the first bring-up "passed." The round-trip also carries a **constant but session-varying
latency** (~0–18 frames).

**Fix (this cycle, PR against master):**
- **`doctor.py` `_vocoder_loopback`** rewritten: a **staircase-of-steady-tones** probe (+ flush frames),
  **encode the whole stream then decode the whole stream**, and a **lag-aligned per-step pitch-tracking**
  metric (`staircase_pitch_metrics` / `_synth_staircase_pcm` / `VocoderMetrics`, all pure) with a 0.8
  correlation pass threshold and a wide energy band. Replaces the old single-tone `vocoder_roundtrip_metrics`.
- **`vocoder/dvdongle.py`** — docstring hazard note: never interleave `encode`/`decode` per frame; the
  seam API is unchanged and correct (a real TX-encodes / RX-decodes path never interleaves).
- **Tests** (`tests/test_doctor.py`) rewritten for the new pure metric: identity tracks (corr 1.0, lag 0),
  a delayed round-trip is recovered by the lag search, a fixed buzz / silent output fail. Fake-serial
  `test_vocoder_*` suites untouched and green. Full suite: 1139 passed, 5 skipped.
- **No schema touched** — `radio.toml.example` byte-identical, canary unmoved (still unwired).

---

## The vocoder seam: PCM ⇄ AMBE over the DV Dongle, isolated + unwired (ADR 0086) (2026-07-18)

**No hardware, no keying; built/tested against fakes + a fake clock.** PR #138 (ADR 0085) is merged
(`origin/master` tip `56eed3f`); branched fresh (`vocoder-dvdongle-seam`), not stacked.

**Why now:** a future digital-voice/D-STAR path needs a vocoder (8 kHz speech PCM → compressed voice
frame and back). It's the one piece that can't be proven by inspection, so it lands **alone and
unwired**, before any framing/socket/bridge plumbing — the same posture the reverted Codec2 seam took
to open the old M17 arc.

**What shipped (new `radio_server/vocoder/` package):**
- **`vocoder/base.py`** — the seam: a `@runtime_checkable` `Vocoder` `Protocol` (`encode(frame)->bytes`
  / `decode(bytes)->AudioFrame`, one 20 ms frame; `close()`), the 8 kHz geometry constants, and
  `VocoderUnavailable` / `VocoderTimeout`. **The seam is native 8 kHz, not the app's 48 kHz canonical**
  — every real vocoder (AMBE2000/AMBE3000/Codec2/Griffin) is 8 kHz, so the 48k⇄8k resample belongs at
  the *future consuming backend's* edge (reuse `audio/resample.py`), not in the vocoder. Deliberate
  departure from the Codec2 seam (which resampled internally). Frame stays a fail-loud `AudioFrame`.
- **`vocoder/frames.py`** — pure, I/O-free DV Dongle wire codec (the kv4p `frames.py` split). Framing is
  a 2-byte LE header = 13-bit total length + 3-bit type (`length = word & 0x1FFF`, `type = word >> 13`),
  confirmed against every reference constant. The 48-byte AMBE payload = a 24-byte AMBE2000 **D-STAR
  full-rate config block** (verbatim from the reference) + the 9-byte voice frame at offset 24. Streaming
  length-prefixed deframer that resyncs past garbage.
- **`vocoder/dvdongle.py`** — `DVDongleVocoder`, kv4p-transport pattern: lazy pyserial behind `hardware`
  + `_serial_factory` seam; daemon reader thread (read→deframe→dispatch) bounded by a stop `Event`;
  fatal-read path wakes waiters; `Condition`-guarded reply hand-off; idempotent best-effort `close()`.
  Bring-up = `open()` (query name) → `start()`; AMBE config rides every AMBE packet. v1 = synchronous
  query/reply per frame. Port/baud (230400) are marked-verify module constants.
- **`doctor.py`** — new `--vocoder-loopback` (+ `--vocoder-port`), handled before the backend split
  (drives a separate FTDI device, not the radio; like `--analyze-wav`). Synthesize 8 kHz PCM → encode →
  decode → `to_canonical` → `_write_wav_mono16`; reports a pure `vocoder_roundtrip_metrics` (frame count,
  in/out RMS ratio, dominant-tone — **lossy, never sample equality**). Reuses `_Report` + the WAV writer.
- **Protocol source:** g4klx/DummyRepeater `Common/DVDongleController.cpp` (GPL-2) read as a **spec** and
  reimplemented clean — no ported code. AMBE vocoding stays on the licensed DVSI chip, so no codec and no
  patent/copyleft exposure in-tree.
- **Docs:** ADR 0086, README index row, this handoff. **`pyproject.toml`**: one-line note that the DV
  Dongle rides the existing `serial`/`hardware` extra (no new extra).

**Explicitly NOT done (unwired, per the issue):** no `[vocoder]` config group (so **no canary bump,
`radio.toml.example` byte-identical**), no factory registration, no `backend_config.py` kwargs, no
`Radio` backend, no reflector/D-STAR-header/bridge/DTMF work.

**Verified:** `uv run pytest` → **1137 passed, 5 skipped** (+37: `test_vocoder_frames.py`,
`test_vocoder_dvdongle.py` — a request/response `FakeDongle` proving handshake + per-frame codec +
fail-loud guards + missing-pyserial; extended `test_doctor.py` for the metric + argparse wiring).
`radio.toml.example` byte-identical, canary unmoved.

**Bench acceptance (operator, NOT headless — the risky unknown):** plug in the DV Dongle and run
`uv run python -m radio_server.doctor --vocoder-loopback --vocoder-port <by-id> --out vocoder-loopback.wav`.
Acceptance = the handshake + AMBE2000 config succeed and the written WAV is **intelligible** (DVTool's
"Audio Loopback Only" equivalent). Cross-check port/baud/config bytes and the metric threshold against
DVTool; correct any constant that differs and update its verify note.

## A post-transmit RX guard: keep the TX→RX turnaround transient off Mumble (ADR 0085) (2026-07-18)

**No hardware, no keying; built/tested against fakes + a fake clock.** PR #137 (ADR 0084) is merged
(`origin/master` tip `2ae1295`); branched fresh (`mumble-rx-guard`), not stacked.

**The bug (AIOC-only, understood):** talk from the phone (Mumble→RF), release PTT, the radio drops —
then the phone hears a ~0.25 s buzz. It's the UV-5R receiver recovering at the TX→RX turnaround (FM
hash before its squelch settles), captured by the AIOC sound card and relayed because
`audio.squelch="off"` passes everything. The duplex arbiter resumes RX the instant TX releases with no
guard — the exact timing its docstring (`arbiter/state.py:22-23`) says is "a bench fact, not modeled
here." kv4p is immune (SA818 hardware squelch keeps the transient off the wire).

**What shipped:**
- **`api/app.py`** — an app-scoped `rx_guard = DtmfMuteGate()` (the ADR 0049 timed latch reused a
  third time). The arbiter's `on_change` now tracks the prior mode and arms it (`mute_for`) when
  leaving `TRANSMITTING`. Keying off the **arbiter** (source-agnostic) means a **browser talker's**
  release arms it too, not just the Mumble bridge's own TX — both funnel through `release_tx()`.
  Threaded `mumble_rx_guard_seconds` from settings and passed `rx_guard=` into the bridge factory.
- **`link/bridge.py`** — new injected `rx_guard` param; `_rx_to_mumble` (both branches) drops the
  frame while `rx_guard.muted()`, counted as `rx_guarded` in `tx_stats()` (`/link/status`). Suppresses
  **only** the Mumble feed — browser Listen (a separate hub subscriber) and the recorder are untouched
  (recording never loses audio). `None` = no guard (unchanged relay).
- **`link/client.py`** — `DEFAULT_MUMBLE_RX_GUARD_SECONDS = 0.4` (marked verify-on-bench).
- **`config/spec.py`** — `mumble.rx_guard_seconds` (float, `coerce_nonneg_float` so **0 disables**),
  advanced tier beside `mumble.tx_hang`. canary 65→66; `radio.toml.example` regenerated.
- **Web (bundled UI ask):** all Settings-screen collapsibles now default **collapsed** — the basic
  GroupPanels (`SettingsView.jsx`, `open`→`open={false}`) and the Mumble-servers panel
  (`MumbleServersPanel.jsx`, dropped the bare `open`) joined the already-collapsed advanced tier.
- **Docs:** ADR 0085, index row, `troubleshooting.md` ("a short buzz on Mumble right after I stop
  talking (AIOC)" + the try-your-squelch-first note), this handoff.

**Verified:** `uv run pytest` → all green (see the run); `cd web && npm test` + `npm run build` green.
Tests (fakes + fake clock): suppress-then-resume across the guard window (the buzz regression), `0`
disables, the guard arms on a plain arbiter TX→RX release (the browser-talker path) and **not** on
RX→IDLE / IDLE→RX, and a `rx_guard=None` bridge relays exactly as before (Listen/recording unchanged).

**Bench acceptance (operator, not headless):** on the AIOC over Mumble, talk from the phone and
release — the post-release buzz is gone; a fast back-and-forth doesn't clip the reply's start (tune
`mumble.rx_guard_seconds`). kv4p unaffected. **Do not key headless.**

**Non-goals:** no kv4p change, no `mumble.tx_hang` change (that's the Mumble→RF quiet window; this is
the RX side after TX), no AGC/noise-gate, no new backends. Browser Listen could reuse the same latch
if it shows the same buzz — noted in the ADR, not broadened without cause.

## Make the kv4p RECEIVE path a continuous stream — RX mirror of the TX pacer (ADR 0084) (2026-07-18)

**No hardware, no keying; built/tested against fakes.** PR #136 (ADR 0083) is merged (`origin/master`
tip `0058d51`); branched fresh (`kv4p-rx-continuous-silence`), not stacked.

**The bug (mirror of ADR 0082, code-confirmed on both backends):** over Mumble via kv4p, the tail of a
*received* transmission loops ("Max Headroom") when a signal ends — not on the AIOC. The AIOC reads a
continuous sounddevice capture, so `receive()` always returns full-length audio (silence between
transmissions); the frame-push kv4p returned an **empty** frame (`AudioFrame(b"")`) on its idle-poll
timeout. The shared `RxPump` (`rx/pump.py:230` `if frame.samples:`) skips empty frames *before* the
activity gate, so the VAD gate's **hang** (`activity/gate.py`, which is what publishes the trailing
taper) never ran — the RF→Mumble feed (subscribed to the same `AudioHub`, `link/bridge.py:187`)
stopped abruptly and the far-end Mumble/phone client concealed the gap by looping the tail.

**What shipped:**
- **`backends/kv4p/radio.py`** — `receive()` returns a full-length canonical silence frame
  (`_RX_SILENCE`, 1920 zeros — the shape a decoded packet yields) on the idle-poll timeout instead of
  an empty frame. A real packet still returns immediately; only the idle path changed (empty →
  silence). This is a backend-level, gate-agnostic change: the RX stream is now continuous like the
  AIOC, so the pump/gate/recording/DTMF treat kv4p identically (no backend branching anywhere).
- **`doctor.py`** — `measure_rx_levels` now skips fully-silent (all-zero) frames, so the continuity
  fill and true inter-transmission silence can't dilute the avg RMS or inflate the ADR-0070
  `--rx-level` ADC-clock estimate. Measures real received audio only.
- **Tests:** `test_kv4p_radio.py` — idle queue → full-length silence (not empty; the regression),
  burst-then-idle → a continuous full-length stream tapering to silence, and the idle silence reads as
  zero-RMS so the `AudioLevelGate` holds open through its hang then **closes** (taper, not latch).
  `test_doctor.py` — `measure_rx_levels` skips all-zero frames. Existing kv4p RX / switch / TX-pacer
  (0082) tests stay green.

**Verified:** `uv run pytest` → **1100 passed, 5 skipped** (+3 net). No schema change
(`radio.toml.example` byte-identical, canary unmoved). A corrupt-packet decode still returns an empty
frame (a wire error the pump skips) — only the *idle* path fills silence.

**Bench acceptance (operator, not run headless):** over Mumble on the kv4p, someone keys the frequency
and stops — the tail no longer repeats in the Mumble app. AIOC behaviour unchanged.

**Cadence note for the bench:** the firmware sends nothing when idle, so the continuity silence is
produced at the `receive()` idle-timeout cadence (`DEFAULT_RECEIVE_TIMEOUT` = 0.1 s), not real-time —
enough to break the *sustained* loop. If the taper isn't smooth enough, lowering
`DEFAULT_RECEIVE_TIMEOUT` toward the 40 ms frame interval is a follow-up (kept at 0.1 s so a healthy
signal's inter-packet jitter never trips the timeout mid-signal).

**Non-goals:** no TX change (ADR 0082 owns kv4p→RF), no AIOC change, no `mumble.tx_hang` change, no new
backends.

## A fixed over-RF login code option + a collapsible Mumble panel (ADR 0083) (2026-07-18)

**Settings-screen cycle: a UI tidy + an opt-in auth mode. No hardware, no keying.** PR #135 (ADR
0082) is merged (`origin/master` tip `4e41b4b`); branched fresh (`settings-fixed-login-code`), not
stacked. Two operator asks on the Settings screen. (A third — "surface toml settings not on screen" —
was withdrawn by the operator as a mistake; all scalar settings already render, some behind the
Advanced fold.)

**1. Mumble servers panel now folds like the rest.** `web/src/components/MumbleServersPanel.jsx` — the
bespoke `<section className="card">` became the same `<details className="settings-group">` /
`<summary>Mumble servers <span className="settings-group-count">N servers</span></summary>` /
`.settings-group-body` shape the schema `GroupPanel` uses (CSS already existed). Native `<details>`,
no state added. `SecretsPanel` left as an open card (holds set-up actions; only Mumble was asked).

**2. A fixed 6-digit over-RF login code (opt-in, non-default, warned).** Auth is now a derived mode —
off / TOTP / fixed:
- **`auth.fixed_code`** (new bool setting, default false; `spec.py`, `auth` group, NOT advanced) beside
  the unchanged `auth.totp_enabled` gate. Description carries the security warning. Canary 64→65;
  `radio.toml.example` regenerated.
- **`radio_server/auth/fixed.py`** — `FixedCodeVerifier`: same `verify_and_burn(code, now)` surface
  `AuthGate` consumes, constant-time compare, **no burn** (a fixed code is reused → replayable: the
  documented downgrade). Exported from `radio_server/auth/__init__.py`.
- **Wiring:** `build_controller` gains `fixed_code=` and picks `FixedCodeVerifier` vs `TotpVerifier`
  by `load_fixed_code_enabled(settings)`; new `Controller.auth_method` ("fixed"/"totp"). `build_app`'s
  controller-build gate also builds in fixed mode when a code is set (byte-identical when off). The
  code is a **secret** (`fixed_code` / `RADIO_FIXED_CODE`, `config/secrets.py`), never in `radio.toml`.
- **API:** `POST /settings/secrets/fixed-code` (write-only, 6-digit-validated, `api/settings.py`);
  `_secrets_presence` reports `fixed_code` set/unset; `GET /auth/totp` returns `{enforced:true,
  fixed:true}` in fixed mode and **never** echoes the code (503 if selected-but-unset).
- **UI:** `SecretsPanel` gains a write-only 6-digit **Fixed login code** control + inline warning;
  `TotpCard` shows a locked "fixed code" chip (no rotating code); `api.js` `setFixedCode`.

**Verified:** `uv run pytest` → **1097 passed, 5 skipped** (+17: `test_fixed_code.py` verifier+build,
fixed-mode `/auth/totp`, the settings endpoint + presence, secret round-trip). `cd web && npm test` →
**19 passed** (+ `SecretsPanel`/`TotpCard`/`MumbleServersPanel` suites); `npm run build` green. Canary
64→65; `radio.toml.example` regenerated (golden green).

**Docs updated:** `configuration.md` (fixed-code how-to + warning), `using-it.md` (login-code note),
`operating.md` (security implication — no burn, replayable), `api.md` (the new endpoint + `/auth/totp`
`fixed` field), ADR 0083 + index row, this note.

**Non-goals:** no change to TOTP behavior when `auth.fixed_code` is off (existing configs unchanged),
no `auth.totp_enabled` rename/migration, no CLI enrollment for the fixed code (UI/secrets-file/env
only).

## Keep the kv4p transmitter fed while keyed-but-idle — a TX pacer (ADR 0082) (2026-07-18)

**No hardware, no headless keying; built/tested against the fake transport + a deterministic clock.**
PR #134 (ADR 0081) is merged (`origin/master` tip `3047715`); branched fresh
(`kv4p-keyed-idle-silence`), not stacked.

**The bug (code-confirmed on both backends):** over Mumble via kv4p, the last ~0.5 s of speech
repeats at the end of each over (the "Max Headroom" loop) — not on the AIOC. The AIOC opens a
continuous sounddevice output stream that clocks silence out whenever `transmit()` isn't writing
(`aioc_baofeng.py::_key_on`), so its TX buffer never starves. The kv4p is frame-push: `transmit()`
sends Opus frames only when called. When the Mumble bridge (`link/bridge.py::_mumble_to_rf` →
`tx/session.py::TxSession`) holds `ptt(True)` across a `mumble.tx_hang` quiet window
(`DEFAULT_MUMBLE_TX_HANG = 0.8 s`) but stops delivering audio, the kv4p sends nothing while still
keyed, the SA818's TX buffer underruns, and the firmware loops its last content. The browser
`/audio/tx` talker (same `TxSession`) had the identical latent bug.

**What shipped:**
- **`backends/kv4p/pacer.py`** (new) — `_TxPacer`: owns the per-keying `TxAudioEncoder` + a bounded
  drop-oldest PCM jitter buffer. `enqueue(pcm)` (non-blocking) feeds it; a **daemon thread** calls
  `tick()` every ~40 ms (`FRAME_MS`) and sends **exactly one** frame per slot — real audio if a whole
  frame is buffered, else one **encoded-silence** frame (reuses the key-up lead-in's
  silence-through-the-encoder path; zeros are `tx_gain`-invariant). `stop()` joins the thread;
  `flush_tail()` (caller thread, post-join) drains the remainder + flushes the encoder tail. It is a
  **daemon thread** (not an asyncio task) because the pacer must fire while the bridge's async task is
  parked in `wait_for(..., timeout=tx_hang)` — the transport-reader-thread / AIOC-output-stream shape.
- **`backends/kv4p/radio.py`** — `ptt(True)` starts the pacer **after** `_key_on()` (so the
  synchronous lead-in never overlaps it); `transmit()` while keyed `enqueue()`s instead of pushing to
  the encoder inline; `ptt(False)` → `_key_off_streaming()` (stop → flush_tail → drop PTT). The
  one-shot path (`_key_on`/`push`/`_key_off`) is **unchanged** — it never holds the key idle, so it
  never starves. New `_drop_ptt()` factored out of `_key_off` so streaming doesn't double-flush.
- **The single coherent sender is the crux:** during a held key the pacer thread is the *only* thing
  pushing to the encoder / calling `send_tx_audio` (thread-safe via the credit window), so there is no
  encoder race and no doubled frame — exactly one frame per slot.
- **Tests:** `test_kv4p_radio.py` — pacer policy driven by direct `tick()` calls (silence each idle
  slot; sparse audio one-per-slot with a multi-frame gap that decays past Opus prediction; `tx_gain`
  on real audio, gain-invariant silence; sub-frame held until complete; bounded drop-oldest;
  `tick()` swallows `Kv4pTimeout`, stops on `Kv4pClosed`) + lifecycle through `Kv4pHt` (keyed-idle
  emits silence end-to-end; key-down flush + drop-PTT-once; clean restart). Updated the existing
  streaming test (audio now ships on the pacer/flush, not inline). `test_kv4p_transport.py` — 200
  silence slots through the **real** transport + credit window with modeled `WINDOW_UPDATE` refunds
  stay within `[0, window]`, never block, never time out.
- **Docs:** ADR 0082 (cross-refs 0064/0065/0069/0080 + AIOC 0029); ADR index row; this note.

**Verified (no hardware):** `uv run pytest` → **1080 passed, 5 skipped** (+20 new). **No schema
change:** `radio.toml.example` byte-identical, settings-count canary unmoved (no config surface — the
frame interval is a fixed protocol constant).

**Bench acceptance (operator, not run headless):** over Mumble on the kv4p, speak and stop — the tail
no longer repeats; confirm a mid-speech pause is clean and that AIOC behaviour is unchanged.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075); Opus
bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()`
protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI step (ADR 0077).

## Removed the decorative frequency-dial scale from the control panel (ADR 0081) (2026-07-18)

**UI-only cycle, no hardware, no keying.** PR #133 (ADR 0080) is merged (`origin/master` tip
`f51f7f7`); branched fresh (`remove-frequency-dial`), not stacked.

**What & why:** the operator asked to drop the horizontal frequency-dial scale on the control
panel's "face" — the 144–148 MHz ruler + red needle (`DialScale`). It was decoration from the
ADR 0044 retro refresh: `aria-hidden="true"`, hard-coded to the 2 m band, and duplicating the
authoritative `FreqLcd` numeric readout right above it. Pure clutter, no function.

**What shipped:**
- **`web/src/components/ControlPanel.jsx`** — deleted the `DialScale` component and its
  `{showDial && <DialScale state={state} />}` mount. Kept `showDial` (`hasCap("set_frequency")`); it
  still gates the `FreqLcd`, which is unchanged.
- **`web/src/styles.css`** — deleted the `.dial*` block. Left `--tick`/`--ticksoft`/`--red` (used by
  other elements, e.g. the `.decor-dial` gate decoration).
- **Tests/build:** no test referenced the dial, so no test change; `npm test` (10 passing) and
  `npm run build` both green. No Python/server change.

**Non-goals:** no change to the numeric LCD, CAT tuning/scan cards, scanning, or any server-side
behaviour. Decoration removed only.

## The kv4p now has a TX audio-level control, `kv4p.tx_gain` (ADR 0080) (2026-07-18)

**Feature cycle, no hardware, no keying; RX-only-safe, built/tested against fakes.** PR #132 (ADR
0079) is merged (`origin/master` tip `ab797b8`); branched fresh (`kv4p-tx-gain`), not stacked.

**The symptom:** kv4p announcements/voice are **overmodulated**. The firmware TX path applies no
boost and the backend encodes near-full-scale TTS/CW to Opus with no attenuation, so it over-deviates
the SA818. The AIOC tames identical audio with `alsamixer`'s playback slider; the kv4p has no sound
card and no such stage — and no backend had a software TX-level knob.

**What shipped:**
- **`backends/kv4p/audio.py`** — `TxAudioEncoder` gains a `tx_gain` param and a pure
  `_apply_tx_gain(samples, gain)` helper applied in `push()` on the int16 samples **before** the Opus
  encoder (the one choke point every TX byte flows through). `gain == 1.0` is an exact int16 no-op;
  otherwise it multiplies and **clamps to ±32767** (so `>1.0` clamps, never wraps).
- **`backends/kv4p/radio.py`** — `DEFAULT_TX_GAIN = 1.0` (verify-on-bench, guardrail 1); a `tx_gain`
  constructor kwarg carried into the encoder built in `_key_on()`, so streaming and one-shot TX both
  inherit it.
- **`config/spec.py`** — `kv4p.tx_gain` (`coerce_positive_float`, matching `sample_rate_correction`;
  advanced tier). **`api/backend_config.py`** and **`doctor.py`** thread it through; both the initial
  build and the ADR 0076 live rebuild go via `build_radio → backend_kwargs`, so a switch honours it.
- **Tests:** `_apply_tx_gain` (0.5 halves, 1.0 exact no-op, 2.0 clamps not wraps); the encoder scales
  the pre-encode accumulator (sub-frame push, no libopus); the setting reaches the live encoder at
  key-up + defaults to unity; a one-shot transmit is attenuated **end-to-end** (decode the emitted
  Opus, energy halves); resolve/coerce + wiring. Canary 63 → 64; `radio.toml.example` regenerated.
- **Docs:** ADR 0080 (cross-refs 0076/0065/0070; notes AIOC needs no equivalent — OS mixer owns its
  TX level); ADR index row; configuration.md KV4P bullet ("overmodulated? lower it, ~0.5 start");
  this note.

**Non-goals:** no AIOC change, no limiter/AGC (plain gain), no RX change, no new backends.

**Note for the bench cycle:** the default is 1.0 (no change). The right level is a per-radio
deviation fact — set `kv4p.tx_gain` empirically until modulation is clean; ~0.5 is the documented
starting point.

## The over-RF auth session now persists across a backend switch (ADR 0079) (2026-07-18)

**Bug-fix cycle, no hardware, no keying; reproduced against fakes.** PR #131 (ADR 0078) is merged
(`origin/master` tip `9ef628e`); branched fresh (`persist-auth-session-across-switch`), not stacked.

**The bug:** a live backend switch (`POST /radio/select`, ADR 0076) logged out an authenticated
operator. `build_controller` minted a **fresh `Session()`** on every call
(`controller/engine.py`), and `holder.rebuild` → `controller_factory` → `build_controller` rebuilds
the controller on each switch — so a fresh, unauthenticated session replaced the live one. The auth
session belongs to the operator at the station, not the per-radio controller; it must outlive the
rebuild. (Same class as ADR 0078 — "a switch must preserve everything a fresh boot would set up" —
but for runtime session state, not config.)

**What shipped:**
- **`controller/engine.py`** — `build_controller` gains `session: Session | None = None`: use the
  passed one, else mint a fresh `Session()` (back-compat — direct callers/tests unchanged).
- **`api/app.py`** (`build_app`) — construct **one** `Session` and capture it in the
  `controller_factory` closure (alongside the stable service-bindings/mumble/plugins deps); pass it
  into every `build_controller` call. The same object flows into the initial build and every rebuild,
  so a switch injects the live session and its state + `last_activity` survive. `AuthGate` is still
  rebuilt fresh (stateless re: the session; re-wires to the new dispatcher/station ID).
- **Tests (`tests/test_backend_select.py`):** session survives a rebuild (same object, still
  authenticated, controller genuinely rebuilt); back-compat mints a fresh one; end-to-end
  (`POST /auth/session` → `POST /radio/select` → `GET /status` still `session_open: True`); the
  inactivity clock carries (near-timeout stays near-timeout, expires on schedule, not reset/extended);
  a mid-entry DTMF accumulation does not survive (per-controller framer). Verified the 4 behavioural
  tests FAIL without the engine fix and PASS with it.
- **Docs:** ADR 0079 (cross-refs 0076/0078); ADR index row; this note. No operator doc claimed the RF
  session lifecycle across a switch, so no correction was needed.

**Behaviour confirmed, not weakened:** Part 97 (guardrail 5) — the rebuilt `StationId` starts with
`_last_id=None`, so the **first** over on the new radio always carries the ID (errs toward ID-ing,
legal); the periodic-ID net doesn't fire until the new radio transmits. No `StationId` change.
Rationale for persisting across a LOCAL switch: auth over RF is "gated, not secure" (guardrail 4) —
same operator, same station, their own radio.

**Verified:** `uv run pytest` (full suite green: 1060 passed, +5 new tests). No schema change —
`radio.toml.example` byte-identical, settings-count canary unmoved at 63.

**No bench acceptance needed** — fully reproduced against a fake backend + wired controller. Operator
confirmation optional: authenticate over the air, switch AIOC↔kv4p in the browser, stay logged in.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075 noted
it); Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the
`Radio.close()` protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI
step (ADR 0077).

## A live backend switch dropped every local-plugin service — extra-channel loss (ADR 0078) (2026-07-18)

**Bug-fix cycle, no hardware, no keying; reproduced against fakes.** PR #130 (ADR 0077) is merged
(`origin/master` tip `507366c`); branched fresh (`fix-extra-channel-on-switch`), not stacked.

**The bug (confirmed on hardware + in code):** `POST /radio/select` dropped every local-plugin
(`[plugins.*]`) service from the live catalog until a restart. On the operator's box, switching
AIOC↔kv4p made weather/astronomy/quote/battery/bible vanish from the UI; a restart brought them back.

**Root cause:** the select handler rebuilt settings from **schema keys only** and called
`resolve_settings({**base, "server.backend": target})` with **no `extra=`** — so `new_settings` had an
empty extra channel (ADR 0051). `holder.rebuild` → `controller_factory(new_settings, …)` →
`build_controller(new_settings, …)` then gated every local plugin off (`enabled()` reads
`settings.extra("<name>.base_url")` → `""`), shrinking `controller.service_catalog`; `app.state.settings
= new_settings` propagated the stripped settings app-wide. Runtime-only — `save_settings` leaves the
on-disk `[plugins.*]` untouched, so a restart restores them, but every switch re-strips.

**Audit:** only the extra channel rides on `Settings`; `load_service_bindings`/`load_mumble_servers`/
`configured_backends`/`validate_configured_backends`/`backend_kwargs` are switch-safe (disk- or
schema-only). But the **identical idiom in `PATCH /settings`** had the same defect — any save stripped
the live plugins channel too. Both fixed.

**What shipped:**
- **`config/settings.py`** — new public `Settings.extras() -> dict` (the whole extra channel as a copy;
  `Settings` stays immutable). Was only reachable via private `_extra` / the per-key `extra(key)` getter.
- **`api/app.py`** (`POST /radio/select`) and **`api/settings.py`** (`PATCH /settings`) — both now pass
  `extra=current.extras()` through `resolve_settings`. `holder.rebuild`→`build_controller` already flow
  `new_settings` end-to-end, so the restored channel reaches the plugin gate and the catalog is whole.
- **Tests:** `test_config.py` (the `extras()` accessor + patch-idiom round-trip); `test_backend_select.py`
  (switch preserves the extra channel on `app.state.settings`; end-to-end — a controller wired to an
  extra-gated local plugin keeps its `GET /services` entry across a switch **and the switch back**);
  `test_settings_api.py` (PATCH preserves the channel). Verified the 3 endpoint tests FAIL without the
  fix and PASS with it.
- **Docs:** ADR 0078 (cross-refs 0076/0051); ADR index row; this note. `using-it.md`'s switch section
  never overclaimed service preservation, so no correction needed (the vanishing was a bug, not
  documented behavior).

**Verified:** `uv run pytest` (full suite green; +6 new tests). No schema change — `radio.toml.example`
byte-identical, settings-count canary unmoved at 63.

**No bench acceptance needed** — the failure and fix are fully reproduced against a fake backend +
MockRadio + a wired controller. Operator confirmation optional: switch AIOC↔kv4p in the browser and see
the local services stay in the panel without a restart.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075 noted it);
Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()`
protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI step (ADR 0077).

## The backend selector in the web UI (ADR 0077) (2026-07-18)

**UI cycle, no server change, no keying.** PR #129 (ADR 0076) is merged (`origin/master` tip `cb51a4c`);
branched fresh (`backend-selector-ui`), not stacked. This is the web control panel consuming the ADR 0076
switch endpoints — the last user-facing piece of switching radios in the app.

**What shipped:**
- **Reactive capabilities (the crux)** — `web/src/useEvents.js`: `reduceStatus` gains
  `case "capabilities" → {...prev, caps: data.capabilities}` (and is now `export`ed for unit tests), so
  the ADR 0076 re-emit becomes reactive `state.caps` instead of being silently dropped.
  `web/src/components/ControlPanel.jsx`: `advertised = new Set(state.caps ?? caps)` — prefers the reactive
  set over the one-shot login prop, so the CAT tuning/scan cards mount/unmount live on a switch **without a
  reconnect**; the additive `disabledCaps` (501 greying) clears on `[state.caps]` so the new radio isn't
  greyed by the old radio's 501.
- **The selector** — new `web/src/components/BackendPanel.jsx`, a `.card` in the left column built from the
  `ModeControl` `<select>`/`useAction`/`.error` idiom. Fetches `GET /radio/backends` on mount, **self-hides
  when <2 backends are configured**. Tracks the **live** active backend (`state.backend`), so a 503 (switch
  failed, server already rolled back) snaps the dropdown back and the error names the radio you're still on;
  `pending` → "Switching…"/disabled; a caption warns switching drops PTT while transmitting.
- **`web/src/api.js`** — `backends()` + `selectBackend(backend)` beside the Mumble-link methods (no new
  error mapping needed). **`web/vite.config.js`** — dev proxy gains `/radio` (the ADR 0076 endpoints
  predate any UI caller).
- **Bootstrapped Vitest** — the frontend had **no JS test runner** (browser-verified, no CI). Added
  vitest + @testing-library/react + jsdom, a `test` block in `vite.config.js`, `src/test-setup.js`, and an
  `npm test` script. Three suites (10 tests): `BackendPanel.test.jsx` (renders list w/ active marked,
  selects POSTs the backend, in-flight disabled + "Switching…", 503 snaps back, mid-TX warning),
  `ControlPanel.test.jsx` (caps re-emit re-greys the CAT cards both directions), `useEvents.test.js`
  (`reduceStatus` capabilities fold).

**Verified:** `cd web && npm test` → **10 passed**; `npm run build` builds `web/dist/`; `uv run pytest` →
**1050 passed, 5 skipped** (no server change). `web/dist` is gitignored, so the rebuild isn't committed.

**Bench acceptance (operator, two-radio box — not run headless):** in the browser, pick the other radio;
confirm the tuning/scan controls appear for the kv4p and vanish for the AIOC **without a reconnect**, the
face label follows, a forced-failure leaves the selector on the previous radio, and the selection survives a
restart (ADR 0076 persists `server.backend`). Both backend blocks must be present in `radio.toml`.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (one box now runs two
radios — ADR 0075 noted it); Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner
gate; the `Radio.close()` protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals). A JS-test CI
step could now run `npm test` where before there was nothing to run.

## The live backend switch (ADR 0076) (2026-07-18)

**Endpoint + API cycle, no keying; tested against fakes.** PR #128 (ADR 0075) is merged (`origin/master`
tip `3790851`); branched fresh (`radio-backend-select-live`), not stacked. This wires the ADR 0073
holder seam + ADR 0074 `configured_backends()` into a live switch. **No UI** — the dropdown is next.

**What shipped:**
- **`api/holder.py`** — `RadioHolder.rebuild(new_settings)`: atomic under a new `asyncio.Lock`; runs
  `stop() → radio_factory(new_settings) → start()`. `start()` rebuilds the controller via a
  `controller_factory` (because `stop()` reaps it and it captures the radio). **Rollback** is the
  load-bearing case: if the target fails to construct/open, it reconstructs+restarts the *previous*
  backend and re-raises (the old radio was closed by `stop()`, so restore rebuilds fresh). Two injected
  factories added to `__init__` — `radio_factory` (default `build_radio`; fakes injectable) and
  `controller_factory` (default `None`) — both defaulted so the DI seam is unchanged.
- **`api/events.py`** — new `"capabilities"` event type + `capabilities_event(radio)` helper
  (`data.capabilities` = sorted cap strings, mirroring `GET /capabilities`).
- **`api/app.py`** — inside `create_app`: `POST /radio/select {backend}` (409 if not in
  `configured_backends`; `resolve_settings` patch; `holder.rebuild`; 503 + previous backend on failure;
  then `save_settings` write-back, `nonlocal`-rebind of `radio`/`rx_pump`/`scan_runner`/`controller` +
  the matching `app.state.*`, `if rx_demand>0: rx_pump.start()`, and re-emit `capabilities`+`status`)
  and `GET /radio/backends` (`{active, active_capabilities, backends:[{name,active,settings}]}`).
  `create_app` gained `controller_factory`/`radio_factory` kwargs; `build_app` builds the controller
  through the factory (same totp/secret gate) and forwards it.
- **The `nonlocal` rebind is the ADR 0073-deferred "routes read holder.radio live" step** — every
  late-binding closure (`_require_cat`, `get_capabilities`, `_acquire_rx`/`_release_rx`, the scan
  routes, the Mumble bridge's `rx_active`) then follows the new radio with no per-handler edits.
- **Tests** — `test_radio_holder.py` +4 (swap, rollback, lock-serializes, controller-rebuild, all
  against fakes keyed on `server.backend`); new `test_backend_select.py` +6 (select 200 + caps change,
  409 unconfigured untouched, 503 rollback + config unwritten, `capabilities` re-emit over `/events`,
  persistence round-trip preserving the rest of the file).

**Persistence decision (recorded in ADR):** a live switch **writes `server.backend` back** through the
schema on success only, so a restart lands on the last-selected radio; the rest of `radio.toml` is
preserved (tomlkit round-trip).

**Verified:** `uv run pytest` → **1050 passed, 5 skipped** (1040 prior + 10 new). **No schema change:**
`radio.toml.example` byte-identical (golden green, no regen), settings-count canary unmoved.

**Bench acceptance (operator, two-radio box — not run headless):** `POST /radio/select` to flip
AIOC→kv4p and back; confirm RX audio follows the newly-selected radio and the `capabilities` payload
changes; record switch latency both directions (kv4p reboots on open — a beat is expected, not a
failure). RX-only, no dummy load needed for the select itself.

**Next (the UI cycle):** the backend dropdown consuming `GET /radio/backends` + `POST /radio/select`;
frontend consumption of the `capabilities` event (a `reduceStatus` case + lift `caps` out of the
one-shot `session.caps` prop so controls re-grey live). Open items unchanged: kv4p DTMF bench
acceptance; per-backend DTMF twist (now that one box runs two radios — ADR 0075 noted it); Opus bitrate
cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()` protocol
promotion / `ControllerRunner` removal (ADR 0073 deferrals).

## Configurable DTMF reverse-twist tolerance (ADR 0075) (2026-07-18)

**Decoder + config cycle, no hardware, no keying.** PR #127 (ADR 0074) is merged (`origin/master` tip
`6e4e1e9`); branched fresh (`dtmf-reverse-twist-config`), not stacked.

**Why (bench-confirmed on real hardware):** same AIOC backend + same native decoder, a UV-5R decodes
DTMF fine but a **UV-5R Mini decodes nothing**. Replaying both captures through the real
`GoertzelStream`: the Mini's tones are on-frequency and above the energy floor, but its **low group
runs ~6.4 dB hotter than the high** (median reverse twist −6.4 dB), tripping the hardcoded −4 dB
`NATIVE_REVERSE_TWIST_DB` gate on 172/176 blocks; the UV-5R sits at −0.1 dB. The real `mini.wav`
decodes at a 10 dB limit, garbles at 8, nothing at 4 (UV-5R still fine at 10). Talk-off holds at 10 dB
— dominance + second-harmonic gates carry it, not twist — so widening reverse twist is talk-off-safe.

**What shipped (opt-in, default unchanged):**
- **`audio/dtmf.py`** — `GoertzelStream.__init__(reverse_twist_db=NATIVE_REVERSE_TWIST_DB)` computes
  `self._reverse_twist = 10.0 ** (reverse_twist_db / 10.0)` (power ratio — Goertzel `power` is
  magnitude-squared, so dB/10 not dB/20). `NATIVE_REVERSE_TWIST_DB` stays 4.0 as the fallback constant.
  New loader `load_dtmf_reverse_twist_db(settings)` beside the other DTMF loaders (re-exported from
  `audio/__init__.py`).
- **`config/spec.py`** — new `audio.dtmf_reverse_twist_db` (`RADIO_DTMF_REVERSE_TWIST_DB`,
  `coerce_positive_float`, default = imported `NATIVE_REVERSE_TWIST_DB`), in the `audio` group and
  `_ADVANCED_KEYS`. Settings-count canary 62 → **63**; `radio.toml.example` regenerated.
- **`controller/engine.py`** — native decode path builds
  `GoertzelStream(reverse_twist_db=load_dtmf_reverse_twist_db(settings))`.
- **`doctor.py`** — listen path threads the loaded value too (defaults to 4.0 if config read fails, in
  the existing `try/except`), so the diagnostic honors the override.
- **Tests (`tests/test_native_dtmf.py`)** — synthesized −6.4 dB Mini-profile tone **fails at 4.0,
  decodes at 10.0** (`1234#`); default-equals-constant preservation check; talk-off holds at the wide
  10.0 gate (12 white-noise seeds, a chirp sweep, off-grid/same-group tone pairs). The rest of the DTMF
  suite is unchanged — that's the proof the 4.0 default preserves every existing decode.
- **Docs** — ADR 0075; `configuration.md` + `troubleshooting.md` ("DTMF works on one radio but not
  another") entries; ADR index row.

**Deliberate scope:** reverse twist only (no forward-twist problem seen; could mirror this later);
**global**, not per-backend (revisit once the backend-switch arc lets one box run two radios). The
default stays 4.0 — the Mini is non-spec; compliant radios keep the tighter, talk-off-safe gate.

**Verify:** `uv run pytest` (full suite green), or focused
`uv run pytest tests/test_native_dtmf.py tests/test_config.py tests/test_settings_api.py -q`.

## radio.toml describes more than one backend (ADR 0074) (2026-07-18)

**Config-model cycle, no hardware, no keying.** PR #126 is merged (`origin/master` tip `0155068`);
branched fresh (`multi-backend-config`), not stacked. Builds on the ADR 0073 holder seam. **The user
chose the presence-based model** (over an explicit `server.backends` list) — lighter, no schema change.

**Why:** the holder can now be stopped/rebuilt (ADR 0073), but the config is still single-backend —
`server.backend` names one and the other `[<backend>]` block is inert. Per-key coercion already runs
for every block, but the *cross-field* validation (the two `audio.squelch=cat` guards; the kv4p
frequency band check) only ran for the *active* backend. So a config could carry a broken *other*
block that nothing notices until someone selects it live (the ADR 0051 "latent config surfaces on a
restart" lesson). This cycle moves that failure to load time. **No switching yet** — that's next.

**The model (presence-based):** a backend is *configured* if its `[<backend>]` block is present in
`radio.toml` (any `baofeng.*`/`kv4p.*` key), plus the active `server.backend` (always configured — it
boots from defaults). `server.backend` is the *initial* selection, not the only permitted one. A
single-block config is unchanged: only `[baofeng]` → only baofeng validated/enumerated.

**What shipped:**
- **`config/settings.py`** — presence captured during resolution (it can't be recovered later: every
  backend key has a default). `Settings.configured_backend_names() -> frozenset[str]`; derived
  `BACKEND_BLOCK_GROUPS = {spec groups} ∩ available_backends()` = `{baofeng, kv4p}`.
- **`api/backend_config.py`** (new, light — no pipeline imports, so `doctor` imports it cheaply):
  `backend_kwargs` (the settings→ctor mapping extracted verbatim from `build_radio`'s switch);
  `validate_backend_config(settings, backend, *, include_construction_checks)` — pure, no construction
  (constructing a hw backend opens serial / v71 raises); `validate_configured_backends` (validates
  every configured backend **except the active one**, which stays validated as before);
  `configured_backends() -> tuple[BackendChoice, ...]` enumeration (active first, each with resolved
  kwargs) for the next cycle's select endpoint + UI — **no caller yet**, shape defined per the task.
- **`api/holder.py`** — `build_radio` reuses the extracted helpers; behaviour byte-identical, and it
  still looks `create_radio` up locally so `test_backend_wiring`'s monkeypatch target is unchanged.
- **`api/app.py`** — `validate_configured_backends(settings)` right before `build_radio`.
- **`doctor.py`** — `_validate_doctor_backend_config` validates the selected backend loudly against
  the real `radio.toml` (`include_construction_checks=True`, so an out-of-band `kv4p.frequency`
  surfaces even with no hardware). Validation stays **out of** `resolve_settings`/`load_settings` on
  purpose: doctor wraps every settings read in `try/except`, so a raising loader would be swallowed and
  regress the ADR 0069 "read the real file" fix.
- **`backends/kv4p/radio.py`** — pure `default_freq_range_hz(band)` for the load-time band check.

**The validation split (the behaviour-preservation key):** the active backend is validated exactly as
before (squelch guard in `build_radio`; frequency at construction, HELLO-aware). Only the *inactive*
present blocks get the added pure checks (`include_construction_checks=True`) — they are never
constructed. This is why `test_kv4p_backend_passes_every_setting_through` (uhf + a VHF `146520000`)
stays green: the active backend skips the load-time band check.

**Deliberately stricter (called out in the PR):** a config that names both blocks AND sets
`audio.squelch=cat` while the *inactive* block is baofeng now fails at load (baofeng+cat is invalid) —
where before the stray block was ignored. Presence-scoped, so single-backend configs are unaffected.

**Verified:** `uv run pytest` → **1028 passed, 5 skipped** (1015 prior + 13 new `test_multi_backend.py`:
presence, invalid-inactive-fails-loud for both squelch and frequency, both-blocks-valid builds,
single-block back-compat, the active/construction validation split, the enumeration surface, and two
doctor validation tests). **No schema change:** `radio.toml.example` byte-identical (golden test green,
no regen), settings-count canary unmoved at 62. Behaviour of the active backend byte-identical
(`test_backend_wiring` green).

**Next (the swap cycle):** `RadioHolder.rebuild(new_settings)` + a `POST` select endpoint (consuming
`configured_backends()`) + the UI dropdown; make the routes read `holder.radio` live. Then per-backend
live capabilities (require construction — a note in ADR 0074). Other open items unchanged: kv4p DTMF
bench acceptance, Opus bitrate cap (ADR 0069), installer kv4p path, conditional Mumble-banner gate.

## A radio-holder seam for a swappable active radio (ADR 0073) (2026-07-18)

**Pure behaviour-preserving refactor, no hardware, no keying.** PR #125 is merged (`origin/master` tip
`dafb80c`); branched fresh (`radio-holder-seam`), not stacked. **This cycle's PR: #126.** (The authoring
machine crashed after the commit was pushed but before the PR was opened, corrupting the *local* git
object store; the commit itself was safe on `origin`. The local repo was repaired from the remote —
re-fetch of the pushed objects, `git fsck` clean — the suite re-run green (1015/5), and #126 opened.
No content change from the pushed commit.)

**Why:** the app was single-radio to the bone — `build_app` built one `radio` and threaded it into `RxPump`,
`TxSession`, the DTMF controller, `ScanEngine`, and the ID paths, with teardown scattered across the lifespan.
Live backend switching is impossible until one object owns the radio + its pipeline and can stop/restart them.
This cycle adds that seam and nothing else. "Make the change easy, then make the easy change."

**What shipped (new `radio_server/api/holder.py`):**
- **`build_radio(settings)`** — the `server.backend` switch + the two squelch fail-loud guards, extracted
  verbatim from `build_app` (which now just calls it). Lives at the composition root, not `backends/factory.py`
  (backends stay Settings-free). The one config→radio path the swap cycle reuses.
- **`class RadioHolder`** — owns the active radio (`.radio`) and the radio-bound pipeline lifecycle.
  `start()` (sync, idempotent) constructs `RxPump` + `ScanRunner` against the radio (with the two hub-publish
  adapters now owned by the holder); it starts **no** task (pump is demand-started, scan is plan-started).
  `stop()` (async, idempotent, fail-safe) tears down in the proven order: drop PTT **if `arbiter.transmitting`**
  → `scan_runner.stop()` → `rx_pump.stop()` → `controller.close()` (guarded) → `radio.close()` (getattr-guarded;
  V71 has none).
- **`create_app`** builds the holder, calls `holder.start()`, and **rebinds its locals** `rx_pump =
  holder.rx_pump` / `scan_runner = holder.scan_runner` (the demand-counter, the Mumble bridge's `rx_active`,
  and the scan routes close over those locals). `app.state.{holder,radio,rx_pump,scan_runner}` all point at the
  holder's instances. The lifespan's radio-bound teardown block is now `await app_.state.holder.stop()`, with
  `link.disconnect()` kept before it and recorder/event-log teardown after.

**Findings recorded in ADR 0073 (surfaced, not papered over):** (a) no single app-level "is keyed" flag — PTT
drop is fragmented across per-connection `TxSession`, the direct `POST /ptt` path, and the Mumble bridge, so
`stop()` keys down **conditionally on `arbiter.transmitting`** (an unconditional drop was tried and rejected:
it changed observable teardown keying and broke 5 keying-contract tests; the conditional form preserves
behaviour and still guarantees an arbiter-holding session can't latch TX across a swap); (b) `Radio` protocol
has no `close()` (V71 gap) → `getattr` guard; (c) the station-ID "scheduler" isn't a stoppable object (ID is
clock-driven inline); (d) the controller has no self-owned task (`ControllerRunner` vestigial).

**Verified:** `uv run pytest` → **1015 passed, 5 skipped** (prior 1008 + 7 new `test_radio_holder.py`). Behaviour
identical: `create_app`'s signature, the `app.state.*` names tests read, and every route's `radio` binding are
unchanged. `test_backend_wiring` now patches `create_radio` where `build_radio` looks it up (switch moved
modules; asserted wiring identical).

**Next (the swap cycle, explicitly deferred here):** `RadioHolder.rebuild(new_settings)` + a select endpoint +
multi-backend config + UI; make the routes read `holder.radio` live (so a swapped radio propagates); optionally
protocol-ize `close()` and remove the vestigial `ControllerRunner`. Other open items unchanged: kv4p DTMF bench
acceptance (`doctor --backend kv4p --dtmf`, operator RX-only); per-block DTMF normalization; Opus bitrate cap
(ADR 0069); installer kv4p path; conditional Mumble-banner gate (ADR 0067).

## kv4p DTMF found & fixed — the decoder's energy floor was ~10× too high for received audio (ADR 0072) (2026-07-18)

**RX-only cycle, no keying.** PR #124 is merged (`origin/master` tip `118ee17`); branched fresh
(`kv4p-dtmf-energy-floor`), not stacked.

**Why:** DTMF still didn't decode on kv4p after 0070 (sample rate) and 0071 (capture). Task: *stop
analysing, reproduce in a test, fix from what it shows.* The frame-size lead (kv4p 1882/1920 vs AIOC 960
against the 205-sample block grid) was **wrong**.

**What the reproduction showed** (feeding the operator's real `cap.wav` — the 0071 `--rx-capture` output
— through the live decode path):
- The analyzer reads `1234#` fine; the live `StreamingDtmfInput(GoertzelStream())` reads `''`.
- Not frame size: clean synth decodes at 960/1920/1882/705/441; the real capture decodes `''` at every
  size *and* as one whole-stream write (so not the per-frame `to_multimon` seams either).
- **Every block fails the energy floor.** 468/468 blocks below `NATIVE_ENERGY_FLOOR` (0.02); zero reach
  the dominance/twist/harmonic gates. Strongest tone block: low ≈937 Hz power **0.0123** (below floor),
  high ≈1483 Hz **0.0281**. Scaling the audio ×2 makes it decode `1234#` cleanly → **purely level**.

**Root cause:** `NATIVE_ENERGY_FLOOR = 0.02` is an *absolute* threshold tuned to the 0.4-amplitude synth
fixtures (power ~0.039). Real received DTMF lands ~10× quieter (~0.012). The same UV-5R decodes into
AIOC only because that cable is hotter. This is the level analogue of 0070's exact-frequency blind spot,
and closes the ADR 0060 / this-file "RX level vs `NATIVE_ENERGY_FLOOR`" item.

**What shipped (one constant + regressions, all hardware-free):**
- **`NATIVE_ENERGY_FLOOR` 0.02 → 0.002** (`radio_server/audio/dtmf.py`), with a marked comment recording
  the measured basis. Talk-off is preserved by the **scale-invariant ratio gates** (dominance 4×, twist,
  2nd-harmonic 4×), not the floor — full-scale white noise stays clean to 0.001 (12 seeds), so 0.002
  keeps a 2× guard. No kv4p-side gain: the decoder is the fix.
- Regressions in `tests/test_native_dtmf.py`: **received-level decode** (quiet clean `1234#` decodes;
  a monkeypatched-0.02 twin proves the old floor ate it), **frame-size invariance** (960/1920/1882/441/
  705 all decode), **12-seed talk-off** guard.
- **Realigned the 0070 offset regression** to a received level (`_RECEIVED_AMPLITUDE = 0.15`): at the
  loud 0.4 fixture level the scalloped off-bin tone clears the *new* floor for digits 1/4, so "offset
  never decodes" only holds at a realistic (quiet) level — where both fixes are genuinely load-bearing.

**Verified:** `uv run pytest` → **1008 passed, 5 skipped**.

**Bench acceptance (operator, RX only — the arbiter):** `doctor --backend kv4p --dtmf`, key `1234#` from
a handheld → digits decode. The offline reproduction over the real capture predicts this passes. This is
the last item before a working node. (`cap.wav` was a local artifact, not committed; its numbers live in
ADR 0072.)

**Deferred (not started without a new task):** per-block normalization / relative floor (fully
level-invariant detection; needs voice-corpus talk-off validation); Opus bitrate cap (ADR 0069);
installer kv4p path; conditional Mumble-banner gate (ADR 0067).

## kv4p DTMF still fails after 0070 — capture the RX audio and read the tones (ADR 0071) (2026-07-18)

**RX-only cycle, no keying.** PR #123 is merged (`origin/master` tip `953ce00`); branched fresh
(`kv4p-rx-capture`), not stacked.

**Why:** DTMF still doesn't decode on kv4p after the ADR-0070 sample-rate fix. Bench state (operator):
true ADC rate measured 48,759 Hz → `kv4p.sample_rate_correction = 1.0158`; signal strong (loudest block
17312); the same UV-5R that always decodes into the AIOC decodes nothing into kv4p. Analysis has been
wrong three times, so this cycle **stops analysing and instruments a direct capture**: read the DTMF
tones out of the actual received audio with an FFT, independent of GoertzelStream.

**What shipped (all hardware-free, tested against fakes):**
- **`doctor --backend kv4p --rx-capture`** — records N s of `receive()` (the corrected 48 kHz the
  decoder sees) to a WAV (`--out`, default `kv4p-rx-capture.wav`) while the operator keys `1234#`, then
  analyses it. `--analyze-wav PATH` re-runs the analysis on a saved WAV (no radio). Never keys.
- **`analyze_dtmf_windows` / `format_dtmf_analysis`** — per ~100 ms window: Hann-FFT, strongest low/high
  band peaks with parabolic sub-bin interpolation, snapped to the DTMF grid → digit, plus clip fraction
  and loudest peaks. Ends in a **verdict** (clipping checked first, since a clipped dual-tone still
  shows its fundamentals): (1) **CLIPPING** → the firmware's **16× RX gain** (`rxAudio.h` `Boost(16.0)`)
  saturates a strong dual-tone, breeding harmonics/intermod that trip the decoder's gates → upstream;
  (2) **off-frequency** → correction still wrong; (3) **on-frequency & clean** → decode-path wiring;
  (0) **absent** → mangled upstream (firmware filter / SA818 / RF).
- **`--rx-level` verdict tightened** to **0.2 %** (`_RATE_MATCH_TOL`) — the old 0.5 % gate wrongly
  called the bench's 1.0158-vs-1.02 (0.4 %) "dialed in"; now it flags the gap and prints the value to set.
- `MockRadio` gained a no-op `close()` (faithful double for the open-then-close diagnostics).

**Firmware RX chain read (`3f0e809` `rxAudio.h`, the leading suspect):** order is
`dcOffsetRemover → gain → afskTapEffect → mute` before Opus. `Boost gain(16.0)` = a 16× stage (the
clipping suspect); `DCOffsetRemover` is a one-pole HPF well below 697 Hz (harmless); the AFSK tap
returns its input unmodified (passive); `mute` is a squelch-gated `Boost(0.0)`. The 16× gain is the
concrete, testable hypothesis — hence the analyzer surfaces the clip fraction.

**Verified:** `uv run pytest` → **1000 passed, 5 skipped** (+ analyzer verdict tests for clean/clipping/
off-frequency/silence, WAV round-trip + bad-format reject, `--rx-capture` writes+analyses / no-audio
fail, and the tightened rate verdict).

**Bench acceptance still open (operator, RX only — the arbiter):** run
`doctor --backend kv4p --rx-capture --seconds 12 --out cap.wav` while keying `1234#`; paste the verdict
and the per-window dominant frequencies into the PR. Then apply the fix the verdict names (attenuate if
clipping; re-trim if off-frequency; decode path if clean). `--dtmf` decoding `1234#` is still done.

**Follow-up (not this cycle):** whatever the WAV names — an RX attenuation stage (if clipping), a
correction re-trim (if off-frequency), or a decode-path fix (if clean).

## kv4p RX sample-rate correction — the firmware `*1.02` offset that broke DTMF (ADR 0070) (2026-07-18)

**RX-only cycle, no keying.** PR #122 is merged (`origin/master` tip `72b0b00`); branched fresh
(`kv4p-rx-sample-rate`), not stacked.

**Root cause (verified in the pinned firmware `3f0e809`, guardrail 1):** `rxAudio.h` sets
`config.sample_rate = AUDIO_SAMPLE_RATE * 1.02` — the RX ADC runs ~2 % fast (≈48960 Hz) — while the
Opus encoder is told the unmultiplied 48000. So received audio arrives ~2 % off and mislabelled
48 kHz. That knocks every DTMF tone off its Goertzel bin (spacing ~39 Hz; 1633 Hz moves ~33 Hz), which
is why the codec, level, and clipping all tested clean while DTMF never decoded — every DTMF test used
*exact* frequencies. `globals.h` `SAMPLING_RATE_OFFSET 0` and `txAudio.h` confirm **TX is clean; the
offset is RX-only.** Wider than DTMF: it's a ~1.2 s/min clock drift for the hub, recorder, and Mumble
link too. (Irony: PR #118 deleted the kv4p soxr resamplers — true of the codec, false of the ADC.)

**The fix:**
- `RxAudioDecoder` (backends/kv4p/audio.py) resamples the true device rate → 48000 with a stateful
  **soxr `ResampleStream`, HQ** (the ADR-0054 `GoertzelStream` precedent, not the VHQ latency trap).
  `sample_rate_correction=1.0` is a byte-for-byte pass-through, so the generic decoder is unchanged.
- Config knob **`kv4p.sample_rate_correction`** (default **1.02**, marked verify-on-bench), threaded
  through `api/app.py` and `doctor._build_backend`; `radio.toml.example` regenerated (golden test green).
- doctor **`--rx-level` prints the measured true rate** = `fps × 1920` (invariant to the correction —
  the device emits one 1920-sample packet per 1920 ADC samples) and the implied correction; advise
  `--seconds 30` so USB jitter averages out.
- Also: **`DEFAULT_CONNECT_TIMEOUT` 2.0 → 10.0** (transport.py) — the reset-on-open board races its
  ~1 s boot and 2 s intermittently lost the elicit (ADR 0069's deferred first-connect item).

**Verified (no hardware for the suite):** `uv run pytest` → **989 passed, 5 skipped** (+ new tests: the
DTMF offset regression `1234#` fails-then-decodes, `RxAudioDecoder` pass-through / corrected-length /
empty-chunk, the correction reaches the decoder, `kv4p.sample_rate_correction` parse+reject, and
`_format_kv4p_rx_rate`). The rig was tested against fakes; no keying, no hardware.

**Bench-verify still open (operator, RX only — the last item before a working node):** run
`doctor --backend kv4p --rx-level --seconds 30` on 445.800 to read the measured rate and trim
`kv4p.sample_rate_correction`; then `doctor --backend kv4p --dtmf` and key `1234#` from a handheld — the
digits should decode. Record both in the PR.

**Follow-ups (unchanged):** Opus bitrate cap (ADR 0069); installer kv4p path + conditional
Mumble-banner gate (ADR 0067).

## kv4p TX bring-up — telemetry rig + first bench keying (ADR 0069) (2026-07-18)

The transmit side keyed hardware for the first time (dummy load, **445.800 MHz**, UHF, second
receiver). PR #121 is merged (`origin/master` tip `024021a`); branched fresh (`kv4p-tx-bringup`), not
stacked. Shape: **instrument (no keying, tested against fakes) → operator keys live → fold the real
numbers in** — the operator did the keying; the RF guards (non-interactive refusal, typed CONFIRM,
2 s/5 s caps) were kept verbatim.

**The rig (Phase 1, no hardware):**
- `transport.TxStats` — per-keying counters under the credit lock: encoded Opus bytes/frame
  (`send_tx_audio`), on-wire escaped bytes, `blocked_frames`, `min_credits` (`_write_frame`). Exposed
  as `Kv4pTransport.tx_stats`/`window_size`; `reset_tx_stats()` at each `_key_on`. Surfaced on `Kv4pHt`.
- doctor: key-up latency in `_kv4p_keying_core`; pure `_format_tx_stats()` printed by `--tx-tone`;
  kv4p-specific no-tone hint (dropped the stale AIOC "alsamixer" text); `--tx-lead SECONDS` sweep knob.
- New runbook `docs/kv4p-tx-bringup.md` (linked from `kv4p-setup.md` + `hardware-bringup.md`).

**Two bugs the bench surfaced (fixed this cycle):**
1. **Doctor never read `radio.toml`.** Every `load_settings()` in `doctor.py` passed no path →
   pure defaults. So a keying test used the **default** serial port/band — and on this bench
   `/dev/ttyUSB0` is a *DV Dongle*, the kv4p is on `ttyUSB1`, and `kv4p.frequency` has **no CLI flag**,
   so 445.800 was unreachable. Fix: `_doctor_settings()` reads `DEFAULT_CONFIG_PATH` (all backends).
2. **Keying modes gave no next step on a connect failure.** The first `--key-test` lost the elicit
   handshake (first-connect/reset-on-open race, ADR 0066) and printed only the raw error. Fix: point
   at the non-keying connect probe (`_print_kv4p_open_hint`).

**Bench numbers (2026-07-18, guardrail-1 facts now measured):**
- **It keys** — `TX_ACTIVE` confirmed, clean unkey; `TX_ALLOWED` gate works. **Key-up ≈ 103 ms.**
- **Clean 1000 Hz tone on a monitoring receiver.**
- **Encoded ≈ 230 B/frame** (min 5, max 245), ~25 fps ≈ 46 kbps.
- **Window 2048 B (HELLO-confirmed)**, ~8.6 frames; a one-shot 3 s clip **blocked 28/80** (min credits
  15) and recovered — **healthy backpressure** (host produces faster than the device drains at real
  time), never neared the 2 s write timeout, audio clean.
- **`tx_lead_seconds`: 0.2 clipped, 0.5 clean** → `DEFAULT_TX_LEAD_SECONDS = 0.5` (spec +
  `radio.toml.example` regenerated; the golden test enforces the match).

**Verified (no hardware for the suite):** `uv run pytest` → **972 passed, 5 skipped** (+11 new tests:
transport `tx_stats`/blocked/reset, `_format_tx_stats`, key-up latency, `--tx-tone` hint, `--tx-lead`
override, the `_doctor_settings` regression, and the connect-hint). Live keying done by the operator.

**Follow-ups (not this cycle):** first-connect reliability (lengthen connect timeout / boot-settle
retry — ADR 0066 territory); an Opus **bitrate cap** to ease window pressure (~230 B/frame is high for
a tone — trades fidelity, needs its own analysis); DTMF-over-kv4p-RF measurement + installer kv4p path
+ conditional Mumble-banner gate (all still open from ADR 0067/0068).

---

## kv4p bring-up — two silent-failure detections + the user docs (ADR 0068) (2026-07-18)

Ships the kv4p user docs **and** the two doctor detections that make them mostly unnecessary — the
detection and the prose are the same fact. PR #120 is merged (`origin/master` tip `b62ff53`); branched
fresh (`kv4p-bringup-detections-docs`), not stacked. **No hardware, no keying** — both detections are
pure logic over injected wire data, verified through test seams.

**The two detections (`doctor.py::_kv4p_connect_probe`):**
- **Pre-KISS firmware.** On a failed handshake, `_sniff_pre_kiss_firmware(port)` re-opens the port
  (DTR/RTS low — the reset-on-open dumps the board's boot frames) and reports pre-KISS **only** on a
  positive tell: the `de ad be ef` delimiter present AND no KISS `FEND` (`0xC0`) AND no `KV4P` prefix.
  → *"this board is running pre-KISS firmware — flash v17"* pointing at `docs/kv4p-setup.md`. The
  delimiter is a **new marked constant** (`_PRE_KISS_DELIMITER`) — it was nowhere in the tree. Boot
  banner deliberately not used (exists in both firmwares).
- **Band mismatch.** When a HELLO is present, compare `RfModuleType(v.rf_module_type)` against the
  configured `kv4p.module_type` (via `module_type_from_band()`). On disagreement → a **WARN** (not a
  FAIL): *"band mismatch: board reports VHF, you configured UHF — the hwconfig NVS is probably
  missing/wrong; reflash the board-config"*. Catches a wiped/never-written board-config, invisible on
  the protocol otherwise.
- Also: fixed a stale "16 kHz ADPCM" phrase in the doctor module docstring (it's Opus now, ADR 0064/65).

**The docs (integrate, not bolt on):**
- **NEW `docs/kv4p-setup.md`** — flashing/first-run guide: two-writes-in-order (firmware `0x0` then
  board-config `0x9000`; the merged `0x0–0xeafff` image **wipes** the NVS, so firmware-only is the
  trap); six board-config images + reading the PCB silkscreen (v2.0e→v2.0d config); web-flasher
  port-lock (fully quit Chrome) with the `esptool` terminal escape; by-id path not `/dev/ttyUSB0`; run
  doctor first; set `kv4p.frequency` (no knob, no invented default); reset-on-open, ADR-0066 flag loss,
  and DTMF-is-an-open-bench-item all stated plainly.
- **`install.md`** forks by radio — kv4p path is `uv sync --extra kv4p` with **no** PortAudio/sound-card
  steps (the easier radio). **`troubleshooting.md`** early fork (kv4p has no volume knob, no capture
  level). **`configuration.md`** gains the `[kv4p]` section and owns the `kv4p.squelch` (SA818 level
  0–8) vs `audio.squelch` (gate mode) collision; notes `audio.squelch="cat"` valid on kv4p, rejected on
  baofeng. **`README.md`/`architecture.md`/`deployment.md`/`hardware-bringup.md`** move kv4p from
  "planned" to supported; `hardware-bringup.md` stays the AIOC bench reference (not merged).
- Stale "TM-V71A only" on `audio.squelch` → "TM-V71A and kv4p" (spec.py + radio.toml.example).
- ADR **0068** (new); ADR index backfilled with the missing 0064–0068 rows.

**Verified (no hardware, no keying):** `uv run pytest` → **961 passed, 5 skipped** (env with opus
installed; +10 new doctor tests). New tests: pre-KISS sniff (delimiter→True; FEND/KV4P/no-delimiter→
False; can't-open→False; probe-level pre-KISS line + generic-when-inconclusive) and band-mismatch
(WARN on disagree, none on agree), all via the existing `_ProbeTransport` seam + a stubbed `_open`.
Grep gate: no stale "kv4p planned / no backend" statements remain (the surviving "planned" lines are
the Kenwood TM-V71A).

**Facts recorded from the bench brief (not repo-derived, marked as such):** the `de ad be ef`
delimiter, flash offsets `0x0`/`0x9000`, the `0x0–0xeafff` blob span, the six `board-config-*.bin`
images, PCB revs (v1x/v2abc/v2de; v2.0e→v2.0d), and the 435.000/400.000 NVS-default frequencies. The
repo had only observed 400.000 (HANDOFF, ADR 0066).

**Live follow-ups (next cycles, NOT this one — deferred in ADR 0067/0068):**
1. **Installer kv4p path.** `scripts/install.sh`/`.ps1` have no kv4p option; `--with-hardware` is the
   AIOC path (drags a sound card). Add a kv4p install path.
2. **Conditional banner gate.** When (1) lands, the installers' `check_mumble_importable()` "earn the
   banner" gate is wrong for a kv4p install with no Mumble — make it conditional on the mumble extra.
3. **DTMF-over-kv4p RF measurement.** Survives Opus in software; RX level vs `NATIVE_ENERGY_FLOOR`
   unmeasured on real RF — fails silently just under the floor. A bench measurement, not a code change.

---

## Extras taxonomy — a node installs what it needs and nothing else (ADR 0067) (2026-07-18)

Factors the optional-dependency leaves and composes the backends from them, so a **kv4p node installs
pyserial + the Opus stack and no system library at all** — no sound card, no PortAudio, no Mumble. PR
#119 is merged (`origin/master` tip `5d9f613`); branched fresh (`extras-taxonomy-kv4p`), not stacked.

**The problem.** `hardware = [pyserial, sounddevice]` and the Opus stack rode the `mumble` extra, and
`opuslib` (the binding both the kv4p codec and pymumble import) was named nowhere — it arrived only
transitively via pymumble. So a kv4p node had to run `--extra hardware --extra mumble` to get pyserial +
libopus, dragging in sounddevice/PortAudio/pymumble it never calls. `uv sync` is exact, so naming the
wrong set silently uninstalls the right one.

**The taxonomy (ADR 0067) — the table of facts for the docs cycle:**

| extra | installs | for |
| --- | --- | --- |
| `serial` | pyserial>=3.5 | leaf: serial line (AIOC PTT / kv4p transport) |
| `soundcard` | sounddevice>=0.4 (+ system libportaudio2) | leaf: AIOC sound card |
| `opus` | opuslib>=3.0.1 + opuslib-next-bundled (env-marked carrier) | leaf: Opus codec stack |
| `tts` | piper-tts, onnxruntime | leaf: Piper TTS (unchanged) |
| `hardware` | serial + soundcard | AIOC/Baofeng backend (**same closure as before**) |
| `kv4p` | serial + opus | kv4p HT backend (**new**) |
| `mumble` | opus + pymumble (git tarball) | Mumble link (**same closure as before**) |

**Changed this cycle:**
- **`pyproject.toml`:** the leaves + composites above (PEP 621 self-referencing extras). `opuslib>=3.0.1`
  now named **explicitly** in `opus` (was transitive via pymumble; pymumble still pins ==3.0.1 so no
  drift). ADR 0057's `opuslib-next-bundled` env-marker gating moved into `opus` **intact**. `uv.lock`
  regenerated — **no version drift**, only extras regrouped.
- **`link/_opus.py`:** `opus_install_hint(*, extra="mumble", …)` — the hint now names the caller's extra.
- **`backends/kv4p/audio.py`:** `_load_opus()` passes `extra="kv4p"`, so a kv4p node with a missing
  libopus is told `uv sync --extra kv4p`, not `--extra mumble`. Docstrings updated (ADR 0067).
- **`AGENTS.md`** setup block: new leaves/composites documented; stale `hardware`→qrcode comment fixed.
- **`test_opus_loader.py`:** new case — `opus_install_hint(extra="kv4p")` yields `--extra kv4p` (and not
  `--extra mumble`), per-platform tails preserved.
- **ADR 0067** (new).

**Verified (no hardware, no keying):**
- Clean `uv sync --extra kv4p` → **pyserial 3.5 + opuslib 3.0.1 + opuslib-next-bundled 0.1.1**, and
  `sounddevice`/`pymumble_py3` confirmed **absent**. `audio._load_opus()` loaded libopus from the carrier
  (`opuslib_next/_native/libopus.so`, **not** a system lib); Opus encode→decode round-tripped
  (3 B packet → 1920 B / 960-sample PCM).
- Clean `uv sync --extra hardware` → pyserial + sounddevice (+ cffi), no opus/pymumble — closure
  identical to before the split.
- `uv run pytest`: bare **941 passed, 14 skipped**; with `--extra mumble` **951 passed, 5 skipped**.

**Compatibility (stated loudly):** `update-radio-server.sh`, `scripts/install.sh`, `scripts/install.ps1`
are **untouched** — their `--extra hardware --extra tts --extra mumble` / `--extra mumble` lines resolve
to the identical package set. Kris's deployed box keeps working exactly.

**Live follow-ups (next — the docs cycle, NOT this one):**
1. **User docs** still say `--extra hardware --extra mumble`: `docs/install.md`, `getting-started.md`,
   `deployment.md`, `configuration.md`, `hardware-bringup.md`. Update them from the table above; add a
   kv4p install path (`--extra kv4p`).
2. **Installer kv4p path.** `--with-hardware` is the AIOC path (would drag a sound card onto a kv4p node);
   no kv4p install path exists yet. When one is added, the installers' "earn the banner" gate
   (`check_mumble_importable()`, ADR 0057) is **wrong for a kv4p install with no Mumble** — make it
   conditional on whether the mumble extra was requested. It stays correct for today's mumble installer.

---

## kv4p HT — connect on a running board, re-founded on shipped firmware (ADR 0066) (2026-07-18)

Makes `Kv4pTransport.connect()` work on a **not-just-reset** board and fixes a confirmed **NVS data-loss
bug**. PR #118 is merged (`origin/master` tip `bb66396`); branched fresh (`kv4p-connect-running-board`),
not stacked.

**The re-derivation (shipped v2.0.0.1 `3f0e809`, read verbatim this cycle).** The last two diagnoses —
"sequence gate" (ADR 0062) then "edge-triggered reports" (ADR 0064) — were inherited from the wrong pin
(`e9935bd`). Shipped firmware: `handleCommands` accepts a `HostDesiredState` iff `param_len == 22`, then
**whole-struct memcpy** (no session, no sequence gate, no mask); `reconcileDesiredState` persists it to
NVS **unconditionally**; `deviceStateFlags()` echoes the **whole** `desiredState.flags` word; reports
fire **on-dirty AND periodically**. So a probe that lands *is* answered — the real failure is a
**silently dropped probe** (the `param_len` gate gives no error), and the old **neutral-zeros probe
permanently wrote freq 0.0 + `tx_allowed=false` to NVS** on every connect/close.

**Changed this cycle:**
- **`transport.py connect()` re-founded (ADR 0066): passive-first → elicit-with-retransmit → restore.**
  (1) Listen first — a board already streaming reports is read with **zero writes**. (2) Else send an
  elicit (`ENABLE_STATUS_REPORTS` on, `RADIO_CONFIG_VALID` off — no retune), **retransmitting** until the
  flag is echoed. (3) **Restore** the tuning read back (freq/CTCSS/bw/memory) with safe flag defaults
  (`RADIO_CONFIG_VALID|HIGH_POWER|RSSI_ENABLED`; **TX_ALLOWED left cleared**), undoing the elicit's
  zero-clobber of the stored frequency.
- **`close()` de-clobbered:** the PTT-off reconcile now echoes the last known state (PTT cleared), not
  zeros — no NVS write. **`_session_flags` → `_link_flags`** (kept asserted for the connection's life; the
  "session mask" model was `e9935bd` fiction). Docstrings/comments re-founded on shipped behaviour.
- **`radio.py` backend also de-clobbered** (surfaced at the bench): `Kv4pHt`'s initial reconcile sent
  `freq_rx=0.0` (RADIO_CONFIG_VALID off), which shipped still persists — so it now **seeds its tuning from
  the `DeviceState` `connect()` returns**. This is what makes `doctor --rx-level` (which builds the
  backend) non-destructive, not just the bare connect probe.
- **`doctor.py` wording made true:** the kv4p connect probe "does not key" but is **not read-only** —
  it preserves the board's tuned frequency/CTCSS and resets TX-allow/filter flags to safe defaults. The
  TX_ALLOWED / RADIO_CONFIG_VALID report lines updated. `--rx-level` stays genuinely read-only.
- **ADR 0066** (new); **ADR 0062 Decision 1 amended** (marked historical); `frames.py`'s
  `HOST_STATE_*_MASK` re-labelled a host-side grouping, not a firmware mask.
- **Tests:** `FirmwareFakeSerial` re-founded on shipped acceptance (whole-struct memcpy, whole-flags echo,
  conditional retune, **unconditional persist** with a modeled `persisted` view). New regressions: passive
  zero-write path; elicit-then-restore preserves the stored frequency and leaves TX_ALLOWED safely off;
  `close()` doesn't clobber; the backend seeds tuning from connect's state.
- **Bench (kv4p HT, SA818_UHF, `/dev/ttyUSB1`, RX-only, never keyed):** `connect()` **succeeded** on the
  running board. Wire capture (hand-decoded) proved the real cause: the board **resets on open** (a HELLO
  arrives despite DTR/RTS held low), so a single probe races the boot and is lost — `connect()` sent
  **3–4 elicit retransmits** (seq 1→4, flags `0x1000`) before the board echoed, then a **restore** (flags
  `0x1019`, freq `0x43c80000`=400.0 — the board's real appliedState freq). **NOT** edge-triggering or a
  sequence gate. With RX audio opened (RX-only): **75 Opus frames in 3 s (25/s), 144000 samples, rms ≈
  6979, 0 drops** — live RX across the backend for the first time.

**Firmware limitation (recorded, ADR 0066):** shipped exposes no read-before-write, so on a reports-off
board the operator's *flag* bits (TX_ALLOWED/power/filters) are unrecoverable — only the *tuning* is
preserved; TX is left safely off. There is also a sub-second window during the elicit where NVS holds
zeros before the restore lands.

**Live follow-ups (next cycles, NOT this one):**
1. **Extras taxonomy.** A kv4p-only extra so libopus arrives without `--extra mumble`.
2. **User docs.**

---

## kv4p HT — the Opus audio codec: replace the dead ADPCM edge (ADR 0065) (2026-07-17)

Implements what ADR 0064 pinned. **Audio now actually crosses the kv4p backend.** PR #117 is merged
(`origin/master` tip `241c547`); branched fresh (`kv4p-opus-codec`), not stacked.

**Changed this cycle:**
- **`backends/kv4p/audio.py` rewritten ADPCM → Opus.** Deleted the IMA-ADPCM codec, the `soxr` 16k↔48k
  resamplers, the 249↔747 re-blocker, and the step/index tables. New surface (same class names):
  - `RxAudioDecoder.push(packet) -> AudioFrame` — `opuslib.Decoder(48000, 1)`, one Opus packet → one
    canonical 1920-sample frame. **No re-block, no resample** (Opus is native 48 kHz; `AudioFrame` has no
    length contract). A corrupt/truncated packet (`opuslib.OpusError`) is **dropped** (empty frame, no
    raise) so a bad wire byte can't kill the RX pump.
  - `TxAudioEncoder.push/.flush` — `opuslib.Encoder(48000, 1, APPLICATION_AUDIO)`, `vbr=1`,
    `max_bandwidth=NARROWBAND` (**mirrors the firmware's own RX encoder** — ADR 0064; a wrong setting
    decodes fine and just sounds wrong on the air). Re-blocks arbitrary 48 k input to exact 1920-sample
    frames (the only re-blocker left); `flush` zero-pads the tail (padding, never sample loss).
- **libopus loads lazily** (first encode/decode, not at import / not at `Kv4pHt.__init__`, so the ~30
  codec-free backend tests need no libopus) via the shared `link/_opus.py ensure_opus_loadable()` shim
  (ADR 0056/0057). Missing libopus → **`Kv4pOpusUnavailable`** with an actionable hint, not an
  `ImportError` three frames down.
- **`radio.py`:** `receive()` decodes each queued packet straight to a frame — **removed #117's
  non-128-byte scaffolding drop** (its job is done). Docstrings/wiring updated (audio path is live Opus).
- **`transport.py`:** comment-only — "Opus packet" wording; flow-control headroom note (narrowband VBR
  Opus ≪ ADPCM's ~89 kbit/s, so the RX deque depth `256` has ample headroom — revisit on real numbers).
- **ADR 0065** (new): frame geometry (1920/3840), TX-settings-and-source, ADPCM deletion, lazy-load +
  `Kv4pOpusUnavailable`, the packaging gap, flow-control headroom.
- **Tests:** `test_kv4p_audio.py` rewritten as the Opus suite (round-trip, corrupt-drop, TX re-block
  loses-no-samples, missing-opus→actionable-error, frame geometry). `FirmwareFakeSerial` grew an
  `emit_rx_audio` Opus path + two end-to-end RX tests. `test_kv4p_radio.py` RX/TX tests updated to Opus.
  Codec-behaviour tests `pytest.importorskip` opus (skip green bare); the missing-opus test always runs.
  **Bare `uv run pytest`: 941 passed, 14 skipped. With `--extra mumble`: 950 passed, 5 skipped.**

**Packaging gap (recorded, NOT fixed — it's the next-but-one cycle):** `opuslib` rides the **`mumble`
extra** today, so a kv4p node currently needs `uv sync --extra mumble` for libopus even though it has
nothing to do with Mumble. The clean kv4p-only extra is the extras-taxonomy cycle.

**Live follow-ups (next cycles, NOT this one):**
1. **Running-board handshake.** `connect()` completes only right after a boot (shipped status reports are
   edge-triggered — ADR 0064). Read shipped `reconcileDesiredState` for the exact dirty-trigger, then make
   `connect()` robust against a no-change probe. (Bench: RTS-pulse reset the board before each run.)
2. **Extras taxonomy.** Give kv4p its own extra so libopus arrives without `--extra mumble` (no sound
   card / PortAudio / pymumble needed for a kv4p node).

---

## kv4p HT — re-pin the spec to shipped firmware, write ADR 0064, fix the audio command IDs (2026-07-17)

Follow-up to the bench-debug cycle below. That cycle *found* the firmware drift; this one **re-pins the
spec to what actually ships** and records the real protocol in **ADR 0064** — from source, not the wire
(the discipline that isolated the bug). **No codec work** (deferred, see follow-ups). PR #116 is merged
(`origin/master` tip `8b2dcf6`); branched fresh, not stacked.

**The re-pin (authoritative, read from GitHub source this cycle):**
- Shipped firmware is **v2.0.0.1** (`3f0e809baa02a946c3f0602681303f600c321d31`, released 2026-06-01;
  v2.0.0.0 `6a3b3e30…` matches it). Our old pin **`e9935bd…` is unreleased — `FIRMWARE_VER 17`, exactly
  **+44 commits ahead** of v2.0.0.1 (which is *also* `FIRMWARE_VER 17`). The version number cannot
  discriminate the two protocols; that is the whole trap.
- **Audio (both directions) moved `0x0C` → `0x07`, and the codec is Opus, not ADPCM.** Shipped RX encoder:
  Opus, 48 kHz mono s16, `OPUS_APPLICATION_AUDIO`, `OPUS_FRAMESIZE_40_MS` (40 ms), `vbr=1`,
  `OPUS_BANDWIDTH_NARROWBAND`, **no length prefix** — one Opus packet per `RX_AUDIO` KISS frame, bounded by
  `PROTO_MTU=2048`. This replaces the 128-byte/249-sample block contract; no resample, no re-block (Opus is
  native 48 kHz). Our old pin `e9935bd` genuinely *is* `0x0C`+IMA-ADPCM — our code was a correct read of an
  unreleased commit.
- **What else moved — checked, not assumed** (full `protocol.h`/`globals.h` diff read): `e9935bd` adds the
  BT/BLE `ProtocolSession` plumbing, `HOST_STATE_SESSION_FLAG_MASK`/`GLOBAL_FLAG_MASK`, and the ADPCM
  `globals.h` constants — none exist in shipped. Everything else is **byte-identical**: KISS framing/vendor
  envelope, `HostDesiredState` (22 B), `DeviceState` (26 B), `Version` (17 B), all flag bits, and
  `HOST_DESIRED_STATE=0x0D`/`HELLO=0x06`/`WINDOW_UPDATE=0x09`/`DEVICE_STATE=0x0B`.
- **Correction to last cycle's deferred item 2:** shipped has **no sequence gate** — `handleCommands`
  applies `HOST_DESIRED_STATE` unconditionally on `param_len==22` via a whole-struct `memcpy` (no flag
  mask). `connect()` times out on a *running* board because status reports are **edge-triggered**
  (`sendCurrentDeviceState` emits only when `deviceStateDirty` AND `ENABLE_STATUS_REPORTS`) — a no-op probe
  changes nothing and draws no echo. The `appliedSequence` sync stays correct. ADR 0062's sequence-gate
  rationale was `e9935bd`-only.

**Changed this cycle:**
- **ADR 0064** (new): the shipped protocol, the Opus params, the "version can't discriminate" point, and
  three decisions — (1) **support shipped only**, explicitly rejecting command-ID sniffing to dual-support
  the unreleased line; (2) **PR #111's IMA-ADPCM codec (`audio.py` + `tests/test_kv4p_audio.py`) is dead**,
  marked now, *deleted by the Opus cycle* (deletion belongs with the replacement so the tree is never
  without a decoder); (3) the Opus reuse path (ADR 0056/0057 `_opus.py`) + a packaging note (opuslib rides
  the `mumble` extra today — a kv4p codec is a second consumer).
- **ADRs 0061/0062/0063 amended:** citation `e9935bd…` → shipped `3f0e809…`, each with a caveat block; 0062
  corrects the sequence-gate rationale; 0063 corrects the `flags &= …_GLOBAL_FLAG_MASK` firmware quote
  (shipped keeps the whole flags word — our "ride every flag every frame" discipline stays correct).
- **`frames.py`:** `RcvCommand.HOST_TX_AUDIO` and `SndCommand.RX_AUDIO` `0x0C → 0x07`; source-of-truth SHA
  updated. **`radio.py`:** `receive()` now **drops a non-128-byte block** (returns an empty frame) instead
  of raising — a live shipped board sends variable-length Opus, and the `receive()` call sites (`rx/pump`,
  `controller/engine`, `doctor`) are unguarded, so the old ADPCM `ValueError` would have killed the RX
  capture task. **`audio.py`/`transport.py`:** comment-only (SHA + dead-code banner + shipped-handshake NB).
- **Tests:** `test_kv4p_frames` command asserts → `0x07`; new `test_receive_drops_a_non_adpcm_block_without_raising`.
  Full suite green.

**Live follow-ups (next cycles, NOT this one):**
1. **Opus codec.** Delete `audio.py`'s ADPCM/resampler/re-blocker + `tests/test_kv4p_audio.py`; add an Opus
   RX decoder (`opuslib.Decoder(48000, 1)`, one packet per `RX_AUDIO` frame) and TX encoder via the ADR
   0056/0057 infra (`ensure_opus_loadable`). This is what actually makes audio flow.
2. **Handshake bootstrap on a running board.** Now correctly understood as edge-triggered status reports,
   not a sequence gate — read shipped `reconcileDesiredState` to pin the exact dirty-trigger, then make
   `connect()` robust against a no-change probe.
3. **Packaging.** opuslib must be available to a kv4p node without the `mumble` extra (compounds the
   kv4p-only-extra question the docs cycle flagged).

**No GitHub instruction issue this cycle** — recorded in the PR. RX-only, board not keyed.

## kv4p HT bench debug — the boot-HELLO latch, the module band, and the v17 firmware drift (2026-07-17)

First cycle to drive the real board (kv4p HT, PCB v2.0e, SA818_UHF, firmware **v17**, on the CP2102N
by-id path — RX-only, **no keying**). Chased "`doctor --backend kv4p --rx-level` captures zero audio."
The stated hypothesis — *nothing we send is landing* — was **disproven on the bench**; the truth is a
device→host firmware drift. This cycle lands the three fixes that the bench **confirmed** and pins the
authoritative firmware source for the (larger) audio fix, which is its own next cycle.

**What the bench proved (RX-only, never keyed):**
- **Frames land.** A 22-byte `HostDesiredState` was accepted and echoed (`ENABLE_STATUS_REPORTS`
  came back in `DeviceState.flags`). The host→device codec is correct; `HOST_DESIRED_STATE` is `0x0D`,
  unchanged across the drift, so tuning/PTT/flags all reach the device.
- **The zero-RX-audio root cause is a firmware-version drift, authoritatively pinned.** The board
  streams RX audio on **vendor command `0x07`** as **Opus** (variable-length frames), but our code
  expects `SndCommand.RX_AUDIO = 0x0C` + 128-byte IMA-ADPCM. Commit `3012612f` ("BLE+move adpcm to a
  different ID") moved both audio commands `0x07 → 0x0C` on the unreleased BLE/main line; **every
  shipped release (v2.0.0.0, v2.0.0.1) — both `FIRMWARE_VER = 17` — still uses `0x07` + Opus**. Our repo
  pins `e9935bd`, which is **44 commits ahead of that move** — an **unreleased** commit no shipped
  firmware matches. We pinned repo-tip and assumed the shipped v17 matched it; it does not.
- **The board is UHF** (HELLO: SA818_UHF, 400–480 MHz) and HELLO is boot-only, so the VHF-default band
  bug is real.

**Fixes landed this cycle (the three the bench confirmed — audio is NOT touched here):**
- **`transport.connect()` now proves a round trip** instead of latching the boot HELLO. It waits for a
  `DeviceState` that echoes the session flag we sent (`ENABLE_STATUS_REPORTS`) — the firmware ORs
  session flags into `DeviceState.flags`, and a boot HELLO's embedded state has session flags `0`, so it
  can no longer be mistaken for an ack. A dropped/ignored frame is now a **loud timeout**, not a
  false-green. Bench-confirmed: proves the round trip on a freshly-booted board; fails loud on a running
  one. New helper `_session_acknowledged()`; `DeviceStateFlag` imported. The 3 existing connect tests
  encoded the old "boot data is enough" behaviour and were updated; **new firmware-accurate fake**
  (`FirmwareFakeSerial`) drops a wrong-length `HOST_DESIRED_STATE` and only echoes session flags once a
  frame is accepted — the regression the old accept-anything fakes could never catch.
- **`kv4p.module_type` config (`vhf`/`uhf`, default `vhf`, verify-on-bench)** decides the frequency band
  when no HELLO arrives (the normal case on a server restart against a running board — ADR 0062). Threads
  `radio.py` (new `Kv4pBand` StrEnum + `module_type_from_band`) → `spec.py` → `app.py`/`doctor` →
  `create_radio`; `--module-type` CLI override; `radio.toml.example` regenerated. Without it a UHF board
  rejects every UHF frequency as out-of-band.
- **doctor honesty:** the probe's `TX_ALLOWED`/`RADIO_CONFIG_VALID` lines were WARNs advising
  `set kv4p.tx_allowed = true`; but the probe is read-only (neutral state) so those flags reading clear
  is **expected** — now reported, not warned. `--rx-level`'s open-fail and zero-frame messages are
  backend-aware (no more "is the AIOC capture device correct?" on a kv4p run; kv4p points at the connect
  probe). Full suite **955 passed, 5 skipped**.

**Surfaced, deferred to the follow-up (firmware drift consequences, NOT fixed here):**
1. **RX audio (the actual symptom).** To make audio flow, re-pin the spec to a **shipped** release
   (v2.0.0.1), change the audio command to `0x07`, and **replace the ADPCM codec with Opus** (both
   directions — TX audio moved too). Alternatively decide the deployment runs a firmware matching our
   current `e9935bd` pin — but that is an unreleased build, so re-pinning to shipped is the safer call.
   This is a real `frames.py`/`audio.py` change with its own bench pass; it is the next cycle.
2. **Handshake bootstrap on a *running* board.** Shipped v17 appears to gate session-flag application
   behind the sequence check, so `connect()` only completes right after a device boot (a stale-sequence
   probe gets no echo). Today `connect()` fails loud on a running board; the follow-up should make it
   robust (e.g. seed the sequence high, or a scoped reset) once the firmware is re-pinned and understood.

**No new ADR** (this repairs ADR 0061/0062/0063 tooling against the real device). **No GitHub instruction
issue** — recorded in the PR. Bench scripts were scratch-only (session scratchpad), not committed.

## kv4p HT backend — `doctor` bench diagnostic learns the kv4p (2026-07-17)

Teaches `python -m radio_server.doctor` the kv4p backend (the bench tool an operator runs *first* when
the board is plugged in). Previously it was AIOC/Baofeng-shaped throughout: it never read
`server.backend`, `_build_backend()` hardcoded `create_radio("baofeng", …)`, the default check probed a
PortAudio sound card, and `--key-test` bisected a DTR/RTS line. **No new ADR** (implements the bench
tooling ADR 0061/0062/0063 already specified). **Non-goal (next cycle):** user docs + the packaging
question — see the note at the end.

- **Dispatch (behaviour-preserving).** New `_resolve_doctor_backend(args)`: the `--backend
  {baofeng,kv4p}` override if given, else `server.backend` **iff it is `kv4p`**, else `baofeng`.
  Rationale: `server.backend` defaults to `mock` and the AIOC bring-up runs doctor *before* flipping it
  to `baofeng`, so every non-kv4p value resolves to the AIOC checks — **today's behaviour, unchanged**.
  The baofeng paths are kept intact and routed to for baofeng (byte-for-byte). `--link` is
  backend-independent and handled before the split.
- **`--rx-level` / `--tx-tone` / `--dtmf` needed no rewrite** — their measurement/decode primitives
  already drive only the `Radio` surface. The sole coupling was `_build_backend` hardcoding `baofeng`;
  it now dispatches on `cfg["backend"]` (kv4p → `create_radio("kv4p", serial_port/squelch/
  tx_lead_seconds/high_power/tx_allowed/frequency)`). The three "AIOC backend" error strings name the
  resolved backend (baofeng wording preserved). `--tx-tone`'s PTT-line banner is now conditional
  (kv4p has no line). `--rx-level`'s *silent* hint is kv4p-specific: no OS capture level / no volume
  knob — the SA818 volume is a firmware constant (kv4p-ht `globals.h DEFAULT_VOLUME 8 → hw.volume`,
  **verify against pinned firmware / on bench**) and **not** in `HostDesiredState` (confirmed in-repo:
  `frames.py` has no volume field), so the only host levers are `kv4p.squelch` + `audio.vad_on_rms`.
- **The star — kv4p connect probe** (`_kv4p_connect_probe`, replaces the sound-card check as the
  default). Read-only, **never keys** (`Kv4pTransport.connect()` sends only the neutral state +
  `ENABLE_STATUS_REPORTS`). Uses `Kv4pTransport` directly, **not** `Kv4pHt` (whose ctor eagerly
  reconciles/configures NVS — a probe must observe, not mutate). Prints: HELLO (fw/module band/
  windowSize/features — absent is a WARN, it only fires at ESP32 boot, ADR 0062), the DeviceState
  (applied freqs/bw/ctcss/squelch/mode/rssi + flags **decoded into words**), a non-`NONE` `lastError`
  as a FAIL (never a silent pass), and whether `TX_ALLOWED`/`RADIO_CONFIG_VALID` survived the reconcile.
  Degrades to a clear FAIL when the `hardware` extra / device is absent (still runs in CI). Plus
  `_check_kv4p_serial` (by-id CP210x/CH340, `/dev/ttyUSB*` not the AIOC's `ttyACM*`, dialout via a
  lines-low open). **This one command settles a pile of guardrail-1 items on bench day** (windowSize
  2048, whether pyserial's open resets the board, the real module band, flag survival) — the docstring
  says to run it first.
- **`--key-test` for kv4p = a KEYING test** (`_kv4p_key_test` + testable `_kv4p_keying_core`). No line
  to bisect; instead reconcile `PTT_REQUESTED` on, assert `TX_ACTIVE` came back (a withheld key raises
  `Kv4pKeyingError` → **loud FAIL**, never reported as success — exercises the `TX_ALLOWED` gate, ADR
  0063), hold the hard cap, drop, assert it cleared. **Every RF guard reused unchanged** — refuses
  non-interactive/CI, dummy-load warning, typed CONFIRM, `_KEY_TEST_SECONDS` cap.
- **`--dtmf` on kv4p** is unchanged code, but the module docstring + this handoff record that running it
  is the bench measurement that settles the arc's oldest open question — DTMF through the lossy 16 kHz
  ADPCM path against the native Goertzel decoder (open since cycle 1). A measurement, not a code change.
- **Tests (`tests/test_doctor.py`, +14):** backend dispatch (kv4p threads every setting, baofeng
  unchanged, unknown → `ValueError`; `_resolve_doctor_backend` flag/`server.backend`/default) via a
  `create_radio` stub + `conftest.make_settings`; the connect probe against a fake transport (with/
  without HELLO, `lastError` surfaced, flags decoded, missing-extra degrade, missing device); the
  keying core (`FakeTransport(grant_tx=True)` → pass, `grant_tx=False` → loud FAIL) reusing
  `test_kv4p_radio`'s `FakeTransport`/`make_radio`; and the RF guard refusing non-interactive on kv4p.
  All existing baofeng doctor tests pass untouched. Full suite **950 passed, 5 skipped** (936 + 14).

**Decisions noted:** dispatch falls back to baofeng for any non-kv4p `server.backend` (preserves the
documented AIOC-before-flip workflow); the connect probe drives `Kv4pTransport` not `Kv4pHt` (read-only,
no NVS mutation); `_check_kv4p_serial`/`--tx-tone` reuse the baofeng shape minus the PTT-line concept.

**NEXT CYCLE — user docs + the packaging question (flagged, NOT built this cycle):** leave `install.md`,
`configuration.md`, `troubleshooting.md`, `hardware-bringup.md` alone until then. The `hardware` extra is
pyserial + sounddevice, but **a kv4p node needs no sound card at all** (no sounddevice, no
`libportaudio2`). A pyserial-only `kv4p` extra would delete install.md's PortAudio step and
troubleshooting.md's whole premise for kv4p users — a pyproject + installer + docs change with real
blast radius. Then the empirical **hardware bring-up** phase ("plug it in, it keys up clean").

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR instead
of an issue comment/label.

## kv4p HT backend — wiring: `server.backend="kv4p"` selectable/configurable/startable (2026-07-17)

Makes the `Kv4pHt` class (ADR 0063, prior cycle) reachable: factory registration, a `[kv4p]` config
section, and the `api/app.py` composition branch. **No new ADR** — follows ADR 0063's complete-state
model and the frequency recommendation. Still no hardware touched. **Non-goals (next cycle):**
`doctor` bring-up and the user docs (install/configuration/hardware-bringup prose).

- **`radio_server/backends/kv4p/radio.py`** — `Kv4pHt.__init__` gained four config params that ride
  the **initial** desired state (before the first reconcile), plus module `DEFAULT_*` constants that
  `config/spec.py` imports (source-of-truth, like `aioc_baofeng.py`): `squelch` (SA818 level 0..8,
  default `4`), `high_power` (HIGH_POWER flag, default True), `tx_allowed` (TX_ALLOWED NVS gate,
  default True), `frequency` (optional Hz — when set, `set_frequency` at construction reusing the
  existing out-of-band validation; unset leaves the device on its NVS frequency, **no invented
  default on the air**). `DEFAULT_SERIAL_PORT = /dev/ttyUSB0` (CP210x/CH340, **not** the AIOC's
  `ttyACM0`).
- **`radio_server/config/spec.py`** — `[kv4p]` block (serial_port/squelch/tx_lead_seconds/
  high_power/tx_allowed/frequency), a new `coerce_optional_int` (for the `None`-default frequency),
  the six keys added to `_ADVANCED_KEYS`, and `server.backend`'s description now names `kv4p`. The
  `kv4p.squelch` description owns the collision with `audio.squelch` and the level-0 caveat.
- **`radio_server/config/save.py`** — `kv4p` group banner (between baofeng and mumble),
  `kv4p.frequency` in `_COMMENTED_DEFAULTS` (renders commented, no invented value); `save_settings`
  now skips an optional `None` (would be unwritable TOML). `radio.toml.example` regenerated (the
  byte-exact contract test guards it).
- **`radio_server/backends/factory.py`** — `Kv4pHt` registered; `available_backends()` →
  `(mock, v71, baofeng, kv4p)`.
- **`radio_server/api/app.py`** — the `elif backend == "kv4p"` branch passes the `[kv4p]` settings
  through, same shape as baofeng. It **relaxes the `audio.squelch="cat"` rejection**: cat is valid
  here (real busy line), but `cat` + `kv4p.squelch=0` raises a `RuntimeError` naming **both**
  settings (at level 0 the SQ pin never asserts → busy latches True → a cat scan dwells forever).
  Baofeng + cat still raises exactly as before.
- **Tests:** `tests/test_backend_wiring.py` (new, 5 — build_app passthrough + the squelch-gate
  combinations, monkeypatching `create_radio` so no serial is opened); `tests/test_config.py` (+7:
  kv4p resolve/coerce/round-trip, frequency optional/reject, the `_ADVANCED_KEYS` known-keys guard);
  `tests/test_kv4p_radio.py` (+6: config flags/squelch on the first frame, tx_allowed/high_power
  withheld, frequency tune-once/no-tune/out-of-band). Count fixes in `test_factory.py` (+kv4p) and
  `test_settings_api.py` (54→60 keys, kv4p render check). Full suite **936 passed, 5 skipped** (918
  baseline + 18), no regressions.

**Decisions noted:** `high_power`/`tx_allowed` default True (a node exists to transmit;
operator-overridable, and `tx_allowed=false` is a real receive-only gate). `kv4p.squelch=4` is a
marked verify-on-bench default. **`module_type` intentionally NOT a config key** — it only picks the
*fallback* band when no HELLO arrives, and a HELLO overrides it; a follow-up if a UHF board with no
HELLO ever needs it. `kv4p.frequency` renders in the settings API as an untyped (string) field
because its default is `None` — cosmetic, coerces fine.

**NEXT CYCLE:** `doctor` bring-up for kv4p (the bench diagnostic — its own reviewable unit) **and**
the user docs together (install/configuration/hardware-bringup). Then the empirical **hardware
bring-up** phase ("plug it in, it keys up clean").

## kv4p HT backend — the `Kv4pHt` class (Radio/CatRadio over transport + audio, ADR 0063, 2026-07-17)

Composes `transport.py` + `audio.py` + `frames.py` into the backend implementing the
`Radio`/`CatRadio` surface — the first real `CatRadio`, the first backend with a genuine `busy`
line, the first where the software `ScanEngine` runs on hardware. Still **fake-transport tested**
(guardrail 6). **Not** factory/config/`app.py` wiring, `doctor`, or the `squelch="cat"` relax —
that is the wiring cycle.

- **`radio_server/backends/kv4p/radio.py`** (`Kv4pHt`):
  - **Complete-state reconcile (the load-bearing rule).** `HostDesiredState` is not a partial update
    — the firmware replaces the whole struct + the whole global-flag word each frame. So the class
    owns a full desired-state model and every mutation is read-modify-write-the-whole-thing then
    `send_desired_state` + `await_applied`. `RADIO_CONFIG_VALID` (gates the `sa818.group` apply),
    `TX_ALLOWED` (hard-gates PTT, NVS-persisted, defaults false), and `RX_AUDIO_OPEN` (session, opens
    RX audio) ride **every** frame. On key-up we set `PTT_REQUESTED` **and assert `TX_ACTIVE` came
    back**, else raise `Kv4pKeyingError` — a silent no-key never becomes dead air.
  - **Keying** mirrors `AiocBaofeng`'s `_keyed` one-shot-vs-streaming discipline (reconciled PTT
    flag, not a line). TX audio: `audio.py`'s re-blocker → `HOST_TX_AUDIO` blocks through the
    transport window. `tx_lead_seconds` knob (value **unknown** — marked default, not AIOC's 0.5 by
    analogy). `receive()` polls the transport RX queue (~one block) → one canonical frame per block.
  - **Units (fail loud, ADR 0063):** freq int Hz ↔ float MHz (simplex — both legs; out-of-band
    **raises**, no silent clamp; quantized to a marked raster); tone Hz ↔ CTCSS **index** (0..38,
    unmapped **raises**, TX tone only); mode ↔ `bw` (FM↔25 kHz / NFM↔12.5 kHz, else **raises**).
  - **`status()`:** `busy = not SQUELCHED` (real SQ-pin carrier detect), `transmitting = TX_ACTIVE`
    (also catches the firmware's ~200 s `RUNAWAY_TX_SEC` auto-drop), `frequency` from `freq_rx`,
    tone/mode inverted.
  - **`capabilities()`** = `SHARED_CAPS | {SET_FREQUENCY, SET_TONE, SET_MODE, SCAN}`. **`SCAN` is in**
    (gates the software sweep, which kv4p can run — a first) but `radio.scan(on)` raises (no native
    toggle; `radio.scan()` is tree-wide dead code — possible tidy). **`SET_CHANNEL` omitted**
    (`memory_id` is an opaque echo, no device memory table) → `UnsupportedCapability`.
- **`radio_server/backends/kv4p/transport.py`** gained one public method: **`send_tx_audio(block)`**
  — TX audio must ride the same encoded-byte credit window, but the transport cycle exposed only
  `send_desired_state`. Reuses the existing private `_write_frame`.
- **ADR 0063** (`docs/adr/0063-kv4p-backend-capabilities-and-units.md`, index row added): the two
  decisions — capabilities/the SCAN reversal, and unit mapping — plus the complete-state rule.
- **Tests:** `tests/test_kv4p_radio.py` — 17 fake-transport tests (a `FakeTransport` echoing the last
  desired state as a synthesized `DeviceState`): the whole-word flag regression, the withheld-key
  raise, unit conversions (all raising before send where invalid), capabilities/`set_channel`/`scan`,
  `status()` busy/tx/freq, one-shot-vs-streaming keying, `receive()` decode + clean timeout. Full
  suite **918 passed, 5 skipped** (901 baseline + 17), no regressions.

**Verify-on-bench (guardrail 1):** DRA818 bandwidth code integers; CTCSS index↔Hz mapping; SA818
tuning raster; per-module default freq bands; `tx_lead_seconds`; and (config cycle) the `squelch`
level default (level 0 → SQ never asserts → `busy` reads True forever).

**NEXT CYCLE:** the **wiring** — factory registration + `config/spec.py` (`server.backend="kv4p"`
with `serial_port`/`baud`/`module_type`/`squelch`/`tx_lead_seconds`), the `app.py` backend branch,
`radio.toml.example`, `doctor` bring-up, and relaxing the `audio.squelch="cat"` rejection
(`api/app.py`) now that this backend reports a real `busy`. Then the empirical **hardware bring-up**
phase ("plug it in, it keys up clean").

## kv4p HT backend — the serial transport (reader thread + window + reconciler, ADR 0062, 2026-07-17)

The I/O layer under `frames.py` — the first kv4p cycle that touches a wire. Still **fake-serial
tested** (guardrail 6; hardware exists but bring-up is its own phase). **Not** the `Kv4pHt` backend
class, `capabilities()`, or factory/config/`app.py` wiring — those compose transport + `audio.py` +
`frames.py` later. Uses the `_serial_factory` DI seam from `aioc_baofeng.py`.

- **`radio_server/backends/kv4p/transport.py`** (`Kv4pTransport`; stdlib + lazy pyserial, the
  `hardware` extra — import stays hardware-free):
  - **Reader thread** (`kv4p-reader`, daemon; the `MultimonStream` idiom): `read` → `KissDecoder.feed`
    → `parse_frame` → dispatch. `RX_AUDIO` → bounded drop-oldest `deque` (drops counted,
    `rx_audio_drops`); `DEVICE_STATE` → latest + `applied_sequence`; `HELLO` → adopt
    windowSize/module/freq range; `WINDOW_UPDATE` → credits; `DEBUG_*` → `logging` at the matching
    level (TRACE→debug); a KISS **DATA** frame → inert `Ax25Frame`, **separate path, never a vendor
    sink**. A read error (SerialException et al.) is **surfaced** (stored + re-raised to blocked
    writers/waiters), not wedged; a malformed frame is logged and skipped without killing the reader.
  - **Flow control counts ENCODED bytes** (the cycle-1 gotcha): `build_vendor_frame` returns the
    escaped/FEND-delimited on-wire bytes, so `len(frame)` *is* the ack unit (`_encodedFrameLen`). A
    write blocks until the window has room and raises `Kv4pTimeout` rather than hanging TX; a
    `WINDOW_UPDATE` refunds the same encoded count.
  - **Reconciler:** `send_desired_state(state)` assigns the next sequence + ORs in the session flags
    (which ride every frame — the `HOST_STATE_SESSION/GLOBAL_FLAG_MASK` split); `await_applied(seq,
    timeout)` blocks on `DeviceState.appliedSequence`.
  - **Lifecycle:** `close()` idempotent + atexit; safe shutdown is a **reconciled PTT-off flag, not a
    dropped line** (there is none), bounded by a short `_CLOSE_ACK_TIMEOUT` (0.5 s) so shutdown never
    hangs on a silent device; fail-safe if the port is already gone.
- **ADR 0062** (`docs/adr/0062-kv4p-transport-handshake.md`, index row added) records the two real
  decisions, both firmware facts read from `kv4p_ht_esp32_wroom_32.ino` (not memory):
  - **Decision 1 — connect by syncing `DeviceState.appliedSequence`, never by waiting for a HELLO.**
    USB HELLO fires once at boot (no connect event; `connected` hardcoded true), and `sequence` is
    RAM-only/monotonic-within-a-boot — so a restarted host counting from 1 is **silently ignored**.
    `connect()` sends a probe with `ENABLE_STATUS_REPORTS` (firmware applies session flags + pushes
    DeviceState *before* the sequence check), reads `appliedSequence`, sets the counter to
    `applied + 1`. HELLO is a bonus, never a precondition; else windowSize defaults to
    `USB_BUFFER_SIZE = 2048` (**verify-on-bench**).
  - **Decision 2 — hold DTR/RTS low before `open()`** (ESP32 auto-reset footgun; the aioc shape, for
    a different reason). Deliberately **do not** reset-to-get-a-HELLO (would reboot the radio every
    restart; the appliedSequence sync makes it needless). Whether pyserial's default resets this
    board is **verify-on-bench**.
- **Tests:** `tests/test_kv4p_transport.py` — 15 fake-serial tests (a `FakeSerial` feed/writes pipe +
  background threads for the blocking calls): appliedSequence sync with/without HELLO, sequence never
  regressing below applied, encoded-byte accounting (block-at-zero / resume-on-`WINDOW_UPDATE` /
  timeout, driven with a FEND-heavy payload so encoded >> decoded), dispatch routing, DATA-frame
  inertness, reader survival across a chunk boundary / `b""` read / a surfaced serial error, and the
  reset-safe factory (lines low before open). Suite **901 passed, 5 skipped** (886 baseline + 15).

**Verify-on-bench (guardrail 1, recorded not asserted):** windowSize 2048; whether pyserial's default
open resets this board; the real serial path/name (`/dev/ttyUSB*` for CP210x/CH340, not the AIOC's
`/dev/ttyACM*`). **Throughput budget (open measurement, not a problem):** ~64 ADPCM blocks/sec through
cycle 2's pure-Python codec on the reader thread, ~89 kbit/s ≈ 77% of the 115200 line — the reader
must not stall; measured in the composed backend, not here.

**NEXT CYCLE:** the `Kv4pHt` backend class — implement the `Radio`/`CatRadio` surface on top of
transport + `audio.py` (`transmit`/`receive`/`ptt`/`status` + `set_frequency`/`set_channel`/`set_tone`
via a pending `HostDesiredState`), then factory/config/`app.py` wiring. The `Capability.SCAN`
advertise-or-omit question and the `audio.squelch="cat"` relax (`app.py:1276-1286`) land there (ADR 0061).

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR instead
of an issue comment/label.

## kv4p HT backend — the audio edge (ADPCM codec + resamplers + TX re-blocking, 2026-07-17)

Second frame-layer cycle for the kv4p backend (ADR 0061; cycle 1 = `frames.py`, PR #110 merged).
This is the **audio edge**, still pure and hardware-free: no serial, no flow control, no `Kv4pHt`
class, no wiring. **No new ADR** — nothing here decides anything 0061 didn't cover.

- **`radio_server/backends/kv4p/audio.py`** (stdlib + numpy + soxr, all core deps — safe with no
  extras):
  - **IMA ADPCM WAV-block codec both directions.** `decode_adpcm_block(128B) -> 249 int16`
    (self-contained: header seeds predictor+index), `encode_adpcm_block(249, index) -> (128B,
    next_index)`, and `AdpcmEncoder` carrying the step index across blocks. Block = 4-byte header
    (`int16 LE predictor` = sample 0 verbatim, `uint8 index`, `uint8 reserved=0`) + 124 data bytes
    = 248 nibbles **low-nibble-first**; 1+248=249. Per-sample loop is **pure Python ints** (the
    predictor feedback is sequential, not vectorizable; int16 numpy would wrap). **Codec choice
    (documented):** predictor re-anchored to the true first sample each block (bounds drift, exact
    sample-0) while the index is carried (avoids per-block reset artifacts); decode stays
    self-contained because the header carries both.
  - **Streaming 16k↔48k resamplers** (`StreamResampler`) over `soxr.ResampleStream(..., dtype=
    "float32", quality="HQ")` + `resample_chunk` — the `GoertzelStream` precedent (`audio/dtmf.py:
    682`), **not** `audio/resample.py`'s VHQ one-shot (its ~150 ms buffering is the latency trap
    ADR 0054 caught; this is a live full-duplex path). `resample.py` untouched. `flush()` (soxr
    `last=True`) drains the filter tail.
  - **TX re-blocking** (`TxAudioEncoder.push(frame) -> list[128B blocks]`): 48k → 48k→16k resample
    → accumulate → emit whole 249-sample-at-16k blocks, **hold the remainder** (`pending_samples`).
    **RX** (`RxAudioDecoder.push(128B) -> AudioFrame@48k`): decode → 16k→48k resample → one
    canonical frame per block, **no re-blocker** (AudioFrame is format-identity-only, no length
    contract).
- **Empirical (guardrail 1, measured this cycle):** soxr HQ streaming has real filter latency — a
  single 249-chunk emits 0 samples; cumulative output converges to exactly the rate ratio only
  after `flush(last=True)` (16→48 == 3×, 48→16 == ÷3, both exact when flushed). Chunked feeding ==
  one big call (bit-identical). ADPCM round-trip SNR on a 440 Hz sine ≈ **30.5 dB**, step index
  stayed in [56,67] (never runs away). Tests assert an SNR floor of 24 dB and cumulative ratios.
- **Tests:** `tests/test_kv4p_audio.py` — 13 pure tests incl. a hand-worked decode fixture
  (nibbles `[4,4,8,0]` → samples `[0,7,17,16,17]`, derivation in a comment). Suite **886 passed, 5
  skipped** (873 baseline + 13).

**Verify-on-hardware (bench, recorded not asserted):** real ADPCM fidelity against the device's own
pschatzmann-based codec — byte-for-byte block compatibility and audible quality. Our codec follows
the standard IMA WAV spec; the firmware tests only expose the 128/249/747 sizing, not the nibble
tables. **Open from cycle 1, still open:** DTMF through lossy 16k ADPCM has never met the native
Goertzel gauntlet (talk-off / weak-signal).

**NEXT CYCLE:** the reader/writer over pyserial + the reconciler state machine, wiring `frames.py`
+ `audio.py` into a `Kv4pHt` backend (flow control counts *encoded* bytes; the `Capability.SCAN`
question and the `audio.squelch="cat"` relax from ADR 0061 land there).

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR
instead of an issue comment/label.

## kv4p HT backend — ADR 0061 + the pure wire codec (frame layer only, 2026-07-17)

New backend *shape* recorded and its I/O-free wire codec landed. The kv4p HT is not a sound card
+ serial PTT like the AIOC: it is a CP210x/CH340 **UART at 115200 8N1** over which everything
rides — RX/TX audio, tuning, PTT, squelch — in **KISS frames**. No sounddevice, no Hamlib. This
cycle is the frame/struct layer ONLY (no serial I/O, no audio codec, no backend class, no wiring).

- **ADR 0061** (`docs/adr/0061-kv4p-uart-backend.md`, index row added) records three things that
  make it a new shape: (1) it's a **state reconciler** — the host sends a whole `HostDesiredState`
  with a monotonic `sequence`, the firmware echoes `DeviceState.appliedSequence`; **PTT is a flag
  inside that struct** (`HOST_STATE_PTT_REQUESTED`), so guardrail 2 holds trivially (no command to
  misuse). (2) It'd be our **first real `CatRadio`** (only `MockRadio` implements CAT today;
  `SignaLinkV71` is a `NotImplementedError` stub). (3) It has a **real busy line**
  (`DEVICE_STATE_SQUELCHED` + RSSI), so `audio.squelch = "cat"` — which `api/app.py:1276-1286`
  rejects for `baofeng` — becomes valid for this backend.
- **`radio_server/backends/kv4p/frames.py`** (stdlib only, no I/O): streaming `KissDecoder`
  (mirrors the firmware parser `protocol.h:392-515` — boot-banner discard, unknown-escape resync,
  oversize-drop-not-truncate); vendor envelope `FEND|0x06|"KV4P"|0x01|<cmd>|payload|FEND` with
  port-nibble drop and bad-prefix/version ignore; KISS DATA (`0x00`) parsed as a SEPARATE
  `Ax25Frame` path (future text-over-RF, inert this cycle); frozen-dataclass struct codecs for
  `HostDesiredState`(22)/`DeviceState`(26)/`Version`(17)/`Hello`(43)/`WindowUpdate`(4) using
  `struct` `<` (`calcsize` asserted in tests); `HostStateFlag`/`DeviceStateFlag` IntFlags,
  `RcvCommand`/`SndCommand`/`DeviceMode`/`DeviceStateError`/`RfModuleType`/`FeatureFlag` enums, and
  the `HOST_STATE_SESSION/GLOBAL_FLAG_MASK` split carried for the next cycle.
- **Source of truth:** kv4p-ht pinned at `e9935bd37e7505f70ae7023c78fe6a714be90be9`
  (`protocol.h` + `globals.h`), read as a spec — **not ported** (kv4p-ht is GPL-3.0; independent
  impl, cited in the ADR, no firmware source pasted). `RfModuleType` is `uint8_t` (fixes `Version`
  at 17 bytes); ESP32 Xtensa `char` is signed (signed-byte codes for `radioModuleStatus`).
- **Tests:** `tests/test_kv4p_frames.py` — 28 pure tests. Suite **873 passed, 5 skipped**
  (`uv run pytest`), 845 baseline + 28.

**NEXT CYCLE (the backend, recorded in the ADR — none built here):** the reader/writer over
pyserial + a reconciler state machine; flow control counts **encoded** bytes (the firmware acks
each frame with its escaped/FEND-inclusive length, `protocol.h:421-431`), not decoded payload;
audio is 16 kHz 4-bit IMA ADPCM, 128-byte block → **249 samples**, and 249 does not divide our
960-sample canonical blocks (ADPCM + resampling live in that cycle); ≈89 kbit/s ≈ 77% wire use at
115200. **Open question:** whether to advertise `Capability.SCAN` — the kv4p has no hardware scan,
but `ScanEngine.__init__` (`scan/engine.py:199-200`) requires `SCAN` to run its software sweep.
Also relax the `audio.squelch="cat"` rejection (`app.py:1276-1286`) for this backend.

**No GitHub instruction issue this cycle** — `gh issue list` has no target, mirroring the
precedent below; recorded in the PR instead of an issue comment/label.

## Fix four beginner-facing doc bugs (docs-only, no ADR, 2026-07-17)

Four verified defects a beginner hits following the docs to bring up a real radio. No behaviour change,
no code — docs only.

- **BUG 1 — the required Piper voice had no source anywhere in the repo.** `tts.voice` is required with
  no default, yet nothing said where to get a voice or that it's *two* files. Added a **Getting a
  voice** section to `docs/install.md` (voices page + samples + VOICES.md, `en_US-amy-medium` as the
  default, both `.onnx` and `.onnx.json` download links, the sidecar-must-sit-beside-it warning, and
  why medium over high on ~3 kHz FM) and expanded the "Voice file" bullet in `docs/configuration.md`
  with the same essentials + a cross-link. All five URLs verified HTTP 200. Sidecar claim verified
  against `radio_server/services/tts.py:99-103,142-159` (fails loud without `<voice>.onnx.json`, reads
  `audio.sample_rate` from it).
- **BUG 2 — stale "pause between repeated digits" advice removed.** Bench-confirmed false since 0060
  (native does its own onset/gap detection). Dropped the pause clause in `docs/using-it.md` (kept the
  "hold each tone ~1s" tip) and removed the "Held keys count once …" blockquote + a dangling
  held-vs-repeated sentence in `docs/hardware-bringup.md`. The `pause`/`repeated` grep hits that remain
  are all in **historical ADRs (0030/0038)** — left intact on purpose; they accurately record the old
  buffered behaviour, and rewriting them would falsify the record.
- **BUG 3 — DTMF spelling normalized to unspaced** (`10#`/`01#`/`02#`/`98#`/`99#`, matching the Services
  card's `{digit}#` and `radio.toml`) across README.md and docs/. Also unspaced the 6-digit TOTP
  examples (`1 2 3 4 5 6 #` → `123456#`) for one consistent spelling. `grep -rnE '[0-9]( +[0-9#])+ *#'`
  over README + docs is now empty.
- **BUG 4 — Homebrew introduced before first use** in `docs/install.md`'s macOS section (what it is +
  brew.sh + the Xcode CLT it pulls in), so `brew install portaudio` no longer appears from nowhere.
- **Suite: 845 pass, 5 skipped** (`uv run pytest`, unchanged — docs-only).

## Flip `auto` to `native`; multimon-ng becomes optional (ADR 0060, 2026-07-17)

The bench A/B ADR 0055 deferred is settled: on the reference station (AIOC + UV-5R) `native` decodes
better than multimon-ng on real RF. So this cycle makes the one-line flip 0055 named and drops
multimon-ng as a dependency.

- **The flip — `resolve_decode_mode` (`radio_server/audio/dtmf.py`)** loses its `shutil.which` branch:
  `auto` → `(native, "bench-verified, ADR 0060")` unconditionally, binary present or absent. `multimon_bin`
  stays in the signature (call-site stability + explicit modes still read it). Four description sites
  updated in lockstep so nothing lies: the `DECODE_MODES` and `DEFAULT_DTMF_DECODE_MODE` comments in
  `dtmf.py`, the `build_controller` comment (`controller/engine.py:738-748`), the `dtmf.decode_mode`
  help in `config/spec.py`.
- **`streaming`/`buffered` unchanged and still raise.** An explicit mode is a contract — the
  raise-on-missing-binary in `MultimonStream`/`MultimonDtmfDecoder` is untouched. The flip is confined
  to `auto` (the only mode whose job was to choose). `doctor` now prints `decode mode: auto -> native
  (bench-verified, ADR 0060)`.
- **Tests — `tests/test_auto_decode.py` rewritten** to the flipped contract: `auto` → native with the
  binary present AND absent (parametrized), auto wires `GoertzelStream` regardless, auto never raises
  with no binary. Kept verbatim: explicit-mode pass-through and `test_explicit_streaming_without_binary_still_raises`.
  Doctor test asserts the new reason for both present/absent. No `skipif`. Grep confirmed the old
  reason strings (`multimon-ng found` / `no multimon-ng on PATH`) lived only in this file.
- **Docs — multimon-ng optional + Opus collapse (user-approved).** `docs/install.md` extras table is
  now exactly **PortAudio + a voice**; apt → `libportaudio2`, brew → `portaudio` (dropped multimon-ng,
  libopus0, opus); Windows section drops the "no Windows build → WSL2 for DTMF" story (native decodes
  in-process on Windows). `docs/hardware-bringup.md` reframes the DTMF-test section native-first
  (multimon only for the streaming/buffered escape hatches). `scripts/install.ps1:11` softened.
  `radio.toml.example` keeps `multimon_bin`, rewords `decode_mode`/`buffer_seconds`.
  `docs/configuration.md:209` dropped the stale "system libopus0" clause (opus rides the `mumble` wheel).
- **Open item, recorded in the ADR, NOT acted on:** the bench proved *decode*, not *talk-off*. The lever
  is `NATIVE_ONSET_BLOCKS = 1` (Q.24 wants ≥2 blocks / ≥40 ms; pinned by ADR 0038's "two 9s @ 30 ms gap
  → 99" row). Quiet failure mode: a spurious combo fires, and since `98#` is ungated (ADR 0043) the
  visible symptom is a Mumble link dropping on its own. Left pinned this cycle.
- **Suite: 845 pass, 5 skipped** (`uv run pytest`).

## Removed services get a home + the two migrations that took the station down now say what they are (ADR 0059, 2026-07-17)

ADR 0051/0052 made three breaking `radio.toml` changes and shipped a migration error for none of them;
a deployment hit all three, in sequence, on the first restart after an upgrade. This cycle gives the
removed features a home and names the errors — the `_LEGACY_MUMBLE_KEYS` habit, extended.

- **Part 1 — the five services ship as `examples/local_services/`** (weather, astronomy, quote, battery,
  bible), already ported (absolute imports, `settings.extra(...)`, astro's bare `from weather_service
  import …`). **Not** registered in `PLUGINS`, **not** imported by the app — copy-source only. Upgrade
  path is now `cp examples/local_services/*.py local_services/`. `.gitignore`'s `/local_services/` is
  anchored, so the examples commit while the operator's folder stays ignored. Fixed the one residual
  dangling `from .plugin import PluginBuildContext` → absolute in each.
- **Part 1 test — `tests/test_examples_local_services.py`** imports every example through the real
  `discover_local_plugins` and asserts a valid `PLUGIN`. This is the load-bearing unit that catches
  `Fetcher`/`ServiceContext`/`Service`/`ServicePlugin` drift in CI. The deleted per-service tests were
  **not** restored (deliberate — one import test carries it). Note the bare-stem `sys.modules` cache
  gotcha: the test pops/restores the five stems + puts the examples dir first on `sys.path`, so it's
  deterministic even though the dev box's gitignored root `local_services/` shares those stems.
- **Part 2a — `resolve_settings` (settings.py:127)** now splits unknown keys by namespace: a table
  that isn't a schema group (`weather.base_url`) → `"unknown config table(s): [weather] (weather.base_url)
  -> [plugins.weather] … only the TOML nesting moves. See examples/local_services/."`; a real typo whose
  namespace IS a group (`server.prot`) keeps the generic `"not in the config schema"`. Namespaces derived
  from `{s.key.split(".",1)[0] for s in SETTINGS}` — no constant. `_LEGACY_MUMBLE_KEYS` (raised earlier in
  `_flatten`) is untouched; `mumble.enabled` never reaches the new check.
- **Part 2b — `resolve_bindings` (plugin.py:152)** keeps the `"unknown service or command; known ids
  are […]"` prefix (tests match it) and appends: ids come from `./local_services/`; if the id is one of
  the five 0051 removals (`_REMOVED_IN_0051`), names the example file to copy; if the folder is absent,
  says so. `DEFAULT_LOCAL_SERVICES_DIR` is lazy-imported from `.local` inside the function (avoids the
  `local`↔`plugin` cycle).
- **Part 3 — docs/configuration.md** "Add your own services": wrong-vs-right TOML (`[weather]` fails loud
  vs `[plugins.weather]`), notes the plugin code is unchanged, points at `examples/local_services/`.
- **Scope held:** no per-service tests restored, no new plugin features, no digit remap, no `[services]`
  default change. Examples not registered, app doesn't import `examples/`. `PLUGINS` still `("time",)`.

Suite 846 pass, 5 skipped. PR against master; human merges.

## Copy-pasteable commands actually run + a docs↔script contract test (ADR 0058, 2026-07-17)

Narrow, unblocked slice (NOT the hardware-gated install.md/WSL2 rewrite): commands the docs tell people
to type that failed when typed. Three bugs + the reason they kept regressing (nothing tested it).

- **Bug 1 — `curl … | sh` died on Debian/Ubuntu.** install.sh was `#!/usr/bin/env bash` + `set -euo
  pipefail`, but README/getting-started pipe to `sh` = dash, which lacks `pipefail` (fatal, instant).
  **Decision (ADR 0058): POSIX-clean the script**, not `| sh`→`| bash` — the audit found it ~99% POSIX
  already (only `pipefail`, the `curl|tar` pipe, and two dash-supported `local`s). shebang→`#!/bin/sh`,
  `set -euo pipefail`→`set -eu`, and the line-93 `curl | tar` pipe → temp-file download with an explicit
  curl status check (more robust than the masked pipe). README/getting-started keep `| sh` (now correct).
  **Honest repro note:** couldn't reproduce locally — this box's dash is 0.5.12, which *added* pipefail
  (2023); the bug bites dash ≤0.5.11 = Ubuntu 22.04 LTS / Debian 11 (huge base, supported to 2027).
- **Bug 2 — `--with-hardware` advice couldn't work.** install.md:70's "add the flag to the `curl … | sh`
  line" → `sh --with-hardware` = "Illegal option". Fixed to `curl … | sh -s -- --with-hardware` and
  `./scripts/install.sh --with-hardware`.
- **Bug 3 — bare `python -m radio_server`.** Swept to `uv run python` across all user-facing guides
  (the 8 flagged copy-paste blocks + inline mentions in hardware-bringup/deployment/architecture/
  operating/configuration). ADRs left as frozen history (excluded by decision).
- **THE anti-regression: `tests/test_docs_install_command.py`** (4 tests, no skipif) — parses README's
  pipe target, asserts it agrees with install.sh's shebang, **executes** `sh scripts/install.sh --help`
  to prove it starts (not just `-n`), and statically forbids `pipefail` (this box's dash tolerates it,
  so execution alone can't guard reintroduction). Proven to fail on a reverted shebang/pipefail. Suite
  838 pass.
- **Still the docs cycle (out of scope, unchanged):** install.md WSL2 rewrite, prose, Piper voice-link,
  hardware-bringup split.

## Installer ships the Mumble link on all three platforms (ADR 0057, 2026-07-17)

Made the README headline command actually deliver: install on a clean box, open the panel, click
Connect, talk. Both installers ran a bare `uv sync` (no `pymumble`) then printed "All set." — a lie.
Fixed this cycle (branches ADR 0056 → 0057):

- **libopus is now a dependency via a bundled-wheel carrier, all platforms.** Re-asked 0056's gate the
  right way: not "is opuslib-next-bundled a drop-in for `opuslib`?" (no) but "does it carry a libopus
  binary we can point the shim at?" — **yes**, verified end-to-end on Linux. Full wheel tag matrix
  confirmed: win_amd64, macOS x86_64+arm64, manylinux2014 x86_64+aarch64 (so **Pi and Apple Silicon are
  covered**). `radio_server/link/_opus.py` `ensure_opus_loadable()` is now one code path: locate
  `opuslib_next/_native/libopus.*` via `find_spec` (no bindings import) and **patch
  `ctypes.util.find_library('opus')`** to return it (delegating every other name). The vendored
  `radio_server/_vendor/` DLL is **retired** — the win wheel's opus.dll is byte-identical (both sha256
  `d553adca…`, proven in the ADR).
- **Carrier gated by a PEP 508 env marker** to exactly the five wheel tags, so no-wheel tags (win-arm64,
  32-bit) omit it and hit the system-lib hint instead of hard-failing `uv sync` on an sdist build.
  Residual edge: musl/Alpine can't be marker-excluded (non-target; Pi OS is glibc).
- **`--extra mumble` is the default sync** in both installers (browser voice link needs no radio =
  headline). **"All set." is earned:** each installer runs a `python -c "…check_mumble_importable()…"`
  that imports pymumble + libopus and won't claim the link works if it doesn't. `getting-started.md`
  Step 2 gained `--extra mumble` so the hand path matches the one-liner.
- **VERIFY ON HARDWARE:** real Windows amd64 box (git-less `uv sync --extra mumble` → the `python -c`
  check exits 0 → `doctor --link` passes) and **macOS arm64** (CI can't run it; mechanism identical to
  the verified Linux path). `install.ps1` wasn't pwsh-parse-checked here (no pwsh) — eyeball on Windows.
- **NEXT CYCLE (unblocked, out of scope here):** the `docs/install.md` rewrite — its extras table can
  collapse to PortAudio + a voice (multimon optional since 0055, opus now a dep) and drop the
  Windows→WSL2 framing for the browser link. Gated on hardware verification. Tests:
  `tests/test_opus_loader.py` rewritten for the carrier (15, no skipif). Suite 834 pass.

## Link audio fixes + web session-open + restart button (ADR 0045/0046/0047, 2026-07-16)

Two field bugs and two features in one cycle:

- **Mumble→RF never keyed (ADR 0045).** Root cause: the bridge defers to `rx_pump.active`, and
  under the deployment's `squelch = "off"` the pass-through gate never rejects a frame, so
  `active` latched `True` at the first hardware frame — every Mumble frame silently dropped.
  Gates now carry `detects_signal` (`False` on pass-through) and the pump never asserts `active`
  off a signal-blind gate. **Field-verify on the box**: watch `GET /link/status` → the active
  entry's new `tx` counter block (`frames_in` / `dropped_rx_active` / `dropped_slot_busy` /
  `overs_keyed`) while a Mumble peer talks — `overs_keyed` should climb and the radio key. If
  `dropped_rx_active` climbs instead, the deployment is on a VAD gate whose thresholds hold the
  channel busy.
- **DTMF tones leaked into Mumble (ADR 0045).** The bridge's RF→Mumble feed now runs a 0.3 s
  delay line (`DEFAULT_DTMF_MUTE_DELAY`, marked verify-against-hardware) and a decoded digit —
  surfaced via the new `Controller.on_digit` → shared `DtmfMuteGate` — retroactively condemns
  the buffered tone, then holds mute `mumble.dtmf_mute_hold` (1.0 s, re-armed per digit). New
  settings: `mumble.dtmf_mute` (default on), `mumble.dtmf_mute_hold` (advanced). Browser
  listeners/recordings still carry tones (deliberate; possible follow-up). **Field-verify**:
  dial digits from an HT with a Mumble listener attached — a leading blip means bump the delay
  constant to 0.4.
- **The OTA-code chip is now a button (ADR 0046).** `POST /auth/session` →
  `Controller.open_session()`: same on-air effect as a DTMF login (welcome over, ID armed,
  `session` events) but NO TOTP burn — the LAN token is the credential (the `trigger()` posture),
  so an RF caller's code stays valid. Repeat click = keep-alive. The chip lights green while the
  session is open.
- **Restart from the settings screen (ADR 0047).** `POST /server/restart` runs
  `server.restart_command` (default `systemctl --user --no-block restart radio-server`, matching
  `restart-radio-server.sh`; empty disables), spawn delayed 0.3 s so the reply beats the stop.
  `GET /settings` gained `restart_available`; the UI shows a two-step-confirm Restart button in
  the intro card and the post-save banner. Bench servers (no unit): set the command empty or
  ignore the button's 503. Dev proxy: `/server` added to vite's REST_PATHS.

## Retro-ham visual refresh of the web UI, Day/Night themes (ADR 0044, 2026-07-16)

The operator delivered a design handoff (`design_handoff_visual_refresh/`, local-only — not
committed) and the whole `web/` UI was re-skinned to the banner's warm retro-ham brand:
CSS-custom-property token set (Day on `body`, Night on `body[data-theme="night"]`, toggled from
the masthead and persisted in `localStorage["radio.theme"]`), masthead with a segmented
Control/Settings pill + LCD-style OTA-code chip (countdown bar), a "radio face" hero (state lamp,
frequency LCD + live dial scale on CAT radios, Monitor/Transmit sub-panels with LED-segment
meters), typed badges in the operating log, settings groups as collapsible cards with a floating
save bar, and a redesigned login gate reusing the banner radio SVG. IBM Plex Mono is vendored via
`@fontsource/ibm-plex-mono` (no CDN — LAN may be offline). **Zero functional changes**: all
handlers, hooks, capability gates, polling, and dirty-tracking are untouched (verified by diff
audit; PTT pointer-capture block is byte-identical). Layout moves: the state pill left the Status
card for the face; frequency/mode read out on the face LCD instead of status rows. New rule for
future UI work: text on amber gradients is literal `#3a1d0b`, never `var(--ink)`. Screenshots
under `docs/screenshots/`. Dev nicety: `/services` joined the vite dev proxy (it was missing —
the Services card 500'd only under `npm run dev`). Known dev-only quirk (pre-existing): under
React StrictMode's double-mount the TOTP chip's first fetch is discarded and the chip stays
hidden in `npm run dev`; production builds are unaffected.

## `update-radio-server.sh`: updates no longer strip the extras (2026-07-16)

Second field report: after updating the LAN box the Mumble link failed again with the
"needs the 'mumble' extra" 503 — **not a regression** (the new PR #82 message wording on screen
proved the new code was running). Root cause: `uv sync` is exact, so an update flow of
`git pull && uv sync && restart` *uninstalls* the extras installed at setup; the link worked
until the very next update. Bench-verified nuance (uv 0.11): `uv run` — the systemd launcher —
does an **inexact** implicit sync (`--exact` is opt-in), so service restarts never strip
anything; only an explicit bare `uv sync` does. Fix: checked-in `update-radio-server.sh`
(pull → sync naming all three extras → web build → restart) + a "Updating the server" section in
docs/deployment.md. If another extra is ever adopted on the box, it must be added to the script's
sync line.

## Link-off is un-gated over RF (ADR 0043); OTA login code moved into the header (2026-07-16)

Operator request after living with the link: the session times out while listening to a net, and
dropping the link then required a full re-login. **The disconnect combo (`73#`) now bypasses the
TOTP gate** — `Controller.step` intercepts `_link_off_digits` entries *before* `AuthGate.on_dtmf`
and runs the existing `_run_command` link-off branch (on_link(None) + spoken confirmation,
ID-prepended when due + `link` event), appending a plain `COMMAND` outcome. Deliberate
consequences (all in ADR 0043): connect combos stay gated (they enable TX); anyone on frequency
can key 73# (accepted — de-escalation only); the session is untouched (no activity stamp, no
TOTP burn — a disconnect never extends a session); empty `_link_off_digits` (no entries) means
no carve-out. `AuthGate` itself is unchanged. Web change: **TotpCard is now a compact chip in
the topbar** (visible on both Control and Settings views) instead of a card at the bottom of the
control column; fetch/countdown logic untouched.

## Install docs cover the mumble extra; extra hints say `uv sync`, not pip (2026-07-16)

Field report from the operator's LAN deployment: Connect on the Mumble Link card returned the
PR #79 503 ("needs the 'mumble' extra") because the box never had pymumble installed — and
`docs/install.md` never mentioned the `mumble` extra at all. Worse, it prescribed `uv sync
--extra hardware` **then** `uv sync --extra tts` as two commands; `uv sync` is exact by default,
so the second silently uninstalls the first extra. Fixed: install.md now shows one combined
`uv sync --extra hardware --extra tts --extra mumble` with the exactness caveat spelled out,
`libopus0` joined the apt line (`opus` on the brew line), hardware-bringup.md's lone
`--extra hardware` step carries the same caveat, and configuration.md's link-install hint
switched from `pip install '.[mumble]'` to the uv phrasing. The in-app hints
(`link/pymumble_client.py::_EXTRA_MSG`, `doctor.py` `--link` fail) now say `uv sync --extra
mumble` too — the deployment is a uv-managed source checkout, so the old
`pip install 'radio-server[mumble]'` hint didn't work as written. **Known leftover:** the
hardware/tts/qrcode hints (`backends/aioc_baofeng.py`, `services/tts.py`, `enroll.py`,
`doctor.py` audio/serial checks) still use the pip phrasing — same mechanical fix if it bites.

## Mumble nick is now `<callsign> (radio-server)` — per-entry `username` removed (2026-07-16)

Operator request: the station should identify as the licensee on every Murmur, not carry a
per-entry nick. `link/entries.py::link_username(callsign)` is the single source of truth
(`"AE9S (radio-server)"`; callsign-less bench/mock deployments fall back to the bare
`"radio-server"`). `build_app` threads it into `_pymumble_client_factory` (guarded
`settings.is_set("station.callsign")`); `doctor --link` computes the same nick. The
`MumbleEntry.username` field, the settings-API serialization, the web editor's Username input,
and the example prose are all gone. A config still carrying `username =` in an entry **fails loud
with a tailored message** ("delete the line…"), not the generic unknown-field error. No
SettingSpec change — canary stays 56; `/link` entry payloads simply lose the `username` key.

**Verified:** full suite green; `npm run build` clean; live Docker Murmur
(`mumblevoip/mumble-server`, default config): the nick **`AE9S (radio-server)` — space and
parens — was accepted** by the stock server (doctor `--link` pass + connected client), so no
fallback nick was needed.

## Link announcements configurable, combos on the keypad card, TOTP code in the UI (2026-07-16)

Operator follow-ups after first live use of ADR 0042:

- **`mumble.link_announcement`** (a `{name}` template — the entry name, underscores spoken as
  spaces; validated at load by `coerce_link_announcement`) and **`mumble.link_off_announcement`**
  replace the hardcoded "Linked to <name>." / "Link off." in `build_controller`. Blank = silent
  (the `coerce_optional_str` announcement convention). **Canary 54 → 56**, example regenerated.
  (`mumble.disconnect_dtmf` already existed — the operator asked for it, nothing new needed.)
- **Link combos join the `/services` catalog** (`link:<entry>` per combo + `link-off` for the
  disconnect combo), so the web Services card lists them with the keypad and their Transmit
  buttons fire them via the trigger seam (which already ran link built-ins).
- **`GET /auth/totp`** (token-gated) returns `{code, seconds_remaining, interval}` — the current
  authenticator code, NEVER the secret; new `TotpVerifier.current_code()/seconds_remaining()/
  interval` accessors (read-only, burn intact). New **TotpCard** on the Control screen (local 1 s
  countdown, refetch per step, hidden when unenrolled). Posture note added to docs/operating.md:
  the LAN token already transmits directly, so the code display grants no new capability.
- `restart-radio-server.sh` (operator's systemd-user restart helper) checked in.

**Verified:** full suite green; `npm run build` clean; live smoke on a mock-backend scratch server
(catalog rows, custom announcement in tx_log via trigger, /auth/totp matches pyotp across a step
boundary). Vite proxy gained `/auth`.

## Multiple Mumble servers, DTMF-selectable — ADR 0042 (2026-07-16)

The single hardcoded ADR 0041 link became **N named destinations with one active link** (switch
semantics — one radio, one talker slot). New `docs/adr/0042-multi-mumble-servers.md`; one PR, six
implementation commits (config → manager → controller → API → web UI → docs).

**Config**: `[[mumble.servers]]` array-of-tables (per-entry `name` slug / `host` / `port` /
`username` / `channel` / `dtmf` / `tx_to_rf` / `autoconnect`), a separate channel outside the
SettingSpec schema exactly like `[services]` — `load_mumble_servers()` (raw) +
`link/entries.py::resolve_mumble_entries()` (validated frozen `MumbleEntry`s, fail-loud). The six
flat `mumble.*` connection specs are **removed** (a leftover block fails loud with the migration
snippet); `mumble.tx_hang` stays; new `mumble.disconnect_dtmf` (default `"73"`). **Settings canary
59 → 54.** Per-entry passwords are **dynamic secrets** `mumble_password_<name>` (file or
`RADIO_MUMBLE_PASSWORD_<NAME>` env; the legacy `mumble_password` name is gone) — `secrets.py` gained
a prefix predicate and preserves dynamic keys on rewrite.

**Server**: `link/manager.py::LinkManager` — entries + at most one live `MumbleBridge` (bridge
reused unchanged), **fresh client + bridge per connect** via injected factories, `on_change`
transition callback. `create_app` takes `mumble_entries` + `mumble_client_factory`;
`POST /link {entry?, on}` (404 unknown, 422 ambiguous bare `on:true`, still accepted with a sole
entry, 503 unconfigured — **breaking**: old body was `{on}`); `GET /link/status` → `{active,
entries: [...]}`; the `autoconnect` entry starts in the lifespan. DTMF: link combos are controller
built-ins resolved from the entry list, validated against the `[services]` keypad at build
(exact-string only), auth-gated, spoken confirmations ("linked to <name>" / "link off"), crossing
to the manager via the rebindable `controller.on_link` (task-scheduled, failure-isolated).

**Found + fixed while wiring the UI**: WS `status` frames are RadioStatus-only — **`state.link`
was never populated**, so the Cycle D card only ever rendered from its own poll. Now every manager
transition publishes `Event(type="link", data={entry, state, active, entries})` (the full block),
`useEvents` folds it, and the card seeds itself with one `GET /link/status` on mount. The card
lists every entry (state pill, host/channel/combo/peers, per-entry Connect/Disconnect); the
Settings tab gained **MumbleServersPanel** (add/remove/edit rows, whole-list PUT with atomic 400
handling, write-only per-entry password + set-indicator) over the new
`GET/PUT /settings/mumble-servers` + `POST /settings/mumble-servers/{name}/password`.
`doctor --link` takes an optional entry name (defaults to the sole/autoconnect entry).

**Verified**: `uv run pytest` — **718 passed, 5 skipped** (57 new tests across
config/entries/manager/controller/API); `npm run build` clean. **Live Docker-Murmur rig**
(mumblevoip/mumble-server): the 17 `RADIO_TEST_MURMUR`-gated pymumble tests pass; real server with
two entries — connect → switch (old link fully dropped) → rapid A→B→A stable → disconnect; 404
unknown / 422 bare `on:true` / `link` WS event carries the full block; `PUT /settings/mumble-servers`
persists (collision → atomic 400), the password endpoint lands `mumble_password_backup` in the 0600
secrets file (presence-only in GET); `doctor --link home` PASSes and the no-name ambiguous case
lists the entry names; `autoconnect = true` connects on boot; the served bundle contains the new
panels. Browser look/feel is the operator's check. Follow-ups unchanged: `mumble.bandwidth` spec,
client-cert auth; the dedicated `link` WS event follow-up is DONE (this cycle).

## Mumble link — web UI link card; ADR 0041 roadmap complete (Cycle D, 2026-07-16)

The final ADR 0041 roadmap item: a **Mumble link card** on the Control screen. New
`web/src/components/LinkPanel.jsx` (the StatusPanel + ServiceRow idioms): a state pill
(**Linked** green / **Connecting…** amber, new `.state-pill.state-warn` variant / **Off**), rows for
server/channel/peers, a muted receive-only note when `mumble.tx_to_rf` is off, and a
Connect/Disconnect toggle via `useAction` → the new `client.setLink(on)`. **Hidden entirely when the
link isn't configured** (`state.link` null — the TuneControls hide-don't-grey pattern, ADR 0037).
No new ADR: this executes ADR 0041's roadmap inside the ADR 0022/0037 UI conventions.

Plumbing: `api.js` gained `linkStatus()`/`setLink(on)` (the `POST /link` 503 maps onto the existing
`ControllerUnavailable` typed error); `LinkPanel` renders from the WS-folded `state.link` (the
`/events` `status` frames already carry the `link` block) wired in `ControlPanel` after
`StatusPanel`; `web/vite.config.js` proxies `/link` (was missing → dev-server 404).

**Deliberate trade:** there is no dedicated `link` WS event, and link connect is non-blocking — the
status snapshot published by `POST /link` usually still says `connected:false`. So while the link is
running the card **polls `GET /link/status` every 5 s** (plus once immediately, and it applies the
`POST /link` response body), preferring the fresher local snapshot until the next WS status frame.
Follow-up if the poll ever bothers anyone: emit a `link` event from the bridge on connect/disconnect
(needs a thread-safe hop — the pymumble connected callback fires on the library thread).

**Verified live** (Docker Murmur + real server + built bundle): served JS contains the card;
autostart → `connected:true`; `POST /link {on:false}` → `running:false`, `{on:true}` → reconnected;
`/status` carries the block the WS fold feeds. `npm run build` clean; `uv run pytest` unchanged
(**653 passed, 5 skipped** — no Python changes). No UI test framework exists (none added).

**ADR 0041 is now fully delivered** (A design #73, B bridge+streaming-ID #74, C pymumble client #75,
D this cycle). Remaining nice-to-haves: dedicated `link` WS event, `mumble.bandwidth` as a settings
spec, client-cert auth for registered Murmur identities.

## Mumble link — real pymumble client, live-Murmur verified (ADR 0041 Cycle C, 2026-07-16)

Implements ADR 0041's roadmap **Cycle C**: the real network client. `_build_mumble_client` no longer
raises `NotImplementedError` — `mumble.enabled=true` now builds a working `PyMumbleClient`
(`radio_server/link/pymumble_client.py`), and the whole link was **verified against a live Murmur**
(Docker `mumblevoip/mumble-server`, both 1.5.901 and 1.4.230).

**Empirical facts locked this cycle (guardrail 1 — each bench-confirmed):**
- **PyPI pymumble 1.6.1 cannot connect on Python 3.12** (`ssl.wrap_socket` removed). The azlux
  `pymumble_py3` branch fixed SSL in Nov 2023 but never released; the `mumble` extra is now **pinned
  to the branch-head git SHA `a560e60`** (needed `[tool.hatch.metadata] allow-direct-references`).
  Revisit when a >1.6.1 release lands.
- **Uncapped bandwidth = silent audio loss.** pymumble adopts the *server's* max bandwidth (Murmur
  default 558 kbps) as its Opus target → ~1.3 KB voice frames exceed Mumble's ~1 KB voice-packet
  limit → the server drops every frame with no error (confirmed on 1.4 AND 1.5: zero audio uncapped,
  clean audio capped). Fix: `set_bandwidth(96000)` **re-applied on every (re)connect** (the library
  resets it per connection) — `DEFAULT_MUMBLE_BANDWIDTH` in `pymumble_client.py`, a constructor
  param (not a settings spec yet; add one if operators need to tune it).
- **`is_ready()` blocks forever on an unreachable server** → `connect()` never calls it (the bridge
  connects on the event loop). Non-blocking connect + the `connected` callback (bandwidth cap +
  channel join, so both re-apply after auto-reconnect); `status()` polls readiness.
- **Branch-head quirk:** `sound_output` only exists when `set_receive_sound(True)` was called (and
  only after connection init) — the adapter always enables receive and guards every access.
- **The library thread is non-daemon with an uninterruptible retry sleep** — it held the process
  open at exit and raised into a dying interpreter. The adapter daemonizes it before `start()`.

**Shipped:** `PyMumbleClient` (lazy-import `_pm()` seam + injected `_pymumble` fake, the AiocBaofeng
pattern; sound-received → `on_audio` forward; connected → cap + join, missing channel survived;
guarded `send_audio`; peers = channel users minus self), `_build_mumble_client` real construction,
`doctor --link` (read-only connect check, `--host`/`--port` overrides, exit 0/1 verified both ways).

**Live verification (the "plug it in" bar, all passed):** two-client audio loop through Murmur 1.5
and 1.4 (`RADIO_TEST_MURMUR=host:port` gates the pytest version, skipped otherwise); full composed
app (`build_app`, mumble.enabled) autostarted the bridge, `GET /link/status` showed connected+peers,
mock-radio RX audio was heard by an independent pymumble listener in the channel, and a real Mumble
talker keyed the mock radio with the **byte-exact 2.22 s CW station ID leading the over** (Part 97).

**Tests:** `uv run pytest` → **653 passed, 5 skipped** (637 baseline + 16 fake-module client tests;
the 5th skip is the gated live test). `test_link_api.py`'s NotImplementedError test replaced with a
composes-`PyMumbleClient` assertion (construction is import-free, runs without the extra).

**Next (ADR 0041 roadmap):** Cycle D = web UI link card. Possible follow-ups: `mumble.bandwidth` as
a settings spec; certfile/keyfile support for registered-identity servers.

## Mumble link — bridge core + shared streaming station ID (ADR 0041 Cycle B, 2026-07-16)

Implements ADR 0041's roadmap **Cycle B**: the RF↔Mumble bridge against a mock client (no network,
no `pymumble`), plus the streaming station-ID seam it needs — which also **closes a pre-existing gap:
the browser `/audio/tx` talker transmitted un-ID'd** (only the DTMF/dispatcher path went through
`StationId`). The operator chose the full-bridge + shared-ID-fix scope.

**Streaming station ID (Part 97, guardrail 5).** New `StreamingId` in `services/station_id.py` — a
**radio-free** ID scheduler (reuses `IdEncoder`/`load_callsign`/`load_id_interval`/`load_id_mode` +
the `_due` interval logic) that *renders* ID audio on demand instead of owning a radio like
`StationId`. `TxSession` gains an optional `station_id` (a new `TxIdentifier` protocol, Protocol-here
/ concrete-elsewhere like `TxRecorder`, so the `tx -> {audio,backends}` arrow is intact): it transmits
ID into the **same keyed over** at key-up (when due), across the ≤10-min boundary, and at key-down
(due-gated so rapid short overs aren't ID'd every time). Default `station_id=None` → historical
un-ID'd behaviour, so every existing tx test is unchanged. `build_app` builds **one shared**
`StreamingId` (gated on `station.callsign` being set; CW mode needs no TTS) and passes it to BOTH the
`/audio/tx` `TxSession` and the bridge.

**The bridge is a peer, not a backend.** New `radio_server/link/`: `client.py` (`MumbleClient`
Protocol + `MockMumbleClient` + `DEFAULT_MUMBLE_*`), `bridge.py` (`MumbleBridge`). RF→Mumble = an
`AudioHub` subscriber holding a pump demand; Mumble→RF = `on_audio` (client thread) →
`loop.call_soon_threadsafe` → bounded drop-oldest queue → drain task that keys a `TxSession` sharing
the single `TxSlot` + arbiter + the shared `StreamingId`, with a hang timeout to unkey. Defers to a
live RF signal via a new `RxPump.active` property. `tx_to_rf=False` runs receive-only.

**Config/API.** New `[mumble]` group (`enabled`/`host`/`port`=64738/`username`/`channel`/`tx_to_rf`
=true/`tx_hang`) in `config/spec.py`; `mumble_password` secret in `config/secrets.py`. Token-gated
`GET /link/status` + `POST /link` (503 when unconfigured), plus a `link` block in `GET /status`.
`create_app` gained `station_id`/`mumble_client`/`mumble_tx_to_rf`/`mumble_tx_hang`/`mumble_autostart`
kwargs; the lifespan autostarts/stops the link. **Real client deferred:** `_build_mumble_client`
raises `NotImplementedError` (the SignaLinkV71 stub posture) — enabling the link fails loud until the
`pymumble` bring-up. New optional extra `mumble = ["pymumble>=1.6"]` (needs system `libopus0`).

**Tests:** `uv run pytest` → **637 passed, 4 skipped** (608 baseline + 29). New: `test_streaming_id.py`,
`test_link_bridge.py` (asyncio.run, mock client + MockRadio), `test_link_api.py`; `test_tx_audio.py`
extended (key-up/periodic/sign-off ID + a WS-level "browser talker is now ID'd" proof + un-ID'd
regression guard). Settings canary **52 → 59**; `radio.toml.example` regenerated. `make_secrets` gained
`mumble_password`. Note: `/link` routes are inline in `app.py` (next to `/controller`), not a separate
`register_link_routes` module — 2 small routes, lower surface.

**Next (ADR 0041 roadmap):** Cycle C = real `PyMumbleClient` behind the `mumble` extra (implement
`_build_mumble_client`, live-Murmur talk-through, a `doctor` link check); Cycle D = web UI link card.

## Mumble/Murmur link — design ADR only (ADR 0041, 2026-07-15)

**Ask:** the operator wants to bridge radio-server to a self-hosted **Murmur** (Mumble server) so an
RF radio and a Mumble channel share audio — impromptu-ham-net RF↔VoIP linking. This is the leaner
successor to the **reverted M17 arc** (cycles 41–58: hand-rolled Link protocol + M17 backend + mrefd
reflector + Codec2 + `/link*` routes, all rolled back to Cycle 40): Mumble reuses a mature TLS+Opus
VoIP stack with a maintained Python client instead of a bespoke protocol/reflector/vocoder.

**This cycle is design-only** (operator's choice): a single new ADR, no code. `docs/adr/0041-mumble-link.md`.

**Feasibility: high.** The seams already exist and the audio format matches exactly, so the bridge is
mostly glue:
- Canonical audio = 48 kHz/s16le/mono/20 ms (ADR 0006) == Mumble/Opus 48 k mono → **no resampling on
  the Mumble seam** (unlike the reverted Codec2 path).
- RF→Mumble: the bridge is one more `AudioHub` subscriber (`rx/hub.py`, bounded-queue drop-oldest),
  like a browser `/audio/rx` listener.
- Mumble→RF: the bridge is a TX client through `TxSession`/`TxSlot` + `RadioArbiter` (half-duplex,
  TX-priority) + the RMS activity gate — the existing key-from-external-source primitives.
- pymumble's threads bridge into asyncio via bounded thread-safe queues with drop-oldest — the
  `MultimonStream` reader/writer pattern (ADR 0038/0040); a stuck network drops audio, never blocks
  the loop.

**Key decisions in the ADR:** the bridge is a **peer, not a `Radio` backend** (new `radio_server/link/`,
not in `backends/factory.py`); a `MumbleClient` **Protocol + `MockMumbleClient`** so the whole bridge
is testable with no server (real `PyMumbleClient` is a later hardware-like bring-up cycle);
**Mumble→RF default on when linked** (operator's choice) but a `mumble.tx_to_rf=false` switch drops to
receive-only; **auto station ID (ADR 0005) must cover bridge-originated TX** (Part 97, guardrail 5);
`[mumble]` config group (ADR 0025) with the server password/cert on the **separate 0600 secrets
channel**; token-gated `GET /link/status` + `POST /link` (ADR 0011), independent of `capabilities()`;
new lazily-imported optional extra `mumble = ["pymumble_py3", ...]` needing system `libopus0`.

**Roadmap (in the ADR):** A = this design cycle; B = bridge core vs `MockMumbleClient`+`MockRadio`
(protocol, state machine, arbiter/gate/station-ID wiring, `[mumble]` config, `/link` routes, tests);
C = real `PyMumbleClient` bring-up behind the extra + `doctor` link check; D = web UI link card.

Docs-only cycle — no code touched, `uv run pytest` baseline (602 passed, 3 skipped) unchanged. Next
cycle to implement should start at roadmap Cycle B.

## Streaming DTMF decode — fixes dropped repeated digits like `99#` (ADR 0038, 2026-07-15)

**Problem:** over-the-air DTMF codes with a repeated adjacent digit (notably `99#`, logout) dropped a
digit and failed, while all-distinct codes (`01#`) never missed. Root cause was the ADR 0030
fixed-window path: it ran a fresh `multimon-ng` per ~0.5 s window and papered over window-boundary
double-counts with a lossy held-tone de-dup that also ate genuine repeats unless a fully-silent
window fell between the two presses.

**Fix (ADR 0038):** realize ADR 0030's deferred "persistent streaming multimon process". New in
`radio_server/audio/dtmf.py`: `DtmfStream` protocol, `MultimonStream` (one long-lived
`multimon-ng -a DTMF -t raw -` with a daemon reader thread → thread-safe queue, restart-on-death,
`atexit`/`close()` reaping), and `StreamingDtmfInput` (same `pump`/`flush` surface as
`BufferedDtmfInput`, **no de-dup** — multimon does its own onset/gap detection). Empirically verified
against multimon-ng 1.3.1: a held tone emits once, two presses emit twice even at a 30 ms gap.

**Toggle:** `dtmf.decode_mode` (`streaming` default | `buffered` fallback, env
`RADIO_DTMF_DECODE_MODE`, Advanced tier). `buffered` keeps the ADR 0030 path verbatim as a one-line
in-field revert (guardrail 1). Wired in `build_controller` (an injected `decoder` still forces the
buffered path, so all existing controller tests are unchanged), `Controller.close()` reaps the
process, called from the API lifespan shutdown. `doctor --dtmf` uses the same streaming path via a
shared `_drive_dtmf` loop.

Settings canary **49 → 50**; `radio.toml.example` regenerated with `dtmf.decode_mode`. `uv run pytest`
reports **602 passed, 3 skipped** (592 baseline + 9 streaming tests incl. a `skipif`-guarded
real-multimon `99#`→`"99"` regression, + 1 config case). Buffered-vs-streaming A/B confirmed: same
`99#` input yields `['9']` (old) vs `['99']` (new).

## Restore — PR #50 (web-UI simplification, ADR 0037) reinstated (2026-07-15)

After the cycles-41-58 revert (below), **PR #50 was restored on its own** — it was authored outside
the reverted arc and is wanted back. Master is now **pre-#48 plus #50, and nothing else**. No other
reverted PR was reinstated.

#50 was cherry-picked as its single work commit `41993bf` onto the reverted master; it applied cleanly
with no conflicts (it touches `web/` plus two config keys and does **not** depend on the #48/#49 ledger
work — verified: no `rx_open`/`rx_close`/`activity`/reader references). It brings the status pill
(collapsing Transmitting/Busy/Arbiter and capability-gating the CAT rows), removal of the PTT and
Controller cards, `controller.autostart` + `web.auto_listen` (both default on), hold-to-talk vs
click-to-toggle, opt-in token persistence + Log out, card reordering (Listen + Talk lead), Basic vs
Advanced settings tiers (`SettingSpec.advanced`), a `styles.css` pass, and ADR 0037. The settings
canary went **47 → 49** and `radio.toml.example` regained `controller.autostart` / `web.auto_listen`.
`uv run pytest` reports **592 passed, 3 skipped** (589 baseline + #50's 3 controller-autostart tests).
`web/dist` (gitignored) was rebuilt so the served UI matches.

**Operator note:** #50's two keys are back in `radio.toml.example`. A live `radio.toml` that predates
#50 will fall back to the defaults (`controller.autostart = true`, `web.auto_listen = true`); add them
explicitly only to override.

## Revert — cycles 41-58 rolled back (2026-07-15)

Cycles 41-58 (PRs #48–#66) were reverted wholesale, rolling the tree back to `703177e` — the commit
immediately before PR #48 (cycle 41, "persist RX activity to the event ledger") merged. The revert is
a single new commit on top of master, so it is itself undoable and rewrote no history; the reverted
work stays on GitHub, cherry-pickable, on its cycle branches. After the revert `uv run pytest` reports
**589 passed, 3 skipped**, and every tracked file is byte-identical to `703177e`.

**The real code state is Cycle 40**, described under "Current state" below. Everything the reverted
cycles built is gone from the code: the RX-activity ledger + channel-activity summary + `/activity`
panel (41-45), the whole Link/M17 arc — Link protocol, mock + M17 backends, mrefd UDP client, Codec2
seam, wire codec, inbound/outbound link audio, TX limiter, `/link*` routes, `doctor --link`
(46-58), optional over-RF TOTP, and the web-UI simplification's link/activity surfaces. The next
cycle continues from Cycle 40, not Cycle 59.

Two gitignored files git does not own were NOT reverted and may need hand-cleanup (see the revert PR
for specifics): `radio.toml` (delete any `[link]`/`[activity]` sections and `controller.require_auth`
/`controller.autostart`/`web.auto_listen` keys if present — the bench copy here had none) and
`radio-server.jsonl` (holds inert `rx_open`/`rx_close` records from cycle 41; nothing pre-#48 reads
the ledger, so removal is optional). `web/dist` (also gitignored) was rebuilt from the reverted source.

## Current state

Cycle 40 follow-up: **the built-ins (`station-id`/`logout`) are operator-assignable too.** Per review
feedback ("#4 and #99 need to be configurable too"), the two controller built-ins are no longer
reserved-digit special cases — they are ordinary entries in the same `[services]` keypad map, keyed by
stable ids `station-id` / `logout` (`BUILTIN_IDS` in `services/plugin.py`). `RESERVED_DIGITS` is gone;
`resolve_bindings` now accepts service **and** built-in ids and no longer rejects `4`/`99` (there are no
reserved digits); `build_registry` skips built-in ids (no `Service` to build). New
`builtin_digits(bindings, id)` reports which digit(s) a built-in sits on; `build_controller` derives
`id_digits`/`logout_digits` from the bindings and passes them to the `Controller`, which matches
incoming digits against those frozensets in `_run_command` (was `== PLAY_ID_DIGIT`/`LOGOUT_DIGITS`
module constants, now **removed**). The catalog's built-in entries are derived from the bindings, not
hard-appended. `DEFAULT_BINDINGS` now includes `"4":"station-id","99":"logout"` (default keypad
unchanged). A `[services]` table is the **complete** keypad: an omitted built-in is off the keypad
(auto-ID + idle timeout still run) — documented in README, ADR 0034 (amended, not superseded), and the
regenerated `radio.toml.example`. Folding both into one TOML table makes service/built-in digit
collisions impossible by construction. New tests cover remapping built-ins over the air, the old digits
going inert after a remap, omission, and `builtin_digits`. `uv run pytest` → **589 passed, 3 skipped**.
Same branch/PR (`cycle-40-pluggable-voice-services` → #44).

Cycle 40: **pluggable voice-service architecture** (ADR 0034). Formalized the existing
`ServiceRegistry`/`Service`/`ServiceContext` seam into a `ServicePlugin` contract and retrofitted all
six services (time/weather/astro/quote/battery/bible) onto it — **behavior-preserving** (every
per-service formatter/factory test unchanged; the settings canary stays 47). New
`radio_server/services/plugin.py`: `ServicePlugin` Protocol (`id`, `description`, `enabled(settings)`,
`build(ctx) -> Service`), `PluginBuildContext` (carries `Settings` + a **lazily-built, memoized shared
`Fetcher`** — reproduces ADR 0033's "one fetcher on first enabled fetch service"), the in-tree
`PLUGINS` tuple, `DEFAULT_BINDINGS`, `RESERVED_DIGITS` (`{"4","99"}`), `resolve_bindings` (fails loud on
reserved/unknown/non-DTMF), and `build_registry`. Each `*_service.py` gained a small `PLUGIN` singleton
wrapping its **unchanged** factory; the `register()` free functions were **removed** (5 helper tests +
engine updated to register via the factory / plugins). **Operator-assigned digits:** a new `[services]`
TOML table maps digit→service id — a **separate config channel** (like secrets; arbitrary digit keys
don't fit the `SettingSpec` schema). `config/settings.py` peels `[services]` off before schema
resolution (`_flatten`) and reads it via new `load_service_bindings`; `save_settings` leaves the table
intact (only rewrites schema keys); `render_example` emits a documented `[services]` block
(`radio.toml.example` regenerated). `build_controller` gained `service_bindings=None` (defaults to
`DEFAULT_BINDINGS`) and **replaced the imperative registration block** with
`build_registry(PLUGINS, resolve_bindings(...), PluginBuildContext(settings, fetcher))`; `build_app`
loads bindings via `load_service_bindings(config_path)`. New tests: `test_service_plugin.py`,
`test_service_bindings.py`; `test_services_catalog.py` gained remap/reserved/unknown cases.
`uv run pytest` → **582 passed, 3 skipped**. Verified end-to-end through the real composition root: a
remapped keypad (time→8#, weather→9#) transmits correctly; an unbound digit is a graceful miss; `4`/`99`
stay controller built-ins. **Adding an in-tree service** is now: write the module + plugin, append to
`PLUGINS`, add its default digit + scalar settings — `build_controller` is untouched. Scope is in-tree
(no pip/entry-point discovery — a Part-97/guardrail-4 trust decision left for later behind an explicit
opt-in). Branch `cycle-40-pluggable-voice-services` from freshly-pulled `origin/master` (`08143f2`), PR
against `master`.

Cycle 34: **weather (2#) + astronomy (3#) DTMF voice services** reading a LAN weather station, plus a
`/services` catalog. New **HTTP fetch seam** `radio_server/services/fetch.py`: a `Fetcher` protocol
(`fetch_json(url) -> Mapping`, mirrors `TtsEngine`), a real `UrllibFetcher(timeout)` over stdlib urllib
(**no new dependency**; the single network-dependent path, wraps every failure as `FetchError`), and a
`StubFetcher` (canned JSON) for tests. Two services mirroring `time_service`
(`radio_server/services/weather_service.py` `2#`, `astro_service.py` `3#`): pure formatters
`format_spoken_weather` (`sensors.outdoor.derived.{temperature_f, feels_like_f,
absolute_humidity_g_m3}` → *"Outdoor temperature 78 degrees. Feels like 78. Absolute humidity 8.1 grams
per cubic meter."*) and `format_spoken_astro` (`astronomy.sun.{sunrise,sunset}` +
`astronomy.moon.{phase_name,moonrise,moonset}`, ISO→local 12-hour, null moon → "not available" →
*"Sunrise 5:43 AM, sunset 8:26 PM. Moon phase New Moon. Moonrise 7:03 AM, moonset 9:10 PM."*). Each
service factory (`weather_service(base_url, fetcher)`) binds URL+fetcher at construction and catches
`FetchError`/`KeyError` → speaks an "unavailable" line (a dead station never crashes the loop; the GET
runs in the controller loop so `weather.timeout` defaults to a short **3 s**). **Config:** new `weather`
group — `weather.base_url` (`RADIO_WEATHER_URL`, optional, default `""`) and `weather.timeout` (default
3.0); settings-API canary 37→39; `radio.toml.example` regenerated; `save.py` banner. **Registration:**
`build_controller` gains an injectable `fetcher=None` and registers weather+astro **only when
`weather.base_url` is set** (else the digits are graceful misses). **`ServiceRegistry.register` gained a
`description`; `catalog() -> [{digit,name,description}]`** surfaced on `Controller.service_catalog` and
a new **`GET /services`** endpoint (token-gated; `[]` when no controller) — drives the web UI panel
(PR B) + the README table. **`uv run pytest` → 503 passed, 3 skipped** (+ test_fetch, test_weather_service,
test_astro_service, test_services_catalog). **Verified live against the real station** (192.168.1.62):
both formatters + the real `UrllibFetcher` produce the natural-language lines above. Docs: README "DTMF
voice services" table. Also set the operator's local `radio.toml`: `[time] tz="America/Denver"` (1# now
speaks 24-hour local time, was UTC) + `[weather] base_url`. Cut from freshly-pulled `origin/master`
(cycle 33 / PR #35 merged, `63cdc6d`); branch `cycle-34-weather-astro-services`, PR against `master`.
**Follow-ups queued:** PR A2 — announce on successful auth + on session timeout/de-auth (controller
voice); PR B — web UI hide-unsupported-controls + services panel; PR C — RX activity in the event log.

Cycle 33: **single capture reader — one `receive()` feeds both the browser and the DTMF controller**
(ADR 0031). Root-causes why over-RF DTMF login did **nothing** on the bench even after cycle 31: (1)
`ControllerRunner` read one ~20 ms AIOC block then slept `controller.poll` (**0.5 s**), sampling ~4% of
the audio into non-contiguous slivers that multimon can never lock; (2) `RxPump` (browser Listen) and
`ControllerRunner` were **two independent `receive()` loops on one single-open capture**, stealing each
other's blocks — Listen made it strictly worse. Both files had literally deferred "one `receive()`
feeding both `controller.step` and this pump" as a hardware decision. **Fix:** `RxPump` is now the
single reader — it reads back-to-back and, when a `controller` is set, calls `controller.step(now,
frame)` on the **raw** frame FIRST (guarded), then the gate→hub→recorder path (`radio_server/rx/pump.py`).
`build_app` no longer creates a `ControllerRunner` (class kept, retired from the live path);
`build_controller` still builds the controller. Lifecycle is **reference-counted demand** in
`create_app`: the reader runs while a `/audio/rx` listener is connected OR the controller is active —
`POST /controller {on}` and `/audio/rx` connect/disconnect each `_acquire_rx`/`_release_rx`
(`radio_server/api/app.py`); `_controller_state.running` now reports `controller_active`. `controller.poll`
is vestigial for DTMF. **Why the cycle-31 test missed it (operator's point — this WAS mockable):** it
fed a `FakeDtmfDecoder` returning whole pre-formed entries, never exercising `receive()` cadence /
real accumulation / real multimon / contention. **New `tests/test_controller_rx_e2e.py`** is the test
that would have caught it: a TOTP code rendered as **real `synth_dtmf` sliced into 20 ms blocks**
(0.5 s tone + 0.5 s silence per key) decoded by **REAL multimon** through the real `BufferedDtmfInput`
→ `session.authenticated` (fails on the old design); plus a proof that ONE `RxPump` feeds both a
`controller` and a hub subscriber from one `receive()`. **`uv run pytest` → 483 passed, 3 skipped.**
**Verified live:** the fixed server starts against the real AIOC, `POST /controller {on:true}` →
`running:true`, the reader pumps the card continuously with no errors (browser `/events` connected).
Docs: ADR 0031, `docs/hardware-bringup.md` DTMF note updated. **The last inch — a human keying a
DTMF code over RF — cannot be automated (no self-loopback on a half-duplex radio); the live decode path
is now byte-identical to `doctor --dtmf`, which already decodes real keyed tones on this hardware.**
Cut from freshly-pulled `origin/master` (cycle 32 / PR #34 merged, `0e62dfc`); branch
`cycle-33-single-rx-reader`, PR against `master`.

Cycle 32: **TOTP enroll CLI for Google Authenticator** (`python -m radio_server.enroll`) — the
companion to cycle 31: now that the live controller decodes over-RF DTMF, the operator needs an easy
way to get the TOTP secret onto their phone. Before this there was no CLI — minting meant the
authenticated REST endpoint `POST /settings/secrets/totp/enroll` or hand-running `pyotp.random_base32()`.
New **`radio_server/enroll.py`**: `enroll(secrets_path, account, *, force, out, env)` mints a fresh
secret via `rotate(path, "totp_secret")` (writes `radio-secrets.toml` `0600`), builds the `otpauth://`
URI via `TotpVerifier.provisioning_uri`, **always prints the base32 secret + URI**, and **renders a
scannable terminal QR** when the optional **`qrcode`** package is importable (soft import; falls back
to a "install the hardware extra" hint + the URI otherwise). Re-enrolling mints a NEW secret and
invalidates the phone's current one, so an existing secret is **refused without `--force`**. `env`
defaults to `os.environ` (respects an ambient `RADIO_TOTP_SECRET`); tests pass `env={}` to isolate.
`main([...])` wires argparse (`--secrets`/`--account`/`--force`). Nothing transmits or touches the
radio. **`qrcode>=7` added to the `hardware` optional extra** (kept optional — the CLI degrades
gracefully; consistent with ADR 0003's no-required-image-dep stance). Docs: `docs/hardware-bringup.md`
gained an "Enrolling Google Authenticator (DTMF login)" section (run enroll → scan QR → set callsign +
voice → restart → key `<code>#` then `1#`), README secrets section points to it. **`uv run pytest` →
480 passed, 4 skipped** (+5 `tests/test_enroll.py`: mints+persists a base32 secret at `0600` with the
URI+account shown, refuses-without-force, `--force` replaces, qrcode-absent URI fallback, qrcode-present
QR render via `importorskip`, `main` writes the named file; the 4th skip is the qrcode QR test when the
dep is absent). **Verified live:** `uv run --with qrcode python -m radio_server.enroll` mints, writes
`0600`, and renders a clean scannable QR + secret + URI. Cut from freshly-pulled `origin/master` **after
PR #33 (cycle 31) merged** (`cd788b5`); branch `cycle-32-totp-enroll-cli`, PR against `master`. NOT
stacked on cycle 31 — it was rebased onto the merged master. **Deferred:** QR is best-effort terminal
rendering (invert=True for dark terminals; the URI/secret always print as the reliable fallback).

Cycle 31: **buffer DTMF audio in the live controller** (ADR 0030) — closes the cycle-30 flagged
limitation so **over-RF TOTP auth actually decodes**. Root cause: `Controller.step` decoded one
`receive()` frame at a time (~20 ms on the AIOC), far too short for multimon to lock a tone (~40–200
ms), so keyed codes never decoded on the live server even with a secret + callsign configured. The
fix promotes the accumulate-and-dedup logic the operator already bench-proved in `doctor --dtmf` into
a shared **`BufferedDtmfInput`** (`radio_server/audio/dtmf.py`, same `pump(frame, now) -> list[str]`
surface as `DtmfInput`): it buffers frame bytes until a **`dtmf.buffer_seconds` window** (default 0.5
s, `dtmf_window_bytes`) then decodes the chunk, **de-dups held tones** (consecutive identical digits
collapsed; a **silent window resets** the run, so a genuinely-repeated key needs a brief pause),
feeds the framer, and returns completed entries; `flush(now)` drains the tail; an optional `on_digit`
hook surfaces each key for live display. **`doctor.py`'s `collect_dtmf` is refactored onto the same
core** (behavior identical — the existing collect_dtmf tests, incl. the real-multimon round-trip,
pass unchanged), so the tool and the live controller share ONE decode path. `build_controller` now
wires `BufferedDtmfInput` (window from `load_dtmf_buffer_seconds`); `Controller.step`'s
`self._dtmf.pump(...)` call is unchanged, and the station-ID/idle checks still tick every poll (only
the *decode* buffers). **`dedup` is a `build_controller` test seam** (default True for production):
the existing controller tests feed a `FakeDtmfDecoder` that returns whole pre-formed entries per
call, which would fold a code's repeated digits, so `build_ctrl` (and the event-log-wiring test) pass
`dedup=False` + a tiny `dtmf.buffer_seconds=0.02` window to keep the per-over cadence. New config
`dtmf.buffer_seconds` (spec.py, `RADIO_DTMF_BUFFER_SECONDS`, positive-float, verify-on-hardware) →
settings-API canary 36→37, `radio.toml.example` regenerated. **`uv run pytest` → 475 passed, 3
skipped** (+ new `tests/test_buffered_dtmf.py`: accumulate-until-window, cross-window framing,
held-tone dedup + silent reset, dedup-off, flush tail, real-multimon round-trip, window-bytes math;
+ `test_controller.py::test_login_accumulates_from_short_frames_over_the_buffered_loop` proving a
code arriving in ~20 ms frames authenticates via the buffered loop with dedup on). Docs:
`docs/hardware-bringup.md` "Testing DTMF decode" note rewritten (over-RF auth now decodes; pause
between repeated code digits; `dtmf.buffer_seconds` knob), ADR 0030. Cut from freshly-pulled
`origin/master` (cycle 30 / PR #32 merged, `e1e11ab`); branch `cycle-31-controller-dtmf-buffering`,
PR against `master`. **FOLLOW-UP (separate branch, not stacked):** PR B — a `python -m
radio_server.enroll` CLI to mint the TOTP secret + print the `otpauth://` URI + a terminal QR (soft
`qrcode` dep) so the operator can load Google Authenticator. **Deferred (noted in ADR 0030):** a
persistent streaming multimon process (more robust to tones split across a window boundary; the
fixed-window accumulator was chosen for simplicity and is already bench-proven — a boundary split
fails *safe*: a corrupted digit just rejects the code, never a false accept).

Cycle 30: **DTMF decode test tool** (`doctor --dtmf`) — the operator wanted to test DTMF decode on
the AIOC. Findings: `multimon-ng` (the decoder the server shells out to) wasn't installed (now is,
1.3.1), and there was no way to watch DTMF decode from the radio — the live DTMF path is gated on a
TOTP secret AND `controller.step()` decodes **one ~20 ms `receive()` frame at a time**, far too short
for multimon to lock onto a tone (needs ~40–200 ms). New **`radio_server/doctor.py --dtmf`** (read-
only, no keying): builds the AIOC backend and runs **`collect_dtmf(radio, decoder, framer, *, seconds,
chunk_bytes, clock, on_event)`** — a pure helper that **accumulates received audio into ~0.5 s chunks**
(`DEFAULT_DTMF_CHUNK_BYTES`) before each `MultimonDtmfDecoder.decode`, feeds digits to `DtmfFramer`
(`#` submits, `*` clears), and prints each digit + completed entry live. Reuses the existing
decoder/framer + the `--rx-level` scaffolding; handles multimon-missing and capture-busy with clean
messages. **Verified live (no hardware needed):** `collect_dtmf` over a `MockRadio` serving
`synth_dtmf("123#")` through REAL multimon decodes entry `123`. **`uv run pytest` → 465 passed, 3
skipped** (+4: `collect_dtmf` accumulate/frame with a fake decoder, silence, and a multimon round-trip
gated on the binary; the pre-existing `test_dtmf` real-decode test now RUNS since multimon is
installed — 4→3 skips). Docs: `docs/hardware-bringup.md` gained a "Testing DTMF decode" section
(install multimon → pytest self-test → `--dtmf` from the radio). **FLAGGED FOLLOW-UP (next cycle,
own ADR):** the live controller's per-frame DTMF decode almost certainly won't decode real over-RF
tones — fix is to buffer received audio into ~0.3–0.5 s windows (or stream a persistent multimon
process) in the controller; `--dtmf` is the tool that confirms the need. Cut from `origin/master`
(cycle 29 merged, `407958a`); branch `cycle-30-dtmf-diagnostic`, PR against `master`.

Cycle 29 (cont.): **AIOC audio-level diagnostics** — added to the same PR #31 after bench testing
showed keying works but audio doesn't audibly flow (unverified levels, guardrail 1). Root causes
confirmed in code: RX "Listen" is silent because `audio.squelch=audio` gates on a software VAD
(`vad_on_rms=500`) and the AIOC's received level (which follows the UV-5R volume knob + the card's
ALSA capture level) sits under it; TX "Talk" transmits the **computer mic** (not the radio) and the
local monitor mutes while keyed. Deliverables: **`python -m radio_server.doctor --rx-level`**
(read-only — reads `receive()` for N s, reports RMS/peak in int16+dBFS vs the VAD thresholds and
recommends `vad_on/off` values or flags "no audio arriving"; pure `measure_rx_levels(radio, seconds,
clock)` reused-`frame_rms` helper, MockRadio-testable) and **`--tx-tone`** (RF, same dummy-load
CONFIRM guard as `--key-test` — one-shot `transmit(synth_tone(...))` into a dummy load to prove TX
audio without the browser mic). Also: `web/src/useTxAudio.js` now requests the mic with
`echoCancellation/noiseSuppression/autoGainControl:false` (raw mic for radio, not call-DSP);
`docs/hardware-bringup.md` gained an "Audio levels & squelch" bring-up flow (squelch=off → alsamixer
+ UV-5R volume → `--rx-level` → set VAD → squelch=audio → `--tx-tone`). **Verified live on the
bench:** `--rx-level` reads real audio and correctly reports it as arriving-but-gated (~112 RMS vs
threshold 500); `--tx-tone`/`--key-test` refuse non-interactively (RF safety). **`uv run pytest` →
461 passed, 4 skipped** (+7: new `tests/test_doctor.py` — level summary, silence, the classify
branches, RF-refusal). Web build clean (51 modules). **AIOC bring-up COMPLETE — full talk-through
confirmed on the bench:** operator raised `alsamixer` + UV-5R volume (received signal then measured
~5675 RMS avg / 25837 peak-block via `--rx-level`), set `audio.vad_on_rms=1000`/`vad_off_rms=500`
(squelch=audio); browser **Listen** gates on real audio, `--tx-tone` was heard on a second radio, and
**Talk** (computer mic → radio) works. Also added a graceful "capture busy — stop the server" message
to `--rx-level` (the AIOC sound card is single-open; the doctor and server can't share it). The
tuned VAD values live in the operator's gitignored `radio.toml`. **AIOC/Baofeng is production-ready.**

Cycle 29 complete: **AIOC/Baofeng hardware backend bring-up** (ADR 0029) — the real `AiocBaofeng`
is implemented; it was a `NotImplementedError` stub. The AIOC cable is physically plugged in and was
**empirically confirmed** (guardrail 1): USB `1209:7388`, PTT serial `/dev/ttyACM0` (stable by-id
`usb-AIOC_All-In-One-Cable_da3441ac-if04`, group `dialout`, operator in `dialout`), ALSA card
`hw:CARD=AllInOneCable` (48 kHz-native capture+playback). **The backend** (`radio_server/backends/
aioc_baofeng.py`) is a pure DI object (Settings-free, like `MockRadio`): `sounddevice` `RawInput/
OutputStream` for TX/RX (48 kHz, no resample — bytes straight through), `pyserial` control line for
PTT. **Keying model:** `transmit()` self-keys only when the line isn't already held — a one-shot clip
(station ID / service TTS / REST `/transmit`, each one `transmit(whole_clip)` call) asserts→plays→
drains→drops; a stream (`TxSession`: `ptt(True)`…N×`transmit`…`ptt(False)`) holds the line across
frames and `transmit()` only plays (state `_keyed`). **PTT line is configurable** (`baofeng.ptt_line`,
`rts`/`dtr` enum, **default RTS — marked verify-on-hardware**). **RF-safety:** the port opens with
both lines pre-set **low** (kills the pulse-on-open footgun), `close()`+`atexit` always drop the line
(never exit keyed), and playback is `stop()`-drained before the line drops (no clipped tail).
`capabilities()`=`SHARED_CAPS` only (API 501 on CAT — guardrail 3); `status().busy` always False (no
COS line — ADR 0015 → use `audio.squelch=audio`; `build_app` **rejects** `squelch=cat` for baofeng).
**Config:** new `[baofeng]` group in `config/spec.py` (serial_port/ptt_line/input_device/
output_device/blocksize) + `save.py` banner; `radio.toml.example` regenerated. **Deps:** new
`hardware` optional extra (`pyserial`,`sounddevice`), lazily imported (CI stays hardware-free);
`sounddevice` also needs system `libportaudio2`. **Composition root** (`api/app.py`) passes the
baofeng kwargs. **New `radio_server/doctor.py`** (`python -m radio_server.doctor`): read-only pass/
fail table (enumerate the AIOC card @48 kHz, serial opens without keying, dialout access) + a guarded
**`--key-test`** (the ONLY RF path — refuses non-interactive/CI, demands typed `CONFIRM`, asserts the
line ~2 s, asks which line keyed) for the empirical RTS-vs-DTR answer. **`uv run pytest` → 452 passed,
5 skipped** (+16): new `tests/test_aioc_baofeng.py` (fake serial/audio seams — format-reject-before-
audio, one-shot self-key + drain-then-drop, streaming holds one stream across frames, ptt idempotency,
no-keying-on-construction parametrized RTS/DTR, lazy-import error, close/atexit line-drop); factory
test now builds baofeng (only `v71` still raises); settings-API canary 31→36 + asserts `ptt_line` enum
renders. 5th skip = the hardware-gated real-capture test (device present but this sandbox lacks
`libportaudio2`). **Bench-verified live this cycle:** doctor audio + serial all PASS against the
plugged-in AIOC; the sound card resolves as **`All-In-One-Cable: USB`** (sounddevice matches by
PortAudio-name substring / index, NOT a raw ALSA `hw:CARD=` string — a bare `All-In-One-Cable` is
ambiguous because PulseAudio also exposes the card; the `: USB` substring targets the raw ALSA
device) and **reads real 48 kHz audio** (the hardware-gated capture test now passes on the bench);
`--key-test` confirmed **DTR keys PTT** (RTS did not) → **default flipped RTS→DTR**. The backend also
constructs against the real `/dev/ttyACM0` holding both lines low (no keying) and closes clean.
**Only operator step left:** run `backend=baofeng`,`squelch=audio` with an API-token secret and
confirm full browser talk-through (TX keys, RX streams back, and — with TOTP+callsign+voice wired —
station ID fires). Docs: ADR 0029, `docs/hardware-bringup.md` rewritten (AIOC section real; V71
still pending), README status updated. **Deferred:** blocking `receive()` still inline on the event
loop (executor is a follow-up, fine at ~20 ms); a composition-root backend `close()` lifecycle hook
(atexit covers the safety-critical drop); `SignaLinkV71` still a stub (hardware not here). Next: the
bench acceptance above, then SignaLinkV71 when its box arrives, or recordings playback/download UI.

Cycle 28 complete: **async scan runner + `/scan/stop`** (ADR 0028), mock-only — makes scan
**stoppable**, closing the cycle-21 "Scan + live phase, no stop" gap that every HANDOFF since has
deferred. `POST /scan` used to run one **synchronous** `ScanEngine.sweep()` (blocks, no stop); it now
starts a **background async task** that steps the existing `ScanEngine.tick()` on the `scan.poll`
cadence — the async **driver** around the unchanged cycle-11 tick/sweep logic and cycle-16
arbiter/TX-suspend behavior, mirroring how `RxPump` drives `receive()`. New
**`radio_server/scan/runner.py`** holds **`ScanRunner`**: owns a single `asyncio.Task`, `start(plan)`
is a **single-scan guard** (builds the engine via an injected `engine_factory`, returns `False` if
already running), `stop()` clears its task ref **before** awaiting the cancel (RxPump discipline) and is
idempotent. It stays below the API — progress **and** the new `stopped` lifecycle event flow through the
same injected `on_event` (`_publish_scan`), so it never imports `EventHub`. **The clean-stop guarantee
is free from `tick()` being fully synchronous:** a `task.cancel()` can only land at the loop's
`await asyncio.sleep(poll)`, never mid-`tick`, so the in-progress tick always completes — no mid-tune
kill. **Stop-while-TX-suspended can't wedge** because while `arbiter.transmitting` the tick early-returns
and the loop keeps *polling* (spinning cheaply), never blocking on a resume that isn't coming. **API:**
`POST /scan` is now `async`, stays **501**-gated naming `"scan"` and **422** on an ambiguous plan, then
starts the runner and returns `{"scanning": true, "status"}` immediately; a start while running is a
**409**. New **`POST /scan/stop`** (capability-gated, idempotent) returns `{"scanning": false,
"stopped": <bool>}`. The old synchronous `held` return is **gone** (no async equivalent; `sweep()` is
retained on the engine but off the live path). `/status` gained a **`scan` block**
(`{running, frequency}`, mirroring `controller`) and `/events` carries the new `stopped` phase, so the
UI reflects running/stopped. **Lifecycle:** one `ScanRunner` in `create_app`
(`app.state.scan_runner`); the lifespan teardown `await app_.state.scan_runner.stop()` right after
`rx_pump.stop()` — a scan running at shutdown is cancelled with no leaked task. **UI (`web/src/`):**
`ScanControl.jsx` replaces the lone "Scan" button with a **Start/Stop pair** modeled on
`ControllerControl` (tracks `running` optimistically from the POST responses **and** from live `scan`
events, so a scan started/stopped elsewhere — or torn down at shutdown — is reflected; a `stopped` phase
means idle); `api.js` gained `scanStop()`. `web/dist` is gitignored (source + `package.json` committed;
`cd web && npm run build` rebuilds — verified clean, 51 modules). **`uv run pytest` → 436 passed, 4
skipped** (+10; 4 skips unchanged): new `tests/test_scan_runner.py` (async unit — background start emits
`scanning`, single-scan guard, clean stop emits `stopped` w/ no leaked task, idle-stop no-op,
stop-while-TX-suspended), and `tests/test_scan.py` endpoint tests rewritten for the async contract
(non-blocking ack, first `scanning`/`stopped` over the WS, 409 second start, 501 both endpoints on
audio-only, shutdown cancels the task, stop-while-TX-suspended). **Testing note:** a task spawned during
a request is cancelled by `TestClient` at request end unless driven as `with TestClient(app) as client:`
(one persistent loop) — the tests needing the scan to live across requests use that form. **Verified
end-to-end**: against a real bound server (uvicorn + `websockets`) the full lifecycle
(start→events→`/status` block→stop→`stopped` event→idempotent no-op→409) is green, and a headless
Chromium walkthrough confirmed Scan→(Scan disabled, Stop enabled, "Live: scanning @ …")→Stop→idle.
Docs: ADR 0028 + `docs/api.md` (async `/scan`, `/scan/stop`, the `scan` status block, the `stopped`
phase, 409). **Deferred, on purpose:** live hot-reload; Opus/compression; hardware backends (real
tune/busy timing — `scan.poll`/settle/dwell stay verify-on-hardware). Next: recordings
playback/download UI + a GET API for the JSONL ledger, or the hardware bring-up phase.

Cycle 27 complete: **Web UI — settings screen** (ADR 0027), mock-only — the browser face of the
cycle-26 endpoints and the close of the config arc. **Pure client feature; the backend is
unchanged** (`uv run pytest` stays **426 passed / 4 skipped**). The cycle-26 contract was verified
sufficient before building (endpoints + `test_settings_api.py`), so no Python edit was needed; the
standing rule (real gap → minimal backend fix + pytest) did not fire. The whole form **renders from
the schema** returned by `GET /settings` — no hardcoded field list — so a setting added to the
registry later needs zero UI change. New `web/src/components/`: **`SettingsView.jsx`** (fetches
`GET /settings`, groups fields by `group` into `.card` sections, dirty-tracks edits, Save PATCHes
**only changed keys**; on the atomic **400** it surfaces the named key inline and **keeps the
operator's edits**; on success shows a **restart-to-apply banner** off `restart_required` then
re-fetches), **`SettingsField.jsx`** (renders one setting **by `type`** — text/number/toggle/select
— with the schema **`description` always visible** as inline help, and required / required-unset
flagged), **`SecretsPanel.jsx`** (api-token + TOTP shown **present/absent only**; **Rotate API
token** reveals the new token **once** with copy + honest "active after restart; current session
still works" wording + a return-to-gate re-auth action; **Re-enroll TOTP** renders the returned
`otpauth://` URI as a **scannable QR** once via **`QrCode.jsx`**, with the URI as copyable text),
and the QR uses **`qrcode.react`** (the one new dep — zero-runtime-deps, MIT SVG). **Wiring:**
`api.js` gained four client methods (`settings`/`updateSettings`/`rotateApiToken`/`enrollTotp`);
`vite.config.js` proxies `/settings`; `ControlPanel.jsx` got a topbar **view toggle** (Control ⇄
Settings); `App.jsx` threads an `onReauth` (deliberate return-to-gate). **`web/dist` is gitignored** —
the commit carries source + `package.json`/`package-lock.json`; `cd web && npm install && npm run
build` rebuilds it (verified: 51 modules, clean). **Apply semantics: restart-to-apply (v1)** — the UI
says so on every write. **Acceptance is a browser walkthrough** against the mock server (see ADR 0027
/ the PR); the endpoint contract the UI consumes was also re-proven via a TestClient. **Deferred:**
live hot-reload; server-side scan-stop (standing, unrelated backend gap); hardware backends.

Cycle 26 complete: **Settings REST API + secret rotation** (ADR 0026), mock-only — a **thin,
token-gated HTTP surface over the cycle-25 config**, so the cycle-27 UI can read/edit settings. **No
new config logic:** endpoints serialize the `SettingSpec` registry and validate via `resolve_settings`
/ persist via `save_settings`/`save_secret`/`rotate`. New `radio_server/api/settings.py` with
`register_settings_routes(api, app)` (called from `create_app`, routes on the existing
`Depends(require_token)` router). **`GET /settings`** serializes every setting — `key, group, type
(+choices for enums), default, value, required, description` — with `type` **derived in the API
layer** (bool/enum/integer/number/string; `bool` checked before `int`; `station.id_mode` keyed off
its coercer) so `config/spec.py` stays untouched; a required-unset value serializes as `null` (no
raise); plus a `secrets` block that reports **presence only** (`{"set": bool}`) — a secret value can
never appear because secrets aren't in `SETTINGS`. **`PATCH /settings`** takes `{"values":{key:val}}`,
rejects secret + unknown keys up front, then validates the **whole** patch atomically by resolving
`{current values}|patch` (raises naming the bad key **before any write** → 400; file untouched), then
`save_settings` round-trips to `radio.toml` and updates `app.state.settings` for display; returns
`restart_required` (v1 = all changed keys). **`POST /settings/secrets/api-token/rotate`** and
**`POST /settings/secrets/totp/enroll`** are **write-only** — generate (or accept, for the API token)
a secret, `save_secret` it 0600, and return it **once** (the token in-body; the TOTP secret as an
`otpauth://` provisioning URI via `TotpVerifier.provisioning_uri`); they never read an existing
secret back. **Wiring:** the one real change was threading the config/secrets **file paths** to the
app — `build_app`/`create_app` gained `config_path`/`secrets_path` (+ the `Secrets` object) stored on
`app.state`; `--config`/`--secrets` flow through; `DEFAULT_CONFIG_PATH` moved into
`config/settings.py`. **Apply semantics: restart-to-apply (v1)** — writes persist but the running
server (the token `require_token` closes over, the scan route's startup settings) is **not**
hot-reloaded; every write response says so. `uv run pytest` **426 passed / 4 skipped** (412 + the new
`tests/test_settings_api.py`: schema+values with no secret leak, atomic-reject-naming-key with the
file byte-unchanged, unknown/secret-key rejection, token-gating, rotate persists+returns-once, enroll
fresh-URI-never-existing-secret). Docs: ADR 0026 + a `## Settings & secrets (ADR 0026)` section in
`docs/api.md`. **Deferred for cycle 27:** the web settings screen (renders off `GET /settings`, shows
a "restart to apply" banner off `restart_required`); live hot-reload; QR rendering of the URI.

Cycle 25 complete: **config foundation — a schema-driven `radio.toml` replaces the ~31 scattered
`RADIO_*` env reads** (ADR 0025), reversing the de-facto env-only decision. **Behavior-preserving
refactor:** with no config file, every default equals today's and the suite stays green (**412
passed / 4 skipped** — up from 386 because the config system added `tests/test_config.py`; five old
`load_api_token`/`load_totp_secret` env-reader tests were removed as those functions are gone). New
`radio_server/config/` package: **`spec.py`** (the `SettingSpec` registry — one source of truth for
key/default/coercion/description, 31 non-secret settings grouped into 11 TOML tables; every default
*references* the existing `DEFAULT_*` constant so there's no duplication), **`settings.py`**
(immutable `Settings` + `resolve_settings`/`load_settings` via stdlib `tomllib`), **`save.py`**
(`save_settings` round-trips via **tomlkit**, preserving hand-added comments; `render_example`
generates `radio.toml.example` from the registry), **`secrets.py`** (`load_secrets`/`save_secret`/
`rotate` — the two secrets on a separate 0600-enforced channel). **The load-bearing subtleties, all
verified against the old loaders:** empty-string handling is per-field (→default for floats, →fail
for callsign/tts, →False for record bools, →True for mock_cat); `time.tz` keeps its
`ZoneInfoNotFoundError`, the VAD `on>off` hysteresis stays a `ValueError` in `AudioLevelGate.__init__`
(a cross-field check, not in the schema); two bool coercers (strict fail-loud for `recording.*`,
permissive for `server.mock_cat`); **required-unset fails loud lazily on access** (so the default
mock app with no callsign/voice still starts — the invariant the whole refactor hinges on). Every
`load_*(env)` is now a thin `load_*(settings)` accessor; `build_app(settings, secrets)` /
`build_controller(settings, *, totp_secret=…)` thread the secret in explicitly (it is never a schema
setting). Bootstrap: `python -m radio_server --config PATH --secrets PATH` (argparse; `create_app`
gained an optional `settings=` only so the on-demand `/scan` route can read scan timing — otherwise
still the env-free DI seam). **Secrets split is the security-load-bearing part:** `RADIO_TOTP_SECRET`
/ `RADIO_API_TOKEN` are never in `radio.toml`, never in the `SETTINGS` schema, never serialized by
`save_settings` — so the future settings API/UI can't leak or clobber them. Also broke a latent
`eventlog↔api` import cycle (`eventlog/log.py`'s `Event` import is now `TYPE_CHECKING`-only) that the
new `config.spec` imports surfaced. **Apply semantics: restart-to-apply (v1)** — `save_settings`
persists but does not hot-reload; live reload is deferred on purpose. Docs swept off env vars to
`radio.toml` (README §Configuration, `docs/operating.md`, `docs/api.md`, `docs/architecture.md`,
`web/README.md`); ADRs left as historical record. **Deferred for cycles 26/27 (helpers built here):**
settings REST API + secret-rotation endpoints (26 — `save_secret`/`rotate` are built and tested),
the UI settings screen (27), and live hot-reload. `save_settings`/rotation have no endpoint yet.

Cycle 24 complete: **comprehensive documentation pass** (no ADR — docs cycle), **zero code change**.
The repo had 24 ADRs but no top-level user-facing docs (`README.md` was a 0-byte stub, `web/README.md`
was stale — it predated the cycle-22/23 audio work and still said "live audio arrives in later
cycles"). Wrote the user-facing set, every factual claim **verified against source, not memory**:
**`README.md`** (front door — the two modes, an honest mock-vs-hardware status block, the
two-auth-planes warning up top, quickstart, and a **complete 33-var `RADIO_*` env table** grouped by
concern with defaults + which 4 are fail-loud: `RADIO_API_TOKEN`/`RADIO_CALLSIGN`/`RADIO_TOTP_SECRET`/
`RADIO_TTS_VOICE`); **`docs/api.md`** (REST + WS reference — the 10 endpoints, the `501`
named-capability gate body `{"detail":{"error":...,"capability":...}}`, the three sockets with the
`{"status":"ready","format":{rate:48000,width:2,channels:1}}` handshake, the `/events` taxonomy
incl. `arbiter`/`auth`/`command`, and close codes `1008`/`1013`-with-the-accept-then-busy quirk/`1003`);
**`docs/architecture.md`** (the `Radio`/`CatRadio` protocol + capability split, the layer map, the
pure-leaf packages activity/arbiter/eventlog/recording, the duplex arbiter's TX-priority auto-resume,
and mock-first testability); **`docs/operating.md`** (Part 97 — the two auth planes, station ID
≤600s/forced/sign-off/cw|voice, the no-secrets whitelist log, security-reality, config guardrails);
**`web/README.md`** rewritten (RX/TX audio now ship, the static-mount-last serve path, the
AudioContext-gesture / mic-permission browser requirements, the dev proxy). Two deferred guides are
honest one-paragraph placeholders — **`docs/hardware-bringup.md`** and **`docs/deployment.md`** —
pointing to the pending bench bring-up; **no fabricated hardware specifics** (Hamlib model,
multimon flags, AIOC PTT line stay verify-on-hardware). ADRs are **linked, not duplicated**.
`uv run pytest` **386 passed, 4 skipped** (unchanged — proves zero behavior change); `git status`
shows only `.md` files. **Two doc/code discrepancies surfaced (flagged in PR #26 for a later cycle,
NOT fixed here):** (1) **duplicate ADR 0001** — both `0001-cycle-model.md` and
`0001-two-backend-radio-abstraction.md` exist; (2) **`api/events.py` is stale** — `EVENT_TYPES` still
lists `"busy"` (reserved/unused) and its docstring predates ADR 0019, omitting the `arbiter`/`auth`/
`command` types the app actually emits. (The suspected "web/dist committed despite gitignore" was a
non-issue — `web/dist/` is correctly gitignored/untracked, only a local build artifact.) **No
instruction issue exists in this repo** (`gh issue list` empty), so the CLAUDE.md end-of-cycle issue
comment/label step had no target — noted in the PR instead. Next: pick up either discrepancy as a
tiny code cycle, or the backend scan-stop / recordings playback-download UI deferred earlier.

Cycle 23 complete: **web UI — TX mic capture** (ADR 0024), mock-only. The browser operator can now
**talk through the gateway** — the mirror of cycle 22, an almost pure client feature over the cycle-15
`/audio/tx` socket. **Verified, not assumed:** the whole TX contract already exists — `?token=`→1008,
single-talker `TxSlot`→1013, the JSON format handshake
(`{"rate":48000,"width":2,"channels":1}`→`parse_tx_format`, non-canonical→1003), the
`{"status":"ready","format":…}` ack, whole-sample framing (odd→1003), PTT keyed on the first real
frame + dropped on close/idle (2 s), and `MockRadio.tx_log` (`list[AudioFrame]`, `.samples`==bytes
sent) — all covered by `tests/test_tx_audio.py`. **One minimal server change** surfaced in browser
verification (the "server gap gets a pytest" the brief anticipated): a browser **cannot see a
pre-accept WS close code** — a rejected handshake shows as generic **1006**, so the single-talker
**1013 was invisible**. Fixed by **accept-then-inform**: `api/app.py`'s busy path now `accept()`s,
sends an explicit **`{"status":"busy"}`** message the client reads, then closes 1013 (ordering is
load-bearing — the busy path returns before the `session`/`finally`, so it never releases the *other*
talker's slot). Token/1008 stays pre-accept (a browser 1008 is a rare rotated-token edge — token is
gate-validated first — surfacing as a generic error). The two second-talker tests now assert the busy
message then 1013. `uv run pytest` stays **386 passed, 4 skipped**. **The client** (new under
`web/src/`): **`txWorklet.js`** — a
`"tx-capture"` sink worklet (`numberOfOutputs:0`), the inverse of `rxWorklet.js`, forwarding each
captured Float32 quantum (a copy) to the main thread. **`useTxAudio.js`** — mirrors `useRxAudio`
(state/ref split, gesture gate, rAF meter of the *outgoing* audio): `startTalk()` (from the Talk click)
`getUserMedia({audio:{channelCount:1,…}})` (denial→clear "denied" state, no hang), builds
`MediaStreamSource → tx-capture` on a **default-rate** `AudioContext` (NOT forced 48k, so the
resampler is the real path), opens `/audio/tx?token=`, sends the canonical header, awaits the ready
ack, then streams. **The load-bearing piece: client-side resample** `ctx.sampleRate → 48000` (streaming
linear interpolation, carrying `prev` sample + fractional `pos` across quanta so it's click-free) +
Float32→Int16 LE, batched into ~20 ms (960-sample/1920-byte) frames — the exact inverse of cycle 22's
decode. **No auto-reconnect** (unlike RX): a keyed transmitter must never silently resurrect — close
codes map to states (1008→`onAuthError`/re-gate; **1013→"radio busy", no retry-hammer**;
1003→format-error). `stopTalk()` closes the WS (server `finally` drops PTT + frees the slot), **stops
the mic tracks** (clears the OS indicator), tears down. **`TalkControl.jsx`** — the TX pair to
ListenControl: a red `.ptt.keyed` toggle ("Talk"/"Stop talking"), an "on air" badge, a red mic level
meter (`.meter-tx`), and clear denied/busy states; reports its talking state up. **Half-duplex UX:**
because the RX **jitter buffer holds ~500 ms**, server-side RX suspension alone would let you *hear
yourself gate in/out* — so `ControlPanel` lifts the local `talking` state and passes `suspendedLocally`
→ `ListenControl` → a new **`forceMute`** input on `useRxAudio` (effective gain `=(muted||forceMute)?0:1`,
ramped live) that mutes the monitor **immediately** on local keying; gated on *our own* talk, not the
global `transmitting`, so a remote op's TX doesn't mute us. `PttControl` (REST `/ptt`) left untouched
(orthogonal manual key). Vite proxy already had `/audio/tx` (cycle 22); no `api.js` change (WS auth is
`?token=`). **Verified end-to-end in a real headless browser** (Chrome with a fake mic device): Talk
keys + streams canonical frames into `tx_log`; a forced-44.1k context still lands ~48k/s (resample
proven); release drops PTT + frees the slot; a second talker → "radio busy" no-retry; mic denial →
clear message, no hang; talking mutes the local RX monitor. Deferred, on purpose: recordings
playback/download UI, async scan + `/scan/stop` (noted backend gap), Opus/compression. Next: recordings
playback/download, or the backend scan-stop.

Cycle 22 complete: **web UI — live RX audio playback** (ADR 0023), mock-only. The browser now **plays
what the radio hears** — a pure client feature over the cycle-13 `/audio/rx` socket, plus **one minimal,
symmetric server change**. **Verified, not assumed** (the brief's caution): `/audio/rx` sent *no*
format header (just raw `send_bytes` after `accept()`), while `/audio/tx` has a declared-format
handshake — the "cycle-15 symmetry decision" was never actually implemented. Realized now: `/audio/rx`
sends **`{"status":"ready","format": asdict(CANONICAL_FORMAT)}` first**, mirroring TX's ready ack, then
the raw canonical PCM as before (the **only** Python edit — one `send_json` in `api/app.py`; the
demand-driven pump lifecycle is untouched). Three RX WS tests now read the header first
(`receive_json()`) and a new `test_audio_rx_sends_format_header` asserts it (reject-token tests
unaffected — they close `1008` before `accept()`). **The client** (all new under `web/src/`): an
**AudioWorklet ring-buffer player** (`rxWorklet.js`, processor `"rx-player"`) fed by **`port.postMessage`
Float32 — deliberately NOT SharedArrayBuffer, so no COOP/COEP headers and the cycle-21 same-origin
static mount stays intact**. A **jitter buffer** primes ~150 ms then drains and caps latency at ~500 ms
(drop-oldest, mirroring the server hub); an **underrun outputs silence + re-primes**, so *every* gap —
scripted RX silence, WS reconnect, and the arbiter suspending RX during TX (half-duplex, ADR 0017,
where `/audio/rx` just stops delivering frames) — is a **clean pause, no buzz, no crash**, resuming
cleanly when frames return. **`useRxAudio.js`** mirrors `useEvents` (`?token=` auth, backoff reconnect,
`1008` → `onAuthError` back to the gate) but `binaryType="arraybuffer"`; it **creates nothing until the
Listen gesture** (browsers start an `AudioContext` suspended — autoplay is impossible on load), then
builds the context at **48 kHz** (canonical PCM maps 1:1, no resample), loads the worklet, wires
`worklet → GainNode(mute) → destination`, reads the leading header (noted, but plays canonical
regardless → header-less older servers still work), and decodes each frame `Int16Array → Float32`
(`/32768`, LE). **`ListenControl.jsx`** (a `.card` in the left column): Listen/Stop, a mute (`GainNode`
0/1), a peak **level meter** (per-frame peak smoothed on `requestAnimationFrame`, reflects incoming
audio even when muted), a stream conn badge, and a **"receiving paused (transmitting)"** note driven off
the existing `/events` `transmitting`/`arbiter` state (no new server "suspended" marker). `vite.config.js`
gains `/audio/rx` (+ `/audio/tx`, reserved for 23) as `ws:true` dev-proxy entries. `uv run pytest` →
**386 passed, 4 skipped** (+1; the 4 hardware/model skips unchanged). **Verified end-to-end in a real
headless browser** (Chromium against the live mock seeded with an audible looping tone): Listen →
continuous audio + moving meter; a TX-suspend gap via a streaming `/audio/tx` client → paused
indicator, no buzz, clean resume; Stop → pump idle; autoplay confirmed impossible before the gesture.
Deferred, on purpose: **TX mic capture (cycle 23)**, recordings playback/download + a GET API for the
JSONL ledger, a distinct `/events` "suspended" marker, Opus/compression. Next: **TX mic capture.**

Cycle 21 complete: **web UI — control panel** (ADR 0022), mock-only. The first browser client and,
finally, a **real server entrypoint**. Control + visibility only; **live audio is deferred to cycles
22–23**. A **React + Vite SPA** under a new top-level `web/` (sources in `web/src/`, builds to
`web/dist/`; `node_modules/` + `web/dist/` gitignored, so `npm install && npm run build` is a
documented prerequisite — the chosen cost of a build toolchain in a uv-only repo). **Served
same-origin:** `create_app` gained a keyword-default `web_dir` that mounts the built bundle at `/`
via `StaticFiles(html=True)` **mounted last** (after the REST router + WS routes, so the token-gated
API always wins); an **unbuilt** `web_dir` serves a "run `npm run build`" placeholder instead of
crashing; `web_dir=None` (all prior tests) adds no `/` route → surface unchanged. `build_app` reads
`RADIO_WEB_DIR` (marked default → `web/dist`). **The missing entrypoint exists:** `python -m
radio_server` (`radio_server/__main__.py`) binds uvicorn to `RADIO_HOST` (default `127.0.0.1`) /
`RADIO_PORT` (default `8000`) around the env-composed app — thin; `build_app` still fails loud
without `RADIO_API_TOKEN`. **`websockets` is now a runtime dep** — plain `uvicorn` ships no WS
implementation, so a bound server 404s every `/events` upgrade (the TestClient masked this with its
own in-process WS); added explicitly, not `uvicorn[standard]`, to stay lean. **Mock CAT toggle:**
`RADIO_MOCK_CAT` (marked default `on`) — `off` yields an **audio-only mock** so guardrail 3's
control-greying is demonstrable in a browser without hardware. **The UI:** in-memory token (React
state only, never `localStorage`; `Authorization: Bearer` on REST, `?token=` on the WS); token gate
validates via `GET /capabilities` (bad token → clear error, not a hang); **capability-driven
greying** off `/capabilities` with a **defensive 501** backup (reads `detail.capability`, greys
exactly that control); **one `/events` WS** folds `status`/`ptt`/`scan`/`session`/`auth`/`command`/
`arbiter` frames into a live status panel + a **bounded (~500)** scrolling event log, **reconnects
with exponential backoff** on drop (a `1008` rejected-token close stops retrying → back to the gate).
Controls: tune (freq/channel/tone-with-clear/mode), PTT toggle, scan, controller start/stop. **Honest
about the API:** `/scan` is one synchronous sweep returning `held` with **no server-side stop**, so
there's a "Scan" button and live phase — no dead stop button; controller `503` "not configured"
renders as a disabled control with a message, not a dead button. **Backend behavior unchanged** — the
only Python edits are `api/app.py` (mount + env toggles), the new `__main__.py`, and the `websockets`
dep; every other package untouched. **Verified end-to-end in a real headless browser** (Chromium via
Playwright against the live server): token gate, CAT-vs-audio-only greying, each control hitting its
endpoint and reflecting result, `/events` driving status + log, controller 503, and **WS reconnect
after a server drop/restart** — all green. `uv run pytest` → **385 passed, 4 skipped** (+8 in
`tests/test_web.py`: static mount, unbuilt placeholder, static-never-shadows-gated-API, `web_dir=None`
unchanged surface, `RADIO_WEB_DIR` + `RADIO_MOCK_CAT` env wiring; SPA itself is browser-verified).
Deferred, on purpose: **live RX playback (cycle 22)**, TX mic capture (23), recordings
download/playback + a GET API for the JSONL ledger, a server-side scan-stop, an `/events` "suspended"
marker for arbiter RX-pause. Next: **live RX audio.**

Cycle 20 complete: **recording safety rails + TX recording** (ADR 0021), mock-only. Closes the three
cycle-19 footguns and folds in the deferred TX capture. **The backend is now genuinely complete and safe.**
Four pieces: **(A) Max-duration segment roll** — `Recorder` gained an **always-on** cap `max_seconds`
(`RADIO_RECORD_MAX_SECONDS`, default 3600, positive-or-fail-loud, **no disable sentinel**). `write()` checks
the injected clock **before** the lazy-open and, if the open segment has run `>= max_seconds`,
`end_segment()`s so the existing lazy-open rolls a fresh file — the triggering frame starts the new segment;
`_open_segment` stamps `_segment_started`, the `_wav is not None` guard makes a stale start after `_abort()`
harmless (no reset). So **no single WAV grows without bound even under `RADIO_SQUELCH=off`** — its endless-file
footgun is closed. FakeClock-deterministic (reuses the stamp clock). **(B) Squelch-off warning** — `build_app`
now logs a one-time `WARNING` (the repo's **first** `logging` use — a module `logger`, handler-free, `caplog`-
testable) when `RADIO_RECORD=on` and `RADIO_SQUELCH=off`, saying segmentation is time-based (the roll), not
activity-based. **It does not fail** — the roll makes it safe. **(C) Half-duplex split** — `RxPump.run`'s
existing `if self._arbiter.transmitting:` branch now also calls a **guarded** `self._recorder.end_segment()`
before sleeping, so a streaming-TX key-up mid-RX **finalizes the open RX segment** and resume lazy-opens a
fresh file — a recording reflects one continuous receive, no concatenation across the keyed gap. Idempotent →
no rising-edge flag. Correctly scoped to `arbiter.transmitting` (streaming TX only; REST `/ptt` keys directly
and never touches the arbiter, so it neither pauses nor splits — pre-existing behavior). **(D) TX recording** —
the same `Recorder` records transmitted audio, distinguished only by a **`tx-` filename prefix** (the
hardcoded `rx-` became a ctor `prefix` param). `TxSession` gained a `recorder` injection (a **local**
`TxRecorder` Protocol + `null_recorder` default mirroring `rx.pump.RxRecorder`, so `tx` **never imports**
`recording` — the arrow stays `tx -> {audio, backends}`); `feed()` writes each transmitted frame (guarded,
after `transmit`), `close()` finalizes. Opt-in via **`RADIO_RECORD_TX`** (default off, **independent** of
`RADIO_RECORD`); shares `RADIO_RECORD_PATH`, inherits `RADIO_RECORD_MAX_SECONDS`, ignores `RADIO_RECORD_MODE`
(gating is an RX concept). RX/TX are **separate `Recorder` instances** (own sequence counters) in the same dir,
disambiguated by prefix, both on `time.time` → filename stamps **timestamp-align** with the ledger's
`tx_key_up`/`tx_key_down`. **The sharpest failure-isolation call:** `close()`'s `end_segment` is placed **after**
the keying/arbiter-release work and **inside `if self._keyed`**, and **guarded** — the `/audio/tx` `finally`
runs `session.close()` **then** `tx_slot.release()`, so an exception escaping `close()` would skip the slot
release and **permanently wedge the single transmitter**; guard + ordering guarantee a disk fault can never
break keying or leak the slot. Concurrent isolation comes from `TxSlot` (a second talker is refused **before**
its `TxSession` is built), so the shared `tx_recorder` is only ever fed by one talker; sequential talkers share
it and get a continuous `tx-000001`, `tx-000002`… counter. **Wiring:** `create_app`/`build_app` gained a
`tx_recorder` param (keyword-default → existing callers unchanged), stored on `app.state.tx_recorder`, closed in
the lifespan teardown alongside `recorder`, and passed into each `/audio/tx` `TxSession`; `build_app` calls
`build_tx_recorder(env)`. `uv run pytest` → **377 passed, 4 skipped** (+25; the 4 multimon/piper skips
unchanged; all prior tests pass untouched — only keyword-default params were added). Deferred, on purpose:
Opus/compression, retention/cleanup, the playback/download API (the web UI), full-capture (pre-gate) mode
(seam only), decoupling recording from the demand-driven pump. Next: **the web UI.**

Cycle 19 complete: **audio recording — received audio → WAV** (ADR 0020), mock-only. The stack could
capture, stream, gate, and log audio but not **keep** it; this cycle adds a passive `Recorder` that
writes received audio to timestamped WAV files, one per RX activity session. **The load-bearing
design call:** the brief said tap the pump "as another sink alongside the WS listeners" (an
`AudioHub` subscriber), but the hub only ever carries **gate-open** frames — a subscriber can't see
the **gate-close** edge that bounds one session, so segmentation would need a wall-clock gap timeout
(not `FakeClock`-deterministic) or a racy second channel. So the recorder is a **`Protocol`-injected
sink the pump calls directly** (confirmed with the user): gate-open → `recorder.write(samples)`, a
non-empty frame the gate **rejects** → `recorder.end_segment()` (the close edge), empty frames reach
neither; the hub `publish` runs **first** so recording never adds stream latency. This sees exactly
the post-gate frames the hub streams, opens **no second capture reader**, is deterministically
testable, and gated-recording falls out for free. A new pure-leaf **`radio_server/recording/`**
package (sibling of `eventlog/`, imports only `..audio`) holds **`Recorder`**: WAV via the stdlib
**`wave`** module (no new dep; fixed canonical 48k/s16le/mono → deterministic header), lazy per-
segment open, filename `rx-{seq:06d}-{YYYYmmddTHHMMSSZ}.wav` (the **sequence counter** guarantees
uniqueness + lexical==chronological order; timestamp from an injected `Clock`). `rx/pump.py` gained
only a local **`RxRecorder`** Protocol + a **`null_recorder`** no-op default; `api/app.py` is the
only meeting point (`create_app(recorder=None)` → `app.state.recorder` → `RxPump`; shutdown
`close()`; `build_app` calls `build_recorder(env)`). **Config (opt-in):** `RADIO_RECORD` (default
**off** → no recorder, writes nothing), `RADIO_RECORD_PATH` (marked default `recordings`, validated
**fail-loud at construction** via makedirs+probe like `JsonlSink`), `RADIO_RECORD_MODE` (default
`gated`; `full`/pre-gate is a **reserved seam** — `build_recorder` raises `NotImplementedError`, not
silently gated). **Failure isolation (hard rule):** `Recorder` catches+drops internally **and** the
pump guards its calls (double guard — the pump is the single shared capture task whose death blinds
every listener; disk I/O is a broader fault surface than the non-raising leaves — the `EventLog.handle`
reasoning). **TX recording deferred** with a note (clean via the cycle-18 `on_key` edges + a `tx-`
prefix later; `feed` is the load-bearing keying path, its own cycle). **Documented, not fixed:** with
`RADIO_SQUELCH=off` there's no gate-close edge so all RX becomes one file (finalized on pump stop);
recording is coupled to the demand-driven pump (nothing records when nobody's listening); a
half-duplex TX pause concatenates across the keyed gap. `uv run pytest` → **352 passed, 4 skipped**
(+29; `create_app`/`RxPump` gained only keyword-default params, so all prior tests pass unchanged; 4
hardware skips unchanged). End-to-end smoke: scripted frames through `/audio/rx` produced both the
live stream and a valid on-disk WAV (canonical header, exact PCM). Deferred: Opus/compression,
retention/cleanup, playback/download API (the UI), full-capture mode (seam only), TX recording,
pump-decoupling. Next: the web UI.

Cycle 18 complete: **emit the deferred log events** (ADR 0019) — pure wiring, mock-only, **no new
record shapes**. Cycle 17 built the full ledger taxonomy but ~half was **dead in production**: the
`auth`/`command`/`arbiter` mapper branches and the `station_id` `callsign`/`mode` fields are pure
functions of events **nothing published**. This cycle connects the real producers. The load-bearing
constraint: every leaf (`auth`/`services`/`controller`/`arbiter`/`tx`) **deliberately does not import
`EventHub`** — the only `hub.publish` sites are in `api/app.py`; leaves emit domain events through
injected callbacks and the API adapts. So "publish in the site's own package" is impossible as
written; the faithful realization (and what "don't centralize" means) is **each producer surfaces its
own signal at its own site**, routed through a callback the API turns into a hub event. **Five
emissions, all via the callback → API-adapter pattern, zero `hub.publish` in any leaf:** (1)
`auth_accepted` + `auth_rejected` from the `Controller.step` outcome loop (auth signals carry **no
data** — never a code); (2) `command_dispatched {service}` on a **transmitted** dispatch only (a
registry miss is a graceful no-op, no record); (3) `station_id` enriched with `{callsign, mode}` —
`StationId` gained `callsign`/`mode` properties, `mode` threaded from `load_id_mode(env)` at
`build_controller`; (4) `arbiter_mode` via a new `RadioArbiter(on_change=...)` fired **only on a real
derived-mode change** (leaf-pure `Callable`, no import); (5) streaming-TX `ptt` via a new
`TxSession(on_key=...)` at both key edges — streaming keying now logs `tx_key_up`/`tx_key_down` with
duration like REST. The API adapter `_publish_controller` (renamed from `_publish_session`) fans the
controller's one `on_event` channel out by phase → `auth`/`command`/`session` hub types. **Correction
to the brief:** "auth_accepted already flows" was wrong — the accept path emitted only `session_open`;
both are now distinct records. **Fire-and-forget confirmed, not regressed:** `EventHub.publish` is
`put_nowait` onto unbounded queues (non-blocking, non-raising), so these synchronous emissions can't
break auth/dispatch/keying/arbiter. The cycle-17 `eventlog/` mappers changed **zero lines** — they
just light up. `uv run pytest` → **323 passed, 4 skipped** (+12; 3 existing controller assertions
updated for the richer stream, none weakened; 4 skips unchanged). End-to-end proof: a bad-code →
login → command → forced-ID → streaming-TX round-trip through `create_app` writes a JSONL file
containing **every** taxonomy type with no code/secret/token material. Deferred: SQLite sink, log
rotation/retention, query/`GET` API, audio recording (next cycle), web UI (the sequence after).

Cycle 17 complete: **event log / QSO ledger** (ADR 0018) — a durable, structured, timestamped
station log, mock-only and hardware-free. The events a log needs **already flow** through `EventHub`
(ADR 0011), so the ledger is **not new instrumentation** — it is **another SUBSCRIBER** of that flow
that writes durable records, adding **zero** `hub.publish` sites to `auth`/`arbiter`/`tx`/`controller`.
A new pure-leaf **`radio_server/eventlog/`** package (imports only stdlib + `..api.events.Event`)
holds it. **`LogSink`** is the storage protocol (`write`/`close`); the default **`JsonlSink`** writes
**append-only JSONL, one JSON object per line** (greppable, `tail -f`-able — the project's first
persistence). A **SQLite sink is the documented future swap**, not built. Path is `RADIO_LOG_PATH`
(marked default `radio-server.jsonl`, mirroring `time_service.load_timezone`); a **set-but-unwritable
path fails loud at construction** (`JsonlSink.__init__` opens in append mode → `OSError` at the
composition root). **`EventLog`** is the sync mapper: a lifespan-managed background task drains its
own `hub.subscribe()` queue and calls `EventLog.handle(event)` — the exact `/events` consumer shape,
passive, never blocks `publish` (unbounded queue). Records are flat `{"ts": <clock float>, "type",
...fields}`; `ts` from the injected **`Clock`** (`Callable[[], float]`, default `time.time`,
`FakeClock`-testable). `tx_key_up` remembers its timestamp so the paired `tx_key_down` records the
keyed **duration** (Part 97 value). **SECURITY (hard rule):** the mapper **whitelists** the fields
each record emits — it **never spreads `event.data`** — so a TOTP code/secret/API token can never
reach the ledger even if it appeared upstream; a rejected-auth record is just `{ts, type:auth_rejected}`
(tested with a fake `code`/`secret` payload → absent). **Failure isolation:** `EventLog.handle` catches
+ drops on any error (a logging fault never reaches the pump or a transmission), and the audio path
(`/audio/rx` `AudioHub`, `/audio/tx` `TxSession`) never flows through `EventHub` anyway; graceful
shutdown drains still-queued events before closing the sink (no lost entries). Live records today:
`ptt` (REST `/ptt` key-up/down), `scan` (phases incl. `active`+freq), `session` (open/id/close).
**Forward-compatible but NOT yet emitted to the hub** (mapper ready, `hub.publish` deferred to a
future instrumentation cycle): `auth_accepted`/`auth_rejected`, `command_dispatched`, `arbiter_mode`,
and ID `callsign`+`mode` fields. Wiring is confined to `create_app` (new `event_log=None` default →
existing tests unchanged) + `build_app` (opens the sink). `uv run pytest` → **311 passed, 4 skipped**
(+18; the 4 skips unchanged). Deferred: SQLite sink, log rotation/retention, a query/GET API, audio
recording (cycle 18), web UI (cycle 19+), and the live emissions above.

Cycle 16 complete: **RX/TX duplex conflict policy** (ADR 0017) — the **last pure-software cycle**,
mock-only. A half-duplex radio can't receive and transmit at once (keying blinds the receiver), so
this cycle adds the seam that enforces it: **TX takes the radio; the RX pump and any live scan stand
down while keyed and resume when TX drops.** A new pure-leaf **`radio_server/arbiter/`** package
(imports *nothing* from the rest of the tree, so `tx`/`rx`/`scan`/`api` all depend on it with no
cycles) holds **`RadioArbiter`** — "who has the radio right now" as **`RadioMode`** (`idle` /
`receiving` / `transmitting`), modeled as **two independent latches** (`_transmitting` set by TX,
`_receiving` set by the RX pump) with a **TX-priority derived mode** (`transmitting > receiving >
idle`). That beats preempt/restore bookkeeping: on `release_tx()` the RX latch is still set, so the
mode falls back to `receiving` on its own. **Coherence guard:** `acquire_tx()` raises
**`ArbiterStateError`** on a double-key (one transmitter, one talker); `release_tx()` is idempotent
(mirrors `TxSession.close()`). One shared arbiter is created in **`create_app`** (`app.state.arbiter`)
and injected into the RX pump and every per-connection `TxSession`. **TX (writer):** `TxSession.feed()`
calls `acquire_tx()` before `ptt(True)`, `close()` calls `release_tx()` after `ptt(False)` — the two
existing keying points. **RX (reader):** `RxPump.run()` asserts `begin_receive()`/`end_receive()` and,
while `arbiter.transmitting`, **does not pull `receive()` at all** (you can't read a blinded receiver);
listeners stay subscribed (`subscriber_count` unchanged) — only delivery pauses, then resumes to the
same queue. **Scan (reader):** `ScanEngine.tick()` early-returns while transmitting — no tune, no poll,
no advance; **resume needs only the flag** because all positional state (`_state`, `_i`, `_current_freq`,
`_tuned_at`, `_dwell_deadline`) already survives on the instance (noted wrinkle, not fixed: `_tuned_at`
is wall-clock, so a channel paused mid-settle polls one tick sooner after a long pause — harmless). The
`POST /scan` `sweep()` path is untouched (synchronous, can't interleave). Every consumer's arbiter param
**defaults to a private idle arbiter**, so standalone construction is behaviorally unchanged — all prior
tests pass untouched. `MockRadio`/`audio/format.py`/`activity/`/`controller/`/`auth/`/`events.py` are
untouched (tune + receive spies live in the test). `uv run pytest` → **293 passed, 4 skipped** (+10 — 6
arbiter unit, 2 RX-pump, 1 scan, 1 end-to-end; the 4 skips unchanged). Deferred: the optional `/events`
"suspended" marker (behavior delivered without it), Opus, and the real backends' audio I/O + on-bench
PTT-tail/turnaround timing (guardrail 1 — the arbiter models the *logical* exclusion, never the ms).

Cycle 15 complete: **TX audio ingest** (ADR 0016) — the **second half of "talk through the gateway,"**
mock-only, the mirror of cycle 13's RX path in the opposite direction. A binary WebSocket **`GET
/audio/tx`** accepts canonical PCM *in* from a LAN client and feeds it to `radio.transmit()`; it lands
in `MockRadio.tx_log`. Same `?token=` auth plane as `/audio/rx` (rejected pre-`accept()` with
`WS_1008`). A new **`radio_server/tx/`** package sits **below `api`** (imports only `..audio` +
`..backends`, never `rx`/`api`), mirroring the `activity` layering. **No hub, no pump** — TX is
**fan-in/serialized** (one radio, one talker), the opposite of RX's fan-out. **`TxSession`** is the
per-connection keying/ingest state machine (guardrail 2): `feed(data)` validates whole-sample framing
**first** (a bad frame raises before any `ptt()`, so it never keys), **skips empty `b""`** (mirrors
`RxPump`), keys **`ptt(True)` once** on the first real frame, `transmit`s each frame, stamps activity;
`close()` drops **`ptt(False)`** (idempotent) on any exit — PTT is keyed via `ptt()`, **never a CAT
TX**. **`TxSlot`** is the single-talker guard — a plain flag, **not** an `asyncio.Lock` (a Lock would
*queue* the second talker; we must *refuse* it): a second concurrent client is closed **`1013`** before
`accept()`, released in the endpoint's `finally`. Wire protocol: token → slot acquire → `accept()` →
**declared-format handshake** (`parse_tx_format` builds the client's declared `AudioFormat` and requires
`== CANONICAL_FORMAT`, else `AudioFormatMismatch` → **`1003`**; on success acks `{"status":"ready"}`) →
binary frame loop. **Idle timeout:** the endpoint wraps each receive in `asyncio.wait_for(...,
timeout=session.idle_timeout)`; on `TimeoutError`, `session.on_idle()` drops PTT — `wait_for` is only
the wakeup, the **decision** is the clock-injected `idle_elapsed()` (`FakeClock`-testable, no real
sleeps). Close codes: `1008` token · `1013` busy · `1003` bad format/frame · idle → normal `1000`.
`create_app` gained **`tx_idle_timeout=DEFAULT_TX_IDLE_TIMEOUT`** + an `app.state.tx_slot`; `build_app`
reads **`RADIO_TX_IDLE_TIMEOUT`** via `load_tx_idle_timeout`. `DEFAULT_TX_IDLE_TIMEOUT` is guardrail-1
**verify-on-hardware** (real PTT-tail/buffer/cadence). `MockRadio` and `audio/format.py` are
**untouched** — the `ptt` spy (`_PttSpyRadio`) lives in the test. `uv run pytest` → **283 passed, 4
skipped** (+26 tests — 12 WS-integration, 14 unit; the 4 skips are unchanged). Deferred: Opus, real
backend transmit + on-bench timing (hardware), and the **full-duplex RX-while-TX conflict policy**
(noted, not built).

Cycle 14 complete: **software squelch / activity detection** (ADR 0015) — the RX activity-gate seam
from cycle 13 is now filled with a real detector, mock-only. A new **`radio_server/activity/`**
package sits **below `rx`** (imports only `..audio` + `..backends`, never `rx`) so the same activity
signal is reusable — later it feeds scan's stop decision, not just the RX stream. **`frame_rms`** is
the pure, shared energy primitive (RMS of a canonical s16le frame via numpy; empty/odd-byte → `0.0`,
never raises). Two gates implement the one `(AudioFrame) -> bool` shape, picked by backend/config
(mirroring scan's busy-poll question): **`AudioLevelGate`** is software VAD with **hysteresis** (open
on the higher `on_threshold`, hold on the lower `off_threshold`, so a marginal signal doesn't chatter)
and **hang** (stay open `hang` s after the level drops so a speech gap doesn't clip — clock-injected,
`FakeClock`-testable, no real sleeps); construction fails loud if `on <= off`. **`CatBusyGate`** reads
the V71's hardware squelch over `status().busy` and **ignores the frame** (the noted interface tension:
it needs the radio at construction, not just the frame) — the only option for the busy-line-less
Baofeng is audio VAD. **`build_rx_gate(env, radio)`** selects via **`RADIO_SQUELCH`** (`off` | `audio`
| `cat`, fail-loud on anything else); **default `off`** returns the cycle-13 `pass_through_gate`
**unchanged** — the intended per-backend mapping (V71→`cat`, Baofeng→`audio`) is documented, not
hardcoded (auto-derive from capabilities is deferred). `create_app` gained an optional
**`rx_gate=pass_through_gate`** flowed into `RxPump`; `build_app` computes it from the env. VAD
thresholds/hang (`DEFAULT_VAD_ON_RMS`/`OFF_RMS`/`HANG`, env `RADIO_VAD_*`) are guardrail-1
**verify-on-hardware** — real noise floor and speech-gap timing are bench-tuned. `rx/`, `scan/`, and
the backends are untouched. `uv run pytest` → **257 passed, 4 skipped** (+19 model-free tests; the 4
skips are unchanged). Deferred: TX ingest (15), Opus, real capture + real threshold tuning (hardware),
scan rewire.

Cycle 13 complete: **RX audio streaming** (ADR 0014) — the **first half of the voice relay**, mock-
only. Received audio now leaves the box: a binary WebSocket **`GET /audio/rx`** streams raw canonical
PCM (48k/s16le/mono) via `send_bytes` — a **separate socket** from the cycle-10 `/events` JSON
stream, sharing only its `?token=` auth plane (rejected pre-`accept()` with `WS_1008`). A new
**`radio_server/rx/`** package holds the transport: **`AudioHub`** is the audio sibling of
`EventHub` but **bounded + drop-oldest** (each subscriber gets a bounded queue; on overflow `publish`
evicts the oldest frame so the live stream stays near-real-time) — a slow/stuck listener drops frames
without ever blocking the pump or other listeners. **`RxPump`** is a thin async loop over the
synchronous `receive()` (the `ControllerRunner` shape) that publishes each **live** frame's PCM; it
is **demand-driven** (`start()` on the first `/audio/rx` subscriber, `await stop()` on the last) and
**controller-independent**. It takes an injectable **`RxActivityGate`** predicate (default
`pass_through_gate`) — the **squelch seam only**; real software squelch/VAD is cycle 14. Distinct
from the gate, the pump **skips empty (0-byte) frames** (a transport sanity rule). `start()` sets
`running` synchronously and is idempotent; `stop()` nulls its task ref **before** awaiting the cancel
(a reconnect-during-teardown starts fresh, not stalled); a **lifespan shutdown handler** also stops
the pump — the real no-leaked-task guarantee. `MockRadio` gained a scriptable RX sequence
(**`rx_frames`** ctor arg + **`script_rx(*frames)`**, drained FIFO by `receive()` before falling back
to `canned_rx`) — the RX mirror of `tx_log`. RX cadence/buffering (`DEFAULT_RX_POLL` > 0,
`DEFAULT_AUDIO_QUEUE_MAXSIZE`) are guardrail-1 **verify-on-hardware** config. `uv run pytest` →
**238 passed, 4 skipped** (+11 model-free tests; the 4 skips are unchanged).

Cycle 12 complete: the **controller loop** (ADR 0013) — the **full software tower now runs live
end-to-end on the mock**. One clock-injected driver pumps everything on a `receive()` loop:
received audio → DTMF → TOTP auth → dispatch → a CW-ID'd transmission, with automatic periodic and
sign-off ID and an optional live scan. `Controller.step(now, rx_audio)` is the **pure, testable
core** (one iteration); `ControllerRunner.run()` is a **thin async shell** looping `radio.receive()`
→ `step()` on a poll cadence with no logic of its own. This is the cycle where `StationId`'s
session-lifecycle methods finally connect to real events (built cycle 4, deferred since): an
`ACCEPTED` outcome **opens a session and arms the ID** (`begin_session`); the periodic-ID safety net
(`check`) **forces an ID when overdue mid-session** (Part 97); an inactivity close **signs off**
(`sign_off`). Because `AuthGate` only demotes an idle session *lazily* inside `on_dtmf`, the
transition was surfaced as **`AuthGate.expire_if_idle(session, now)`** (a behavior-preserving
refactor mirroring `DtmfFramer.tick`) so the loop can detect and act on it. Lifecycle is emitted as
`ControllerEvent(phase, data)` (`session_open`/`id`/`session_close`) through an **injected callback**
— the controller never imports `EventHub`, so `api → controller` has no cycle; the API adapts each to
a **`"session"` event** on the cycle-10 `EventHub`. `build_controller(env, *, radio, decoder, tts,
clock)` is the composition root (fail-loud on the TOTP secret / callsign; `decoder`/`tts` injectable
so tests use `FakeDtmfDecoder` + `StubTts`). The API gained **`POST /controller {on}`** (token-gated,
**503** when unconfigured — never a silent no-op) and a **`controller` block in `/status`**
(`{running, session_open}`, `null` when unwired). Loop cadence is guardrail-1 **verify-on-hardware**
config. `uv run pytest` → **227 passed, 4 skipped** (+14 model-free tests; the 4 skips are unchanged).

Cycle 11 complete: the **software scan engine** (ADR 0012) — "scan channels remotely like in
person." A V71/CAT-only scan *loop* over the `CatRadio` surface (distinct from the radio's built-in
`scan(on)` toggle): it steps a `ScanPlan` of frequencies, tunes each (`set_frequency`), lets the
reading **settle**, polls `status().busy`, and acts on activity. Two drive surfaces share one set of
pure helpers: `ScanEngine.tick(now)` is the **clock-driven resume-mode machine** (carrier = dwell
while busy, resume on drop — the marked default; timed = dwell N s then move on; hold = stop on first
activity), and `ScanEngine.sweep()` is a **synchronous single pass** that stops-and-holds at the
first active channel (clear channels advance instantly — no clock, no sleeps). Lockout skips
channels; a **priority** frequency is re-checked between steps. Progress is emitted as
`ScanEvent(phase, frequency, channel)` (`scanning`→`active`→`dwelling`, plus `resumed`) through an
**injected callback**, so `scan` stays *below* the API (no `scan↔api` cycle); the API adapts it to a
`"scan"` event on the **cycle-10 `EventHub`** (now registered in `EVENT_TYPES`), so a WebSocket
client watches scan progress live. **Capability-gated** exactly like the other CAT endpoints:
`POST /scan` runs one sweep on a CAT backend and returns **`501` naming `"scan"`** (never a no-op) on
an audio-only one, where it is not advertised. `MockRadio` gained scriptable **`busy_frequencies`**
so a test can script per-channel activity and drop a carrier mid-scan — fully deterministic, no
hardware, no real sleeps. Timing (settle, poll cadence) is guardrail-1 **verify-on-hardware** config.
`uv run pytest` → **213 passed, 4 skipped** (+26 model-free tests; the 4 skips are unchanged).

Cycle 10 complete: the **FastAPI HTTP/WebSocket API layer** (ADR 0011) — the stack is reachable
over the network for the first time, and **guardrail 3 (the capability split) is enforced at the
HTTP boundary**. A thin, honest surface over the injected `Radio`: shared endpoints (`GET /status`,
`GET /capabilities`, `POST /ptt`, `POST /transmit`) always live; the CAT endpoints
(`POST /frequency` `/channel` `/tone` `/mode`) check `Capability` membership and, on an audio-only
backend, return **`501` with the missing capability named in the body** (`{"capability":
"set_frequency"}`) — never a silent no-op, so the web UI can grey out exactly the right control. A
**second, separate auth plane** lands here: a LAN-facing static **bearer token** (constant-time
compare, closed by default, `401`/WS-`1008` on missing/bad), kept deliberately distinct from the
over-RF TOTP/DTMF plane (different threat model — no replay window/burn). A `type`-discriminated
WebSocket `EventHub` pushes a `status` snapshot on connect and further events on control calls;
its shape is left **open for the scan engine's `scan` events next cycle**. `FastAPI`/`uvicorn` are
**core deps** (the API is the product's stated purpose), so the tests **run**, not skip. The API is
**independent of the DTMF/piper/voice-ID stack (#7–#9)** — it imports only `backends` + the new
`api` package and touches no `services/` file — so the two compose additively, as they now do on
`master`: with #7–#9 merged alongside, `uv run pytest` → **187 passed, 4 skipped** (cycle 10 added
18 API tests; cycles 7–9 added 38, with 4 hardware/model `skipif` gates).

Cycle 9 complete: **`VoiceId` + configurable ID mode** (ADR 0010) — the **audio-content
tower is now complete**. `VoiceId` is the second `IdEncoder` (after `CwId`): it speaks the
callsign as NATO/ITU phonetics (**9→"niner"**, so "AE9S" → "alpha echo niner sierra")
through an injected `TtsEngine` — `StubTts` in tests (byte-exact), `PiperTts` in production.
It satisfies the same one-arg `encode(callsign)` contract, so the **cycle-4 `StationId`
scheduler is untouched** — swapping CW for voice is an encoder swap, not a scheduler change.
The phonetic map (`PHONETIC`, `spell_callsign`) is **pure and separated from synthesis**, so
it is exactly assertable with no engine; unknown chars **fail loud** (`ValueError`), and the
accepted set matches `CwId`'s (A-Z, 0-9, `/`→"slash"). `RADIO_ID_MODE` (`cw` | `voice`)
selects the encoder via `build_id_encoder` (the first real composition root); **CW is the
marked default** (no model dependency, always works). Voice mode with no `RADIO_TTS_VOICE`
**fails loud** at construction — it never silently degrades to CW. **Guardrail 1:** the one
real-piper `VoiceId` test is `skipif`-gated (skips here) and property-asserted; on-air
intelligibility is a bring-up check. `uv run pytest` → **169 passed, 4 skipped** (+17
model-free tests in `test_voice_id.py`; the 4th skip is the new real-`VoiceId` test).

Cycle 8 complete: **real piper TTS** (`PiperTts`; ADR 0009) — the first real spoken audio,
behind the existing cycle-3 `TtsEngine` protocol. `render(text)` runs piper at the voice's
native rate and resamples up to canonical 48k, so `PiperTts` is the **first consumer of
`to_canonical`** — this cycle *proves the playback edge*, the symmetric mirror of cycle 7's
`to_multimon` decode edge (both ADR 0006 edges are now exercised). It is a **drop-in for
`StubTts`**: same one-method `render` contract, so the time service, dispatcher, `StationId`,
and `CwId` are untouched, and `StubTts` is **retained unchanged** as the deterministic
exact-assert baseline. The voice's native rate is **read from its `.json` sidecar**
(`audio.sample_rate`), never hardcoded to 22050 (voices vary; some are 16000). Model config
**fails loud**: `RADIO_TTS_VOICE` names the `.onnx` and has **no default** (like the TOTP
secret) — `load_tts_voice` raises when unset, and `PiperTts.__init__` raises on a missing
`.onnx`/sidecar/rate, *before* any piper import. **Guardrail 1:** piper + `onnxruntime` are
**not installed** here (declared as an optional `tts` extra, not a core dep), so the two
real-engine tests are `skipif`-gated (skip here, run where a model is present); the exact
piper version/API is isolated in `_synthesize_raw` and marked verify-against-build; neural
output is **property-asserted, never byte-asserted**; RF intelligibility is a bring-up check.
The `to_canonical` edge itself is proven **model-free** — a synthetic 16000/22050 Hz voice
buffer resamples to a canonical 48k frame of the expected length. `uv run pytest` →
**152 passed, 3 skipped** (+9 model-free tests in `test_tts.py`; the 3 skips are the 2 real
piper tests + cycle 7's real-decode test).

Cycle 7 complete: **DTMF decode + framing** (`radio_server/audio/dtmf.py`; ADR 0008) — the
audio-in → digits seam, and the **first full end-to-end on the mock**. Received `AudioFrame`
audio now drives the auth gate: `DtmfDecoder` (protocol seam; real `MultimonDtmfDecoder`
shells out to `multimon-ng -a DTMF -t raw -` over stdin, a `FakeDtmfDecoder` drives tests) →
`DtmfFramer` (pure, clock-injected grammar: `#` submit, `*` clear, inter-digit timeout
**discards** a stalled partial) → `DtmfInput.pump(frame)` returns completed entries → the
**unchanged** `AuthGate.on_dtmf`. Nothing in auth/session/dispatch/`station_id`/`CwId` changed
— the module is even **auth-free** (local `Clock` alias), so the layering arrow stays
audio → nothing-above. Fixtures are deterministic `synth_dtmf` dual-tones (sum two
`synth_tone` frames at the standard `DTMF_FREQS`), asserted by FFT — no on-disk WAVs
(multimon reads raw PCM on stdin). Config: `RADIO_DTMF_TIMEOUT` (default 3.0s) /
`RADIO_MULTIMON_BIN` (default `multimon-ng`), marked defaults. **Guardrail 1:** `multimon-ng`
is **not installed** in this environment, so the one real-decode test is `skipif`-gated on the
binary (skips here, runs where installed); the exact multimon flags/rate are marked
verify-against-build, and real weak-signal / HT-flutter decode robustness is a hardware
bring-up check, not proven here. `uv run pytest` → **143 passed, 1 skipped** (+13 tests in
`test_dtmf.py`). The headline: fixture audio (fake-decoded) → framed digits → TOTP `ACCEPTED`
→ authed `"1"` `COMMAND` → a real CW-ID'd time announcement in `mock.tx_log`.

Cycle 6 complete: **real CW station ID** (`CwId`; ADR 0007) — the first real transmission
content the server produces. `CwId` implements the existing one-method `IdEncoder`, so it is
a **drop-in for `StubId`**: `StationId`, `Dispatcher`, and every config loader are untouched,
and an authed `"1"` now prepends genuine keyed Morse to the time announcement. A pure PARIS
timing layer (`unit_ms`, `cw_timeline` → `(on, duration_ms)` segments) is isolated from PCM so
element/gap timing is exactly assertable; `encode` keys `synth_tone` on/off along it, with
canonical-zero silence for gaps (so concat stays format-identical). Unknown chars **fail loud**
(a wrong ID is worse than a loud failure). WPM/sidetone are **marked-default** config
(`RADIO_CW_WPM`=20, `RADIO_CW_TONE_HZ`=600, guardrail 1) — safe operator prefs, but **on-air CW
readability is an empirical bring-up check, not proven here.** `uv run pytest` → **131 total,
all green**. Still deferred: `VoiceId`, session-lifecycle wiring.

Cycle 5 complete: the **audio format is pinned and load-bearing** (guardrail 1; ADR 0006).
The opaque `AudioFrame = bytes` alias is gone — `AudioFrame` now carries its `AudioFormat`
(rate/width/channels) and **fails loud** (`AudioFormatMismatch`) on a mismatched concat or
transmit, closing the cycle-1 "bytes silently papers over a mismatch" risk by construction.
Canonical internal format is **48000 Hz / s16le / mono**; resampling happens only at the
tolerant software edges via `soxr` (VHQ, anti-aliased so a downsample can't corrupt DTMF). A
real `synth_tone` primitive (sine + raised-cosine anti-click envelope) proves the type with
real PCM and is the CW-ID substrate for cycle 6. **The remaining gate before hardware is now
just the real encoders (CW/voice ID, piper TTS) + empirical bring-up — the format no longer
blocks anything.**

Cycle 4 (merged, PR #4): automatic station ID (guardrail 5, Part 97). The transmit path is
**legality-clean** — every service transmission carries the station ID, there is a
forced-periodic ID timer, and a sign-off ID at session end. `StationId` is the single seam
through which all audio reaches the radio, so no transmission can go out un-ID'd. ID audio
is a deterministic stub (scheduling logic only). See ADR 0005.

Cycle 3 (merged, PR #3): command dispatch + the first voice service (announce-the-time),
the first thing the server transmits. Authenticated digit `"1"` → time announcement
rendered through a stub TTS → `MockRadio.tx_log`. Still fully mock/hardware-free;
unit-tested with the injected fake clock. See ADR 0004.

Cycle 2 (merged, PR #2): a DTMF-gated TOTP auth layer + session state machine, fed digit
strings directly (no audio/DTMF decode yet), unit-tested with an injected fake clock.
See ADR 0003.

Cycle 1 (merged, PR #1): the `Radio` protocol surface + full `MockRadio`, hardware
backends stubbed and wired into a factory. See ADR 0002.

### Controller loop (cycle 12)

- `radio_server/controller/` (new package). `engine.py` — the pure core, thin driver, and root:
  - `Controller.step(now, rx_audio) -> StepResult` — one loop iteration: `DtmfInput.pump` → for each
    entry `AuthGate.on_dtmf` (an `ACCEPTED` → `station.begin_session` + emit `session_open`); then
    `gate.expire_if_idle` (True → `station.sign_off` + emit `session_close`), else if authenticated
    `station.check(now)` (True → emit `id`); then tick an attached `scan`. `StepResult(entries,
    outcomes, session_open, id_sent, signed_off, scanning)`. Order is load-bearing (a session opened
    this step is not idle, so no false close). `on_event` + `scan` are public/reassignable.
  - `ControllerRunner.run()` — `while running: step(clock(), radio.receive()); await sleep(poll)`;
    `stop()` flips the flag. Thin shell, no logic not covered by `step`. Guardrail-1 poll cadence.
  - `ControllerEvent(phase, data)` with `CONTROLLER_PHASES = ("session_open","id","session_close")`.
  - Config: `load_controller_poll` / `load_session_timeout` (`_load_positive_float` shape,
    verify-on-hardware on the poll constant); `build_controller(env, *, radio, decoder, tts, clock)`
    assembles encoder→`StationId`→registry/time-service→`Dispatcher`→verifier/`AuthGate`→`DtmfInput`,
    sharing the **one** `StationId` with the dispatcher. Fail-loud on the TOTP secret / callsign.
- **Layering:** imports only `..audio/auth/services/scan/backends` (all below `api`), emits via the
  injected `on_event` — never imports `EventHub`. `api/app.py` adapts each `ControllerEvent` to
  `Event("session", {"phase":…, …})`, so the arrow stays `api → controller`.
- `auth/session.py` — extracted `AuthGate.expire_if_idle(session, now) -> bool` (returns whether it
  closed an idle authed session); `on_dtmf` now calls it. **Behavior identical** — the seam a polling
  loop needs, since `on_dtmf`'s inactivity demotion is otherwise only reachable by feeding a key.
- `api/app.py` — `create_app(radio, *, api_token, controller=None, runner=None)` rebinds
  `controller.on_event` to the hub adapter and stores both on `app.state`. `POST /controller {on}`
  (token-gated) starts/stops an `asyncio` task running `runner.run()`; **503** when unconfigured.
  `/status` merges a `controller` block (`{running, session_open}` or `null`). `build_app` wires a
  controller only when `RADIO_TOTP_SECRET` is set (prior no-hardware contract preserved).
  `api/events.py` docstring/`EVENT_TYPES` comment updated for the now-live `"session"` type
  (`EventHub` unchanged).
- Tests: `tests/test_controller.py` (12 new) — login opens+arms; authed `"1"` lands a CW-ID'd time
  announcement in `tx_log`; forced periodic ID at the interval; inactivity timeout closes + signs
  off; an attached scan ticks each step and holds on scripted busy; lifecycle events in order; a
  bounded `run()` pumps `step` each iteration; `POST /controller` flips `/status.running` + needs a
  token; `503`/null when unconfigured; `session` events over the WS in order. Plus
  `tests/test_session.py` `expire_if_idle` cases. `uv run pytest` → **227 passed, 4 skipped**. See
  ADR 0013.
- **Deferred (next):** the two hardware backends; optionally starting a *live* scan through the
  controller (the synchronous `/scan` sweep stays); running `receive()` in a thread executor.

### Software scan engine (cycle 11)

- `radio_server/scan/` (new package). `engine.py` — the pure engine + plan + config:
  - `ScanPlan` (frozen): `channels: tuple[int, ...]` (Hz), `lockout: frozenset[int]`,
    `priority: int | None`; `from_frequencies(...)` / `from_range(start, stop, step)`;
    `active_channels()` = order minus lockout. Addresses by **frequency**, not channel number.
  - `ResumeMode` (`carrier` default | `timed` | `hold`); `ScanEvent(phase, frequency, channel)` with
    `SCAN_PHASES = ("scanning", "active", "dwelling", "resumed")`.
  - `ScanEngine(radio, plan, *, on_event, mode, dwell, settle, clock)` — raises
    `UnsupportedCapability(Capability.SCAN)` on an audio-only backend. `tick(now)` is the clock-driven
    machine (settle → poll `status().busy` → dwell/resume/hold/advance, wraps); `sweep()` is the
    synchronous stop-and-hold pass the API uses (no clock, no sleeps). Pure helpers shared by both.
  - Config (guardrail-1 marked, verify-on-hardware on the constant): `load_scan_settle` /
    `load_scan_poll` / `load_scan_dwell` (`_load_positive_float` shape) + `load_scan_mode` (enum,
    fail-loud on unknown); `build_scan_engine(env, *, radio, plan, on_event, clock)` composition root.
- **Layering:** the engine imports only `..backends` and emits via the injected `on_event` — it does
  **not** import `EventHub`. `api/app.py` adapts each `ScanEvent` to `Event("scan", {...})` on the
  hub, so the arrow stays `api → scan`. `api/events.py` only gained `"scan"` in `EVENT_TYPES`
  (`EventHub` itself unchanged, as ADR 0011 promised).
- `api/app.py` — `POST /scan` on the token-gated router: `_require_cat(Capability.SCAN)` → `501`
  naming `"scan"` on audio-only (same body as the other CAT endpoints); else build a plan from
  `frequencies` **or** a `start/stop/step` range (exactly one, else `422`), run `engine.sweep()`,
  publish `scan` events, return `{"held", "status"}`. Live real-time pump **deferred** to the
  controller-loop cycle (like cycle 7's DTMF pump).
- `backends/mock.py` — `MockRadio` gained `busy_frequencies` (public mutable set): `status().busy`
  is true while tuned to a listed freq, on top of the flat `busy` flag (back-compat kept). This is
  the hook that scripts "channel X busy" and drops a carrier mid-scan (`.discard(x)`).
- Tests: `tests/test_scan.py` (26 new) — plan/config; capability gate; sweep holds first active /
  all-clear → None / lockout skips / priority peeked-and-held; tick carrier-resume, timed-move-on,
  hold-stops, settle-gates-the-poll; events in phase order; and the API (`/scan` sweeps on CAT,
  publishes `scan` over WS in order, `501`-naming-`scan` + unadvertised on audio-only, `422` on a bad
  body, `401` without a token). Plus `tests/test_mock_radio.py` busy_frequencies cases. `uv run
  pytest` → **213 passed, 4 skipped**. See ADR 0012.
- **Deferred (next):** the controller/API pump loop that ticks `ScanEngine` + `DtmfInput.pump` + the
  ID session lifecycle on a live `receive()` loop; then the two hardware backends.

### FastAPI API layer (cycle 10)

- `radio_server/api/` (new package). `app.py` — `create_app(radio, *, api_token) -> FastAPI` (the
  DI seam tests drive against `MockRadio`) and `build_app(env)` (the project's first top-level
  composition root: `create_radio(env["RADIO_BACKEND"] or "mock")` + `load_api_token(env)`, mirrors
  `build_id_encoder`). REST routes live on an `APIRouter` gated by a bearer-token dependency; CAT
  routes call `_require_cat(Capability.…)` before dispatching → `501` `{"error":…, "capability":…}`
  when absent (also catches `UnsupportedCapability` to the same body). `POST /transmit` wraps the
  raw request body in a canonical `AudioFrame` → `radio.tx_log`.
- `api/auth.py` — the LAN plane, **separate from `radio_server.auth`**. `RADIO_API_TOKEN` +
  `load_api_token` (fail-loud no-default, mirrors `load_totp_secret`); `token_matches`
  (`hmac.compare_digest`, constant-time); `bearer_token` (parses `Authorization: Bearer …`);
  `make_require_token(expected)` (the FastAPI 401 dependency). No `TotpVerifier`/`Session` reuse —
  static secret, no window/burn.
- `api/events.py` — `Event(type, data)` (`type` ∈ `status|ptt|busy|session`, `scan` reserved),
  `EventHub` (in-process asyncio fan-out: `subscribe`/`publish`/`unsubscribe`), `status_event(radio)`.
  WS `/events?token=…` accepts, sends an initial `status` snapshot, then streams published events;
  bad token → close `1008`.
- Decisions (see ADR 0011): `501` over `409` for gated CAT (permanent not-implemented, not a state
  conflict); token via `?token=` on the WS because browsers can't set WS handshake headers;
  FastAPI/uvicorn **core** (tests run) with httpx in the dev group (TestClient only).
- Tests: `tests/test_api.py` (18 new, `TestClient` over `MockRadio`, both `supports_cat` values) —
  `/status` mirrors state; `/capabilities` tracks `supports_cat`; a CAT route works on a CAT backend
  and returns a `501` naming the capability **with backend state unchanged** on an audio-only one;
  ptt/transmit reach the mock; WS emits a `status` event on connect and a `ptt` event on control;
  auth rejects missing/bad and accepts good; `load_api_token({})` raises. See ADR 0011.
- **Deferred (next):** the V71-only scan engine, which publishes `scan` progress on this
  `EventHub`, plus session-lifecycle wiring surfaced as `session` events on the same stream.

### VoiceId + configurable ID mode (cycle 9)

- `radio_server/services/voice_id.py` (new):
  - `PHONETIC: dict[str, str]` — NATO/ITU A-Z, digits 0-9 with the ham **9→"niner"**, and
    `/`→"slash". Accepted set matches `CwId`'s `MORSE`, so ID mode never changes which
    callsigns encode.
  - `spell_callsign(callsign) -> str` — pure; upper-cases, maps each char, joins with spaces.
    **`ValueError`** on any char outside `PHONETIC` (mirrors `CwId._morse_for`). Engine-free,
    so the map is exactly assertable.
  - `VoiceId` — `__init__(tts)` (DI at construction); `encode(callsign, format=CANONICAL)` →
    `tts.render(spell_callsign(callsign))`. Optional `format` honors the `CwId` shape so
    `isinstance(VoiceId(stub), IdEncoder)` holds and `StationId`'s one-arg call is unaffected.
  - `load_id_mode(env)` / `RADIO_ID_MODE_ENV_VAR` / `DEFAULT_ID_MODE="cw"` — marked-default
    (like `load_id_interval`); a set value outside `{cw,voice}` fails loud.
  - `build_id_encoder(env, *, tts=None)` — the ID composition root. `cw` → `CwId(wpm/tone from
    loaders)`; `voice` → `VoiceId(tts or PiperTts(load_tts_voice(env)))`. Voice mode with no
    voice **raises** (no CW fallback). The `tts` injection lets tests pick voice on `StubTts`.
- `radio_server/services/__init__.py` re-exports `VoiceId`, `spell_callsign`, `PHONETIC`,
  `RADIO_ID_MODE_ENV_VAR`, `DEFAULT_ID_MODE`, `ID_MODES`, `load_id_mode`, `build_id_encoder`.
  No new deps (voice mode reaches piper only via the cycle-8 optional `tts` extra).
- `tests/test_voice_id.py` (17 new) — phonetic map (spell, upper-case, slash, unknown→raise);
  `VoiceId` on `StubTts` byte-exact + canonical + protocol; `RADIO_ID_MODE` selection (default
  cw, reads voice, case-insensitive, unknown→raise); `build_id_encoder` cw/voice + voice-
  without-voice fail-loud-no-fallback; end-to-end authed `"1"` → voice-ID + time in `tx_log`
  (exact); 1 `skipif`-gated real-piper test (property-asserted). `uv run pytest` →
  **169 passed, 4 skipped**. See ADR 0010.
- **Deferred (next):** the FastAPI API layer, the V71-only scan engine, and the two real
  hardware backends. The audio-content tower is done.

### Real piper TTS (cycle 8)

- `radio_server/services/tts.py` (modified) — `PiperTts` added beside the **unchanged**
  `TtsEngine` protocol and `StubTts`:
  - `__init__(voice_path, *, config_path=None)` — default sidecar `<voice>.onnx.json` (piper
    convention, marked verify-against-build). Validates the `.onnx` + sidecar exist and reads
    `audio.sample_rate` into `self._rate`, all fail-loud, **without importing piper**.
  - `render(text) -> AudioFrame` — `to_canonical(AudioFrame(raw, AudioFormat(self._rate,
    2, 1)))`. Canonical 48k out regardless of the voice's native rate.
  - `_synthesize_raw(text)` — the **only** piper-touching seam (lazy import, marked
    VERIFY-AGAINST-INSTALLED-BUILD; missing piper/onnxruntime → fail-loud RuntimeError). A
    test subclass overrides it to drive `render` with a synthetic buffer, no model needed.
  - `load_tts_voice(env)` / `RADIO_TTS_VOICE_ENV_VAR` — fail-loud, **no default** (modeled on
    `load_totp_secret`).
- `radio_server/services/__init__.py` re-exports `PiperTts`, `load_tts_voice`,
  `RADIO_TTS_VOICE_ENV_VAR`. `pyproject.toml` gains an optional `tts` extra
  (`piper-tts`, `onnxruntime`) — declared, not core; piper unpinned (guardrail 1).
- `tests/test_tts.py` — the 5 existing StubTts baseline tests kept; +9 model-free PiperTts
  tests (config fail-loud ×4, rate read from sidecar, non-22050→48k and 22050→48k resample
  edge, protocol conformance) + 2 `skipif`-gated real-engine tests (canonical/nonzero/
  plausible-duration speech; wired into the time service → one canonical over with the CW ID
  prepended, structure asserted). `uv run pytest` → **152 passed, 3 skipped**. No new core
  deps. See ADR 0009.
- **Deferred (next):** `VoiceId` — a second `IdEncoder` speaking the callsign through this
  engine, with the phonetic/"niner" spelling map and `StationId` CW-vs-voice encoder
  selection. ID stays CW this cycle.

### DTMF decode + framing (cycle 7)

- `radio_server/audio/dtmf.py` (new) — two deliberately-distinct concerns plus fixtures:
  - **Decode:** `DtmfDecoder` (one-method `runtime_checkable` protocol, `decode(frame) -> str`,
    mirrors `IdEncoder`) and `MultimonDtmfDecoder` — `to_multimon(frame)` (ADR 0006 anti-alias
    edge) → pipe raw PCM to `multimon-ng` on stdin → parse `DTMF: <key>` lines. Missing binary
    fails loud with an install hint. `MULTIMON_ARGS`/`MULTIMON_RATE`/`RADIO_MULTIMON_BIN` are
    marked verify-against-build (guardrail 1).
  - **Framing:** `DtmfFramer` (pure, clock-injected). `feed(digit, now) -> str | None`: `#`
    emits the buffered run as one entry (empty buffer → nothing), `*` clears, any other key
    appends; inter-digit timeout discards a stalled partial (lazy on `feed`; `tick(now)` for a
    future real loop). Local `Clock` alias — the module imports no auth code.
  - **`DtmfInput`** composes decoder+framer: `pump(frame) -> list[str]` of completed entries.
    Auth-free; the caller feeds entries to `on_dtmf`.
  - **Fixtures:** `synth_dtmf(digit, …)` sums two `synth_tone` frames at `DTMF_FREQS` (standard
    697–1633 Hz pairs), `_mix` sums int16 as int32 + clips. Deterministic, FFT-assertable, no
    external assets. Unknown key fails loud.
  - **Config:** `load_dtmf_timeout` (`RADIO_DTMF_TIMEOUT`, default 3.0s, fail-loud on bad set
    value) and `load_multimon_bin` (`RADIO_MULTIMON_BIN`, default `multimon-ng`).
- `radio_server/audio/__init__.py` re-exports the new surface.
- `tests/test_dtmf.py` (13 new) — synth-fixture FFT (both tones present)/format/determinism/
  fail-loud; `skipif`-gated real multimon decode; framing (full run frames one entry, `*`
  clears, timeout discards partial via `FakeClock`, lone `#` no-op, `tick`); and **the**
  end-to-end (fake decoder → framed TOTP → `ACCEPTED` → authed `"1"` → CW-ID'd time in
  `tx_log`). `uv run pytest` → **143 passed, 1 skipped**. No new deps. See ADR 0008.
- **Deferred (empirical/next):** real recorded-WAV fixtures; a controller/API loop that pumps
  `radio.receive()` and calls `on_dtmf`; weak-signal/HT-flutter robustness + exact multimon
  flags (hardware bring-up); `VoiceId`.

### Real CW station ID (cycle 6)

- `radio_server/services/cw.py` (new) — `CwId` implements `IdEncoder`
  (`encode(callsign, format=CANONICAL_FORMAT) -> AudioFrame`). Built lowest-to-highest so the
  timing is pure: `MORSE` table (A–Z, 0–9, `/`); `unit_ms(wpm) = 1200/wpm`;
  `cw_timeline(text, wpm)` → ordered `(on, duration_ms)` segments using PARIS units
  (dit 1 / dah 3 / intra-char 1 / inter-char 3 / inter-word 7), **no leading/trailing gap**;
  `_silence` builds canonical-zero gap frames. `encode` keys `synth_tone` for each on-segment
  (its raised-cosine ramp kills per-element clicks) and concatenates via `AudioFrame.__add__`.
- **Encoder signature note:** the protocol is one-arg (`encode(callsign)`) and `StationId`
  calls it that way; the cycle-6 `encode(callsign, format)` shape is honored by an **optional**
  `format` param defaulting to canonical, so nothing above the seam changes and
  `isinstance(CwId(), IdEncoder)` still holds.
- Config: `load_cw_wpm`/`load_cw_tone_hz` follow the `load_id_interval` pattern —
  `RADIO_CW_WPM` (default 20) / `RADIO_CW_TONE_HZ` (default 600), marked defaults that still
  **fail loud** on a set non-numeric/non-positive value. WPM/tone injected into `CwId` at
  construction. Guardrail 1: safe operator prefs, not confirmed hardware facts.
- Swap point: `StubId()` → `CwId(...)` at the (still-to-be-written) composition root; nothing
  else changes.
- Tests: `tests/test_cw.py` (21 new) — PARIS `unit_ms`, exact `cw_timeline("AE9S", …)`
  dit/dah/gap sequence, total-duration = timing math, per-segment tone-energy/exact-zero-gap
  render check, sidetone FFT, unknown-char raises, canonical + concat, config loaders, and
  end-to-end via `StationId`/auth gate (authed `"1"` prepends real CW, no within-interval
  repeat — cycle-4 scheduler behavior unchanged). No new deps. See ADR 0007.

### Audio format + resample + tone (cycle 5)

- `radio_server/audio/` (new lowest layer). `format.py` — `AudioFormat(rate,width,channels)`
  and the frozen, format-carrying `AudioFrame(samples, format=CANONICAL_FORMAT)`; `__add__`
  and `MockRadio.transmit` raise `AudioFormatMismatch` on a format mismatch. Canonical =
  `AudioFormat(48000, 2, 1)`. The guard is **format identity, not PCM-length divisibility**,
  so the symbolic stubs (`b"<id:AE9S>"`) stay valid frames and `tx_log` stays assertable.
- `audio/resample.py` — `resample(frame, target_rate)` over `soxr` VHQ (anti-aliased),
  plus `to_multimon` / `to_canonical`. `MULTIMON_RATE = 22050` is a **verify-on-hardware**
  marked default (guardrail 1). Mono 16-bit only for now (raises otherwise).
- `audio/tone.py` — `synth_tone(freq_hz, duration_ms, format=CANONICAL_FORMAT, *,
  amplitude=0.5, ramp_ms=5.0)`: real sine PCM with a raised-cosine on/off envelope (no key
  clicks). Deterministic. This is the substrate CW ID (cycle 6) gates on/off.
- `AudioFrame` moved from `backends/base.py` to `audio/format.py`; `backends` re-exports it,
  so `from ..backends import AudioFrame` still works everywhere. `MockRadio` gained a
  `format` and a transmit guard; `StubTts`/`StubId` now wrap their symbolic payload in a
  canonical frame. New deps: `numpy`, `soxr` (first runtime deps beyond `pyotp`; wheels only).
- Tests: `test_audio_format.py`, `test_resample.py` (in-band survives + no aliasing into the
  DTMF band), `test_tone.py`; existing suites updated for the new frame type. `uv run pytest`
  → **110 total, all green**. See ADR 0006.

### Station ID scheduler (cycle 4)

- `radio_server/services/station_id.py` — `StationId(radio, encoder, callsign, *,
  interval=600, clock)` is the sole `radio.transmit` seam. `transmit(audio)` prepends the ID
  into the same over when *due* (due = first over of the session, i.e. `last_id is None`, OR
  `now - last_id >= interval`); within-interval overs do not repeat it. `check(now)` forces
  an ID-only over when the session is overdue (safety net for a real scheduler task).
  `sign_off(now)` sends a closing ID iff the station transmitted, then resets.
  `begin_session(now)` resets per-session state (for the inactivity-timeout path). The timer
  is measured from `last_id`, not the last transmission — the Part 97 invariant is "≤10 min
  since the last ID."
- Config mirrors the auth pattern: `load_callsign()` reads `RADIO_CALLSIGN` and **fails loud
  (no default)** — a station cannot legally transmit without a callsign (Kris sets `AE9S`).
  `load_id_interval()` reads `RADIO_ID_INTERVAL` (default 600) and **rejects** any value
  > 600 (legal max 10 min), non-numeric, or non-positive.
- `IdEncoder` protocol (`encode(callsign) -> AudioFrame`) + `StubId` (deterministic
  `b"<id:AE9S>"`, so `tx_log` is assertable). Real `CwId`/`VoiceId` are later cycles.
- `radio_server/services/dispatch.py` — `Dispatcher` now holds a `StationId` (`transmitter`)
  instead of a raw `Radio`, so no service transmission can bypass ID by construction.
- `tests/test_station_id.py` (23 new tests) + updated `tests/test_dispatch.py` (first over
  now asserts the ID prefix). `uv run pytest` → **88 total, all green**. No new deps.

### Dispatch + services (cycle 3)

- `radio_server/services/dispatch.py` — `Service = Callable[[Session, ServiceContext],
  AudioFrame]` (handlers *produce* audio, no radio I/O). `ServiceContext(clock, tts)` is
  radio-free. `ServiceRegistry` maps digit → `(name, Service)`. `Dispatcher(radio, ctx,
  registry)` is *callable* matching the auth layer's `Dispatch` contract, so it drops into
  `AuthGate(verifier, ..., dispatch=dispatcher)`; it owns the radio and is the single
  `transmit` seam. Returns `DispatchResult(digits, service, transmitted)` (unknown digit →
  `transmitted=False`, nothing sent — graceful, `Outcome.kind` stays `COMMAND`).
- `radio_server/services/tts.py` — `TtsEngine` protocol (`render(text) -> AudioFrame`) +
  `StubTts` (deterministic `b"<audio:...>"`, so `tx_log` is assertable). Piper is later.
- `radio_server/services/time_service.py` — `format_spoken_time(now, tz)` (pure, 24-hour
  local, isolated from dispatch); `load_timezone()` reads `RADIO_TZ` (IANA name) with a
  marked `UTC` default (bad zone → raises); `time_service(tz)`/`register(registry, tz)`
  bind digit `"1"`. Reads the SAME injected clock as the session timeout.
- `radio_server/services/__init__.py` — public surface re-exports.
- `tests/test_tts.py`, `tests/test_time_service.py`, `tests/test_dispatch.py` — 16 new
  tests (incl. full enroll→auth→`"1"`→exact `tx_log` on a fake clock). `uv run pytest` →
  65 total, all green. No new dependencies (stdlib `zoneinfo`/`datetime`).

### Auth layer (cycle 2)

- `radio_server/auth/totp.py` — `TotpVerifier`. `verify_and_burn(code, now=None)`:
  ±1-step windowed (== pyotp `valid_window=1`), constant-time compare, **single-use**
  (burns each consumed `(code, time_step)`; a replay inside the window is refused).
  Burn set is pruned each call so it stays bounded. `provisioning_uri()` emits the
  `otpauth://` enrollment URI. `load_totp_secret()` reads `RADIO_TOTP_SECRET` (env,
  never hardcoded) and raises if unset. `Clock = Callable[[], float]` alias, injectable.
- `radio_server/auth/session.py` — two-state machine (`SessionState`:
  UNAUTHENTICATED ⇄ AUTHENTICATED). `AuthGate.on_dtmf(digits, session, now=None)` is
  the single entry point → `Outcome(kind, detail)` where `OutcomeKind` ∈
  {ACCEPTED, REJECTED, COMMAND}. Inactivity `timeout` (injectable) drops the session.
  Unauth → TOTP verify; authed → injected `dispatch` hook (stubbed; cycle 3).
- `radio_server/auth/__init__.py` — public surface re-exports.
- `tests/conftest.py` — `FakeClock`, shared `TEST_SECRET`/`verifier`/`code_for`.
- `tests/test_totp.py`, `tests/test_session.py` — 22 new tests. `uv run pytest` → 49
  total, all green.
- ADR 0003 records the state machine, single-use burn strategy, and clock injection.
- `pyproject.toml` now depends on `pyotp>=2.9` (see `uv.lock`).

## Next up

The **entire software tower is now built and runs live end-to-end on the mock**, both halves of the
voice relay stream — receive (cycle 13, squelched cycle 14) and transmit (cycle 15) — and the
**half-duplex conflict between them is now arbitrated** (cycle 16: TX takes the radio, RX + scan
stand down and resume). **Cycle 16 was the last pure-software cycle.** What remains needs the box
(or is optional polish):

- **Optional software polish (mock-testable).** The `/events` **"suspended" marker** — surfacing
  the arbiter's mode to listeners on `/events` when RX pauses/resumes — is a cheap observability add
  cycle 16 deferred (the behavior is delivered; the marker is a nicety). **Opus/compression** on
  `/audio/rx` and `/audio/tx` remains a noted-not-built option for constrained links. And the RX
  pump is still a **second** `receive()` reader — consolidating it with the controller's reader (one
  capture fanned to both) is a bring-up decision (and would let the arbiter also gate the controller
  reader, not just the pump).
- **Real hardware backends** (`SignaLinkV71`, `AiocBaofeng`) — the last thing that needs hardware,
  and the "plug it in, it keys up clean" empirical bring-up phase. This is where the marked
  verify-on-hardware facts get confirmed: the Hamlib rig model + serial speed (V71 CAT), the AIOC's
  PTT line (RTS vs DTR), multimon-ng's exact input rate/flags, the piper voice, and the controller's
  real `receive()` cadence / audio chunk size / loop timing (guardrail 1). PTT stays off the DATA
  port / AIOC serial line, never CAT `TX` (guardrail 2).
- **Live scan through the controller (optional).** `Controller` ticks an *attached* `ScanEngine`, but
  nothing starts one over the API yet; the synchronous `/scan` sweep still stands. A later cycle could
  add start/stop-scan control that installs a live engine on the running controller and streams
  carrier/timed dwell over wall-clock.
- **`build_app` production wiring / the real entrypoint.** `build_app` wires the controller only when
  `RADIO_TOTP_SECRET` is set, and full wiring needs real multimon + a piper voice — that comes online
  with the hardware phase. No `uvicorn` entrypoint binds a server yet.
- **More services / auth strength per service (guardrail 4).** The time announce is read-only; guard
  anything that keys TX for real harder. `ServiceContext` is the place to thread per-service
  authority if needed.
- **Runtime hardening for the async driver.** On hardware, `receive()` blocks — run it in a thread
  executor rather than directly in the event loop; and the single-use TOTP `consumed` set is
  per-process in-memory (noted in ADR 0003).

## Open questions / blocked

(none)

## Notes for the cycle runner

- Single-use `consumed` state is in-memory per process; a restart mid-window or a
  multi-process deployment would need it shared/persisted. Out of scope now; noted in
  ADR 0003.
- There is no GitHub instruction issue in this repo — cycles have arrived via the
  prompt. The CLAUDE.md "comment PR URL / swap label on the issue" close step has no
  issue to act on; PRs are still opened for human merge as required.
