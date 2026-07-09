# 0020 — Audio recording: received audio to WAV via a passive frame-tap

Status: Accepted

## Context

The stack captures and streams received audio (cycle 13 `RxPump`/`AudioHub`), gates it with a
software squelch (cycle 14 `RxActivityGate`), and logs a structured QSO ledger (cycles 17–18). What
it could not do is **keep the audio**: there was no way to record what was received to disk. This
cycle adds that — a passive `Recorder` that writes received audio to timestamped WAV files, one file
per RX activity session, so a recording pairs naturally with the scan/session ledger records that
share its timestamp. It is **mock-only** (guardrail 1): scripted `MockRadio` RX frames + `FakeClock`
+ `tmp_path` drive it end to end; no hardware.

Two facts shaped the design.

- **The `AudioHub` only ever carries gate-open frames.** Gating happens upstream in `RxPump.run`
  (`frame.samples and self._gate(frame)` → `hub.publish`), so anything tapping the streamed flow sees
  only live, post-squelch audio. "Record gated audio" is therefore automatic on any tap — no separate
  gate needs to be consulted.
- **No gate-close edge is observable at the hub.** A hub subscriber sees frames appear and then simply
  *stop* — "gate closed," "slow producer," and "half-duplex TX pause" are indistinguishable from the
  subscriber's side. But segmentation — *one WAV per gate-open → gate-close activity session* — needs
  that edge. Deriving it downstream would take a wall-clock gap timeout (not `FakeClock`-deterministic,
  so not unit-testable with the existing harness) or a second edge channel racing the queued frames.

## Decision

- **Tap the pump synchronously, not the hub.** The brief said "add the recorder as another sink
  alongside the WS listeners," but the pump — not the hub — is the one place the frame bytes **and**
  the gate boolean coexist in call order. So the recorder is a `Protocol`-injected sink the pump calls
  directly: gate-open frame → `recorder.write(samples)`; a non-empty frame the gate **rejects** →
  `recorder.end_segment()` (the gate-close edge); empty transport-skip frames reach neither. This
  honors the brief's intent (it sees exactly the post-gate frames the hub streams, opens **no second
  capture reader** — the single-reader discipline from the arbiter era stands — and is drop/catch
  non-blocking) while making segmentation fall out for free and deterministically testable. The hub
  `publish` runs **first**, then the recorder, so recording can never add latency to the live stream.

- **A new pure-leaf `radio_server/recording/` package**, the structural sibling of `eventlog/`. It
  imports only `..audio`; `rx/pump.py` defines its own narrow `RxRecorder` Protocol (`write`,
  `end_segment`) + a `null_recorder` no-op default and never imports `recording/`; `api/app.py` is the
  only place the two meet. This mirrors how `RxActivityGate` is a Protocol in `pump.py` with the
  concrete gate in `activity/`, and how cycle 18 injected `on_change`/`on_key` callbacks wired only at
  the composition root — the arrows stay acyclic.

- **`Recorder` writes WAV via the stdlib `wave` module** — no new dependency. Because the canonical
  format is fixed (48000 Hz / s16le / mono), the header is deterministic: each segment is a fresh
  `wave.open(path, "wb")` with the canonical channels/width/rate, then `writeframes` per frame.
  `writeframes` patches the RIFF/`data` sizes on every call, so even an abrupt kill leaves a playable
  WAV; `close()` finalizes. Segment filenames are `rx-{seq:06d}-{YYYYmmddTHHMMSSZ}.wav`: a per-instance
  **sequence counter is the uniqueness guarantee** (two segments can share a timestamp), and
  sequence-first makes lexical order == chronological. The timestamp comes from an injected `Clock`
  (default `time.time`, `FakeClock` in tests).

- **Gated by default; `RADIO_RECORD_MODE` is a seam, not a second implementation.** Only `gated` is
  built (the pump-tap records post-gate audio). A future `full`/pre-gate capture mode is reserved:
  `build_recorder` raises `NotImplementedError` rather than silently recording gated. This follows the
  brief's "pick gated default, note full-capture, don't build both."

- **Opt-in config, fail-loud at construction.** `RADIO_RECORD` (default **off**) gates whether a
  recorder is built at all — off means `build_recorder` returns `None`, the pump gets `null_recorder`,
  and nothing is written. `RADIO_RECORD_PATH` (marked default `recordings`) names the output directory,
  validated at construction: `os.makedirs(..., exist_ok=True)` then a `tempfile` probe, so a set-but-
  unwritable path raises `OSError` at the composition root — exactly like `JsonlSink` opening the log
  fail-loud. `RADIO_RECORD_MODE` (default `gated`) fails loud on an unknown value.

- **Failure isolation (hard rule).** Every `Recorder.write`/`end_segment` catches and drops on any
  exception (a corrupt segment is abandoned), and the pump **additionally** guards its two recorder
  calls and the shutdown `end_segment`. The double guard is a deliberate two-line deviation from the
  cycle-18 `on_key` no-guard precedent: the pump is the single shared capture task whose death would
  blind RX for **every** listener, and disk I/O is a far broader fault surface than the non-raising
  `hub.publish`/`on_key` leaves — the same blast-radius reasoning `EventLog.handle` uses. A recording
  fault can never break the RX pump, a TX, or the audio stream.

- **Defer TX recording, with a note.** `TxSession.feed` is a clean choke point and the cycle-18
  `on_key` edges already mark key-up/key-down, so TX recording is architecturally easy — but it doubles
  the surface (a Protocol on `TxSession`, `/audio/tx` wiring, a `RADIO_RECORD_TX` toggle, its own
  tests) and `feed` is the load-bearing keying state machine (guardrail 2), which deserves its own
  focused cycle. The `Recorder` is directly reusable later via the `on_key` edges with a `tx-` prefix;
  nothing here needs to change to accommodate it.

## Consequences

- **Received audio is now recordable to disk.** With `RADIO_RECORD=on` and a real squelch
  (`RADIO_SQUELCH=audio|cat`), each transmission received becomes one timestamped WAV. An end-to-end
  smoke drove scripted frames through the real `/audio/rx` WebSocket and confirmed both the live stream
  and a valid WAV on disk (canonical header, exact PCM).
- **Three behaviors documented, not fixed (future cycles):**
  1. **Squelch off ⇒ one long file.** With `RADIO_SQUELCH=off` (the default `pass_through_gate`) the
     gate never closes, so there is no segmentation edge — all RX accumulates into a single WAV that
     finalizes on pump stop/shutdown. Recording is most useful with a real squelch gate.
  2. **Coupled to the demand-driven pump.** The pump runs only while ≥1 `/audio/rx` listener is
     connected (it is the single capture reader), so nothing records when nobody is listening. A future
     cycle can pin the pump alive when recording is on.
  3. **Half-duplex TX pause** concatenates across the keyed gap in one WAV (no marker) — a minor
     artifact of the arbiter standing the pump down while keyed.
- **Leaf acyclicity preserved.** `recording/` imports only `..audio`; `rx/pump.py` gained only a local
  `RxRecorder` Protocol + `null_recorder` default (so an un-injected pump behaves exactly as before);
  every meeting point is in `api/app.py`. `create_app`/`RxPump` gained only keyword-default params, so
  all prior tests pass unchanged.
- **Test count:** `uv run pytest` → **352 passed, 4 skipped** (was 323; +29 — `Recorder` unit tests
  (valid WAV + canonical header, timestamped/sequenced names, fail-loud construction, swallowed write
  fault, idempotent end_segment), pump-integration (gated one-file-per-session, reject-all → no file,
  pump-stop finalize, `ExplodingRecorder` isolation, default-off writes nothing), and the config
  loaders). The 4 skips remain the multimon + piper hardware/model gates.
- **Deferred, on purpose:** Opus/compression; retention/cleanup; a playback/download API (the web UI
  sequence); full-capture (pre-gate) mode (seam only); TX recording; decoupling recording from the
  demand-driven pump. Then the web UI.
- **Numbering / branch note:** ADR 0020 by cycle order, cut from the cycle-18 merge point (`3aac985`,
  ADR 0019) at **323 passed, 4 skipped**. It adds one pure-leaf package (`recording/`) plus a tap in
  `rx/pump.py` and wiring in the `api` composition root; it adds no new dependency (`wave` is stdlib).
