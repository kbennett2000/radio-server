# 0071 — Stop analysing kv4p DTMF; capture the received audio and read the tones out of it

Status: Accepted

## Context

DTMF still does not decode on the kv4p backend after ADR 0070 corrected the ~2 % RX sample-rate
offset. The bench state going into this cycle: the true ADC rate was measured (48,759 Hz →
`kv4p.sample_rate_correction = 1.0158`), the signal is strong (loudest block 17312, peak near full
scale), and the same UV-5R that always decodes DTMF into the AIOC backend decodes nothing into kv4p —
so the tones are certainly transmitted.

Analysis has now been wrong three times (codec, level, clipping — cleared; then the sample rate —
fixed, still no decode). The lesson is to **stop reasoning about the pipeline and measure the actual
received audio**: capture exactly what `receive()` returns — the corrected 48 kHz stream
`GoertzelStream` would see — and read the DTMF tones straight out of it with an FFT, independent of the
decoder that keeps failing.

## Decision

Add an **RX-capture + direct WAV analysis** to doctor, and tighten the rate verdict.

- **`doctor --backend kv4p --rx-capture`** records N seconds of `receive()` to a WAV (`--out`, default
  `kv4p-rx-capture.wav`) while the operator keys `1234#` from a handheld, then analyses it. Read-only —
  it never keys. `--analyze-wav PATH` re-runs the analysis on a saved WAV (no radio).
- **`analyze_dtmf_windows` / `format_dtmf_analysis`** — pure, hardware-free. Each ~100 ms window is
  Hann-windowed and FFT'd; the strongest low-band (650–1000 Hz) and high-band (1150–1700 Hz) peaks are
  found with **parabolic sub-bin interpolation** (so a ~2 % residual on 1633 Hz is visible past the
  10 Hz bin), snapped to the DTMF grid, and mapped to a digit when both land within ~half a bin. It
  also reports each window's **clip fraction** and loudest overall peaks. Deliberately **not**
  GoertzelStream — it is the independent second opinion on what the decoder is being fed.
- The report ends in a **verdict in the task's priority order**, checking clipping *first* because a
  clipped dual-tone still shows its fundamentals (so it can look "on-frequency") yet is exactly what
  trips the decoder's gates:
  1. **CLIPPING** in a majority of active windows → the firmware's **16× RX gain** (`rxAudio.h`
     `Boost(16.0)`) is saturating a strong dual-tone; the harmonics/intermodulation it breeds knock the
     tones off `GoertzelStream`'s twist / 2nd-harmonic / group-dominance gates. Upstream fault.
  2. tones **present but off-frequency** → the sample-rate correction is still wrong (prints the
     residual and an implied nudge).
  3. tones **present, on-frequency, not clipping** → the audio is clean, so the fault is decode-path
     **wiring** (how the corrected frame reaches `GoertzelStream` in the server/doctor).
  0. tones **absent, not clipping** → mangled upstream (firmware filtering, SA818, RF).
- **`--rx-level` verdict tightened** (`_RATE_MATCH_TOL = 0.002`): flag any >0.2 % gap between the
  measured and configured correction and print the implied value to set. The old 0.5 % gate wrongly
  called the bench's 1.0158-vs-1.02 (0.4 %) "dialed in".

### The firmware RX chain, read as the leading suspect (kv4p-ht `3f0e809`, `rxAudio.h`)

The processing order before Opus encoding is:
`dcOffsetRemover → gain → afskTapEffect → mute`, with:
- `Boost gain(16.0)` — a **16× gain** stage. On a strong SA818 output this saturates; a clipped
  two-tone is the textbook way to destroy DTMF decodability while leaving the fundamentals visible.
- `DCOffsetRemover` — a one-pole high-pass (decay 0.25 s, `alpha` from `expf`), well below the 697 Hz
  low group; harmless to DTMF.
- `AfskTapEffect.process` returns its input unmodified — a **passive** tap, not in the audio's way.
- `Boost mute(0.0)` — gated by squelch; when open it passes audio through.

So the **16× gain is the concrete, testable hypothesis** for "tones mangled upstream" — which is why
the analyzer surfaces the clip fraction and calls it out explicitly. The WAV is the arbiter.

## Consequences

- The next bench run **names the cause with real numbers** instead of a fourth guess: the operator runs
  `--rx-capture` while keying `1234#`, and the verdict points at firmware gain (clipping), the
  correction (off-frequency), or decode wiring (clean) — each with the dominant frequencies found.
- If the verdict is **clipping**, the fix is to attenuate the RX audio (host-side scale-down before the
  decoder, or a firmware gain change upstream) — its own follow-up cycle, out of scope here.
- `MockRadio` gained a no-op `close()` so it is a faithful double for the open-then-close diagnostics.

## Bench acceptance (operator, RX only — no keying by the tool)

1. `doctor --backend kv4p --rx-capture --seconds 12 --out cap.wav`, keying `1234#` a few times.
2. Read the verdict and the per-window dominant frequencies; paste them into the PR.
3. If the verdict is "clipping" or "off-frequency", apply the indicated fix and re-capture; if "clean
   wiring", the follow-up is the decode path. `--dtmf` decoding `1234#` remains the definition of done.

## Follow-ups (not this cycle)

- Whatever the WAV names: an RX attenuation stage (if clipping), a correction re-trim (if
  off-frequency), or a decode-path fix (if clean). Recorded once the capture says which.
