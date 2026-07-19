# 0091 — D-STAR crossband safety: the over always closes, PTT always drops, the reflector is signal-gated

Status: Accepted

## Context

Folding D-STAR onto the live radios (ADR 0089) and taking it to a real reflector exposed two failures —
one dangerous, one merely wrong:

1. **The reflector→RF over could get stuck keying the transmitter.** With a reflector linked, the radio
   sat on the air transmitting dead air, and **neither `POST /dstar/unlink` nor `bridge.stop()` dropped
   PTT — only killing the process did.** Root causes, all in `dstar/bridge.py`:
   - `_reflector_to_rf` is the **only** PTT-drop path (via `_end_rx`). It closes an over on an end-bit,
     a `tx_hang` **queue-idle** timeout, or a new header. When inbound DATA keeps arriving (queue never
     idles) while decodes stop feeding RF — the DV Dongle desyncing mid-over — none of those fire, so
     the over never closes.
   - `_teardown` never dropped PTT directly; it relied on the `_reflector_to_rf` `finally`. But that
     task can be **parked in a blocking vocoder decode** (`run_in_executor`), so `task.cancel()` can't
     be delivered, the `await task` join **hangs**, and the `finally` never runs. Worse, the vocoder
     whose `close()` would unblock the parked `_exchange` was closed **after** the join — a deadlock.
   - The DV Dongle serial port had no `write_timeout`, so a full FTDI buffer could block a write
     unbounded.

2. **The RF→reflector crossband keyed the reflector on receiver hiss.** `_rf_to_reflector` opened an
   over on *any* `AudioHub` frame, so with `audio.squelch=off` it transmitted continuous noise onto the
   reflector. The only mitigation was setting the **global** `audio.squelch=audio`, coupling the
   browser-listen path's gating to the reflector-keying decision.

ADR 0090 added a transmitter time-out timer (TOT) as the last-resort backstop beneath all of this. This
ADR fixes the crossband itself so an over closes in ~`tx_hang`, long before the TOT would ever fire.

## Decision

Five changes on the `dstar/bridge.py` seam (plus one serial and one wiring change); nothing new on the
gateway. No new config — the crossband gate reuses the existing `audio.vad_*` thresholds.

- **`_teardown` drops PTT directly and can't hang.** It now (1) closes the vocoder **first**, so a task
  parked in an executor decode/encode unblocks (the DV Dongle `close()` notifies the waiting exchange)
  and the cancel becomes deliverable; (2) calls a new **`_force_unkey`** that drops PTT
  (`rx_session.close()` → `radio.ptt(False)`), releases the slot, and clears the latch **directly** —
  never relying on the loop's `finally`; (3) bounds each task join with `asyncio.wait_for`, so a still-
  wedged worker can never hang the teardown (PTT is already down).
- **An independent RX PTT watchdog.** At the top of each `_reflector_to_rf` iteration, if the keying
  session is idle past `tx_hang` (`TxSession.idle_elapsed()` — no successful RF feed), the over is
  closed. The loop still cycles on inbound DATA, so this fires even when the queue never idles (the
  incident); a healthy over refreshes the deadline on every fed frame, so it is never cut short.
- **A `write_timeout` on the DV Dongle serial port** (`vocoder/dvdongle.py`), so a stuck write raises a
  bounded codec error the bridge already handles instead of parking forever.
- **The RF→reflector crossband is signal-gated in the bridge, independent of `audio.squelch`.** A per-
  link `AudioLevelGate` (reusing `frame_rms` and the `audio.vad_*` thresholds) gates
  `_rf_to_reflector`: a below-threshold frame never opens an over and closes an open one on the gate-
  close edge (the gate's hang bridges word gaps). `build_app` injects a fresh gate factory; the
  `create_app` DI seam stays ungated, so the crossband keys the reflector only on real RF audio whether
  the operator runs `audio.squelch` off or on.
- **Slot accounting + an operator-over backstop.** `_open_rx_session` records whether it actually
  acquired the shared `TxSlot`, and `_end_rx`/`_force_unkey` release it **only if held** — never freeing
  a slot a concurrent browser-TX talker owns. A new `_reap_stale_tx` on the RF pump's silence path
  closes a leaked "op" over (a browser mic WS that died without `end_operator_over`) so a dropped socket
  can't wedge the TX latch.

## Consequences

- **The transmitter can no longer be left keyed by the crossband.** A stalled/failing decode, a lost
  end-bit with live inbound, a teardown mid-over, or a parked executor decode all now drop PTT within
  ~`tx_hang` (and the ADR 0090 TOT remains the absolute backstop). `unlink`/`stop()` always unkey and
  always return.
- **The crossband keys the reflector only on real signal**, regardless of the global squelch mode — so
  the operator can keep `audio.squelch=off` for the FM/browser path without jamming the reflector on
  hiss.
- **No new config, no canary/example change.** The gate reuses `audio.vad_*`; the DI seam behaviour is
  unchanged (a bare `DStarBridge` and `create_app(...)` stay ungated, so every existing test holds).
- **Verified on fakes, never by live-keying.** New `test_dstar_bridge.py` scenarios cover: the idle
  watchdog closing an over under continuous inbound with failing decodes; `stop()` during a **blocked**
  decode dropping PTT and returning (bounded, no hang); the RF gate opening only on a loud frame; the rx
  session not releasing a slot another talker holds; and a leaked operator over being reaped on RF
  silence. `uv run pytest`: 1223 passed.
- **Re-enable is gated on a bench proof.** D-STAR stays disabled on the live radios (`[dstar]
  callsign=""`) until this merges and is proven on the bench — `doctor --dstar-browser-echo` on the real
  dongle for the audio round trip, and a first reflector→RF test with the radio on a **dummy load** to
  watch the over close and PTT drop — before it goes back on the antenna. See HANDOFF and the
  `dstar-stuck-key-incident` note.

Cross-refs: ADR 0090 (the TOT backstop beneath this), ADR 0089 (the folded crossband whose stuck-key
incident this fixes), ADR 0087 (the bridge + half-duplex latch), ADR 0086 (the vocoder + no-interleave
rule), ADR 0015 (the `audio.vad_*` activity gate reused here).
