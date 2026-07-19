# 0093 — The AIOC transmitter can never be stranded keyed: drop the line first, unconditionally, atomically

Status: Accepted

## Context

Three times, the D-STAR reflector→RF crossband left the AIOC/UV-5R **transmitting dead air** with no
software path to stop it — only killing the process (closing the serial port) dropped PTT. ADRs 0090
(TOT), 0091, and 0092 hardened the *bridge* (watchdog, teardown, re-key guard) but the transmitter
still stuck on real hardware.

A **controlled carrier key-test** (a bare `pyserial` DTR toggle on a dummy load, no audio/dongle/bridge,
with the operator watching) finally isolated the truth:

- **`DTR=False` with the serial port still open cleanly unkeys this AIOC.** Assert DTR → keyed; drop
  DTR (port open) → stopped. So the line control is fine — the hardware is *not* latching.
- Therefore the stuck-key is a **software bug that fails to reach `dtr=False`**, not a hardware fault.

Reading `AiocBaofeng` against that fact exposes the class of bug:

1. **`ptt(False)` was guarded on `self._keyed`.** If the tracked flag ever desynced from the physical
   line, `ptt(False)` — the watchdog's, the teardown's, the REST `/ptt off`'s safety lever — would
   **no-op**, and nothing in software would ever drop the line. The incident journal matches exactly:
   `/ptt off` returned 200 and `status.transmitting` read False, yet the carrier stayed up until the
   port closed.
2. **`_key_on` asserted the line, then did more work.** It set the line high, *then* wrote the TX
   lead-in. If that write raised, the exception propagated with the line asserted but `_keyed` never
   set True — stranding the transmitter under exactly the guarded-`ptt(False)` no-op above.
3. **`_key_off` drained the audio stream *before* dropping the line.** `stream.stop()` blocks until
   the buffer drains and can raise/hang on an xrun'd or starved stream (the DV Dongle write-timeout
   wedge, `SerialTimeoutException`, was failing every decode, starving the playback). The line was
   dropped only *after* that teardown — so a blocking/raising teardown kept the transmitter keyed.

The unit tests never caught it because the fakes never raise on stream ops and `MockRadio`'s PTT is a
bare flag — the failure modes only exist on real PortAudio + a wedged codec.

## Decision

Make the AIOC backend structurally incapable of stranding the transmitter. Three changes, all proven
against injected fakes that now model the hardware failure — **no live keying**:

- **A single unconditional un-key primitive, `_drop_line()`.** A bare serial `setattr(line, False)` +
  `_transmitting = False`. Never guarded on `_keyed` or any tracked state; no drain, no stream
  teardown, so it cannot block or raise on the audio path. Every un-key route ends here.
- **`_key_off` drops the line FIRST, then tears down the stream best-effort.** The RF-safety inversion
  of the original drain-then-drop (ADR 0029): the line goes low immediately via `_drop_line()`, and
  only then is the stream `stop()`/`close()`d inside `contextlib.suppress`. A drain that blocks or a
  `stop()` that raises can no longer keep the transmitter keyed. Cost: a few ms of clipped audio tail
  on key-down — the symmetric counterpart to the key-up lead-in, and always preferable to a stuck key.
- **`_key_on` is atomic, and `ptt(False)` is unconditional.** Once `_key_on` asserts the line, every
  remaining step (the lead-in write) is guarded so any failure drops the line before re-raising.
  `ptt(False)` always calls `_key_off()` (the `if self._keyed` guard is gone), so the safety lever
  *always* drives the line low regardless of the tracked flag.

## Consequences

- **The transmitter can always be unkeyed in software.** Whichever path calls `ptt(False)` — the ADR
  0092 crossband watchdog/teardown, the ADR 0090 TOT, or the REST `/ptt off` — now drives the line low
  immediately and unconditionally, before any audio-stream work, independent of `_keyed`. The bench
  fact this rests on (`dtr=False` unkeys) is proven, so this is far more grounded than the pure-bridge
  fixes were.
- **Every streaming and one-shot keyer benefits** — browser TX, the Mumble bridge, D-STAR, station ID,
  services, `/transmit` — because the fix is in the backend, beneath all of them.
- **Key-down clips a few ms of tail** (line drops before the drain). Acceptable and documented; the
  lead-in already absorbs the symmetric key-up delay. A future refinement could drain with a bounded
  timeout before the guaranteed drop if tail fidelity ever matters.
- **Verified on fakes that model the failure**: a lead-in write that raises (line dropped, not
  stranded, `ptt(False)` still safe); a `stop()` that raises (line still dropped); the line proven low
  *before* the stream is stopped; and `ptt(False)` forcing the line low when `_keyed` is desynced.
  `uv run pytest` green.
- **This does not re-enable D-STAR.** The crossband stays disabled on the live radios; re-enable is
  gated on a **joint** dummy-load re-proof (operator watching), never an autonomous run. The separate
  DV Dongle write-timeout wedge (the trigger that starved the decode) remains a reliability follow-up.

Cross-refs: ADR 0029 (the AIOC backend + the drain-then-drop this inverts), ADR 0092/0091/0090 (the
bridge-side stuck-key fixes this completes at the hardware layer), ADR 0032 (the TX lead-in, the
symmetric key-up counterpart).
