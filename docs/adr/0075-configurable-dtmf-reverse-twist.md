# 0075 — Configurable DTMF reverse-twist tolerance

Status: Accepted

## Context

Bench-confirmed on real hardware, not synthetic: on the **same** AIOC backend and the same native
decoder, a Baofeng **UV-5R decodes DTMF fine while a UV-5R Mini decodes nothing**. Both radios' tones
were captured and replayed through the real `GoertzelStream` (not the FFT analyzer). The Mini's tones
are on-frequency and well above the energy floor — but its **low tone group runs ~6.4 dB hotter than
its high group** (median reverse twist −6.4 dB). The native decoder rejects a block whose groups are
too unbalanced:

```
if hi_max > self._forward_twist * lo_max or lo_max > self._reverse_twist * hi_max:
    return None  # the two tones are too unbalanced to be a keyed digit
```

with the reverse limit hardcoded at `NATIVE_REVERSE_TWIST_DB = 4.0`. At −6.4 dB the Mini trips that
gate on **172 of 176** tone blocks; the compliant UV-5R sits at −0.1 dB and sails through. Replaying
the real `mini.wav`: it decodes cleanly at a 10 dB reverse limit, garbles at 8, and yields nothing at
4. The UV-5R still decodes at 10 (no regression). The gate is a **power** ratio — Goertzel `power` is
magnitude-squared — so the conversion is `10**(dB/10)`, and a −6.4 dB reverse twist is a ~4.4×
low/high power ratio, past 4 dB (ratio 2.51) and under 10 dB (ratio 10).

**Talk-off is not carried by the twist gate.** With the reverse limit widened to 10 dB, 16 torture
signals (white noise across seeds, AM hum, chirp sweeps, random tone pairs) produced **zero** false
digits, because the *dominance* gate (a single tone must dominate its group) and the *second-harmonic*
gate (a fundamental must beat its own second harmonic) are what reject voice and broadband energy —
not twist. So widening reverse twist for the Mini does not open the talk-off door.

## Decision

Add a config setting **`audio.dtmf_reverse_twist_db`** (float, env `RADIO_DTMF_REVERSE_TWIST_DB`,
`coerce_positive_float`) that **defaults to 4.0** — today's value. It is threaded into the decoder in
place of the hardcoded constant, exactly the way `dtmf.timeout` reaches the framer:

- `GoertzelStream.__init__` takes `reverse_twist_db: float = NATIVE_REVERSE_TWIST_DB` and computes
  `self._reverse_twist = 10.0 ** (reverse_twist_db / 10.0)`. `NATIVE_REVERSE_TWIST_DB` stays as the
  default constant the setting falls back to — **the default is unchanged**.
- `load_dtmf_reverse_twist_db(settings)` (in `audio/dtmf.py`, beside the other DTMF loaders) reads it;
  `controller/engine.py` passes it into `GoertzelStream(...)` where the native decoder is built, and
  `doctor.py`'s listen path does the same so the diagnostic honors the override.
- The key lives in the `audio` group (with `audio.squelch` / the `vad_*` gate) and is marked
  **advanced** — a talk-off-adjacent expert tunable, like the other `dtmf.*` / `vad_*` keys.

**The default is deliberately not changed.** The Mini is a non-spec-compliant encoder; a compliant
DTMF pad keeps its two groups within a couple of dB. Globally loosening a talk-off-adjacent gate to
accommodate one out-of-spec radio is the wrong default — it would erode the margin the compliant
majority relies on. So the fix is **opt-in**: the tight, talk-off-safe 4 dB gate stays the default,
and the Mini owner sets `audio.dtmf_reverse_twist_db = 10`.

**Scope: reverse twist only.** That is the demonstrated need — no radio seen has a *forward*-twist
problem (high group hotter than low). Forward twist could be exposed the same way (an
`audio.dtmf_forward_twist_db` mirroring this) if one ever turns up, but building it now would be
untested speculation.

**Global, not per-backend.** One instance drives one radio today, so a single global setting is right.
Once the backend-switch feature (the ADR 0073/0074 arc) lands and one box runs two radios, a
per-backend twist may be worth revisiting — noted for that cycle, not built here.

## Consequences

- A radio whose DTMF was undecodable purely because of reverse twist (the UV-5R Mini) works once the
  operator raises `audio.dtmf_reverse_twist_db`; every existing user is untouched (default 4.0).
- Schema change: one new `SettingSpec`, so `radio.toml.example` is regenerated from `spec.py` and the
  settings-count canary moves 62 → 63.
- New tests reproduce the radio: a synthesized −6.4 dB reverse-twist tone fails at 4.0 and decodes at
  10.0, and talk-off (white-noise seeds, a chirp sweep, off-grid tone pairs) holds at the widened 10.0
  gate — the safety proof. The rest of the DTMF suite is unchanged, which is itself the proof that the
  4.0 default preserves every existing decode.

## Non-goals

Not the backend-switch swap cycle (ADR 0073/0074 arc's next step). Not forward twist. Not per-backend
twist. Docs limited to the `configuration.md` + `troubleshooting.md` entries, this ADR, and HANDOFF.
