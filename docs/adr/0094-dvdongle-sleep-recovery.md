# 0094 — The DV Dongle recovers itself from the idle-sleep wedge: close + reopen + re-handshake on a frame timeout

Status: Accepted

## Context

The D-STAR crossband stuck-key incidents were *triggered* by the DV Dongle wedging: the journal showed
every decode failing with `SerialTimeoutException: Write timeout`. ADR 0092/0093 made the transmitter
un-strandable regardless — the safety problem is closed — but the *reliability* problem (the dongle
wedging mid-over) remained. A **no-RF bench characterisation** (sustained decode stress + idle-gap
cycles on the real dongle, no gateway/bridge) pinned it down:

- **Sustained decode is rock-solid**: 3000 back-to-back decodes, zero slow frames, steady ~36/s.
- **The wedge is the AMBE2000 idle-sleep.** After ~2-3 s with no codec traffic the chip sleeps and the
  next frame times out (`VocoderTimeout`, no reply) — and it **does not self-wake**: every subsequent
  frame fails too. As the caller keeps feeding an unresponsive chip, the FTDI TX buffer fills and the
  writes themselves start timing out (`SerialTimeoutException`) — the exact signature from the incident.
- **A full close + reopen + re-handshake reliably recovers it** (bench-proven: idle 4 s → decode fails
  → `close()` + reopen → decode succeeds).

The idle-sleep is already *prevented while idle* by the ADR 0088 keepalive (a NULL_AMBE decode every
1.2 s keeps the chip warm). The gap the keepalive can't cover is an idle stretch **during** a live
over — a network stall or a pause between reflector talkers while the bridge is in `rx` mode, where the
keepalive is off. There, the chip sleeps mid-stream and the over's audio wedges.

## Decision

Give `DVDongleVocoder` a **self-recovery** path. On a `VocoderTimeout`, `_exchange` calls a new
`_recover()` **once** and retries the frame; a second failure propagates.

`_recover()` rebuilds the transport and session — the same open+start the constructor runs:

- Under the (now **reentrant**) io lock so it can't race a concurrent exchange.
- **Tear the old transport down fully before reassigning anything the reader touches.** The reader
  thread reads `self._serial`/`self._stop` by reference, so `_recover` signals stop, closes the old
  port, and **joins** the old reader before rebuilding — otherwise a live reader would race the swap.
- Rebuild the port, reader, decoder and reply slots, then re-handshake — **retrying the flaky first
  open** a few times (`_RECOVER_HANDSHAKE_ATTEMPTS`), exactly as cold bring-up does (the AMBE2000's
  first open after a bad state answers the name query but drops the session-start, bench-observed).
- If it cannot wake the dongle after those attempts, raise `VocoderUnavailable` — a dead dongle, which
  the bridge already handles.

## Consequences

- **A mid-over sleep no longer wedges the stream.** The first frame after the chip nods off triggers a
  transparent close+reopen and the over's audio keeps flowing, instead of every subsequent frame
  failing until the FTDI buffer fills and the write-timeout cascade begins. This is the reliability
  half of the crossband fix; the ADR 0092/0093 safety net (PTT always drops) sits beneath it unchanged.
- **`_exchange` is split** into the recover-and-retry wrapper and the `_exchange_once` primitive; the
  io lock became an `RLock` so `_recover` can hold it while re-running the handshake (which re-takes
  it). No public surface changed; the bridge and the doctor self-tests are untouched.
- **Bounded, not a loop.** A single recover+retry per frame — a persistently dead dongle raises rather
  than spinning. Recovery is best-effort and logged.
- **Verified on fakes that model the wedge** — a `FakeDongle` that handshakes but never answers an
  exchange, reopened as a healthy one, proves recover-and-complete; a flaky reopen (start drops) proves
  the handshake retry; a permanently wedged pair proves the timeout still propagates after one recover;
  an un-openable dongle proves `VocoderUnavailable`. The underlying close+reopen recovery and the
  idle-sleep trigger are both **bench-proven on the real dongle** (no RF). `uv run pytest` green.
- **Does not change the D-STAR posture.** The crossband stays disabled on the live radios; this only
  makes the vocoder robust for whenever crossband (or browser-only) D-STAR runs.

Cross-refs: ADR 0086 (the vocoder seam + no-interleave rule), ADR 0088 (the idle keepalive this
complements), ADR 0092/0093 (the stuck-key safety net this reliability fix sits beneath).
