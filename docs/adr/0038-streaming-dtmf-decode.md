# 0038 — Stream RX audio through one persistent multimon-ng process for DTMF

Status: Accepted

## Context

DTMF codes with a **repeated adjacent digit** decode unreliably over the air. `99#` (the logout
command) frequently registers as `9#` and is dropped; all-distinct codes like `01#` never miss.
Longer tones and longer inter-key gaps help a little but never make it reliable.

The cause is the fixed-window decode path introduced in ADR 0030. `BufferedDtmfInput` accumulates
~0.5 s of received audio and runs a **fresh `multimon-ng` process per window**
(`MultimonDtmfDecoder.decode` shells out with `subprocess.run` each time). Because a held tone can
straddle a window boundary, it decodes once in each of the two chunks — a spurious double count.
To hide that, the buffered path applies a **held-tone de-dup**: consecutive identical decoded
digits are collapsed, and the run is reset only by a **fully-silent window**. That heuristic cannot
tell "one tone straddling a boundary" from "two genuine presses of the same digit landing in
adjacent windows", so it eats the second `9` of `99#` unless a completely silent 0.5 s window
happens to fall between the two presses. This was documented as a known consequence in ADR 0030
("Repeated adjacent digits need a brief pause"), and its deferred fix was named there: a
**persistent streaming multimon process**.

That fix is now warranted — the symptom is real on the air. We verified the approach empirically
against the installed **multimon-ng 1.3.1** at the codebase's 22050 Hz raw s16le input rate. Fed a
**continuous** stream (not per-window chunks), multimon does its own tone-onset/gap detection and
needs no Python de-dup at all:

| Input | multimon output |
|---|---|
| held `9`, 500 ms (one press) | `9` |
| held `9`, 1500 ms (one long press) | `9` |
| two `9`s, 30 ms gap | `99` |
| two `9`s, 80 ms gap | `99` |
| `9 9 #` (120 ms tones, 80 ms gaps) | `99#` |
| `1 5 5 #` | `155#` |

A held tone emits **once**; two presses emit **twice, even at a 30 ms gap**. The double count the
de-dup fights only exists because we re-decode per window. Streaming the continuous RX into one
long-lived process removes the double count *and* the lossy de-dup, and resolves repeated digits
down to a very short gap. ADR 0031 already made `RxPump` the single capture reader that feeds the
controller one contiguous frame stream, so the audio side is already a continuous stream — only the
decoder was still chopping it up.

## Decision

- **Add a streaming decode path in `radio_server/audio/dtmf.py`:**
  - `DtmfStream` — a small injectable protocol (`write(pcm)`, `read() -> str`, `close()`), mirroring
    the existing `DtmfDecoder` seam so tests drive it with a fake and no binary.
  - `MultimonStream` — the real `DtmfStream`: one lazily-spawned `multimon-ng -a DTMF -t raw -`
    (`subprocess.Popen`), a daemon reader thread that parses `DTMF:` lines onto a thread-safe queue,
    `write()` that feeds resampled PCM to stdin, and `close()` that tears the process down. It
    restarts the process if it dies (spawn/backpressure/drain/restart — the lifecycle ADR 0030 flagged
    as the cost of this approach), with an `atexit`/`__del__` backstop.
  - `StreamingDtmfInput` — same public surface as `BufferedDtmfInput`
    (`pump(frame, now) -> list[str]`, `flush(now)`, optional `on_digit`), composing a `DtmfStream`
    and the unchanged `DtmfFramer`. **No de-dup** — multimon owns repeat detection.
- **A config toggle, streaming by default.** `dtmf.decode_mode` (`streaming` | `buffered`, default
  `streaming`, env `RADIO_DTMF_DECODE_MODE`) selects the path in `build_controller` and
  `doctor --dtmf`. `buffered` keeps the ADR 0030 path verbatim as a one-line in-field revert if
  streaming misbehaves on unfamiliar hardware. Marked default + "verify against hardware"
  (guardrail 1).
- **Lifecycle.** `Controller.close()` reaps the decoder's process; the FastAPI lifespan teardown and
  `RxPump.run`'s `finally` call it (guarded). A half-duplex TX pause is just a silence gap in the
  stream — the process is not closed there.

### Alternative considered — keep buffering, detect intra-window silence

Instead of streaming, the buffered path could reset the de-dup on an energy dip *within* a window
(RMS gap detection) rather than requiring a whole silent window. Rejected: it re-implements, less
well, the onset/gap detection multimon already does correctly when handed a continuous stream, and
it still fights the per-window re-decode. Streaming removes the root cause instead of tuning around
it.

## Consequences

- **`99#` and other repeated digits decode reliably.** A genuine second press only needs a short
  inter-key gap (tens of ms), which any hand-keyed code has, instead of a full silent decode window.
- **This supersedes ADR 0030's held-tone de-dup consequence.** The fixed-window accumulator and its
  de-dup remain in the tree, selectable via `dtmf.decode_mode=buffered`, but `streaming` is the
  default and the recommended path.
- **A long-running subprocess to manage.** `MultimonStream` owns process spawn, a reader thread, and
  restart-on-death; it must be closed on shutdown to avoid an orphan `multimon-ng`. This is the
  materially-larger change ADR 0030 anticipated; the lifecycle is small and centralized in one class
  plus two teardown call sites.
- **Fails safe, unchanged.** A mis-decoded digit still yields a TOTP code that fails
  `verify_and_burn` — a rejected auth, never a false accept (guardrail 4). The caller re-keys.
- **One source of truth preserved.** `doctor --dtmf` uses the same streaming path as the live
  controller, so the operator's decode-validation tool exercises exactly what the server runs (the
  ADR 0030 property is kept).
