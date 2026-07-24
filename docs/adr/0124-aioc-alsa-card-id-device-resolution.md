# 0124 — AIOC sound-card addressing: resolve ALSA card ids, not just PortAudio names

Status: Accepted

## Context

The LAN server (`ubuntuserver`) runs the `[uvk5]` backend on the AIOC cable. The connect probe passed
ALL, but `doctor --rx-noise` failed:

```
[FAIL] could not open the AIOC capture device: No input device matching 'AIOC_K6'
```

Kris had written `/etc/udev/rules.d/85-aioc-names.rules` to name the AIOC's ALSA card `AIOC_K6` off
its USB serial, and set `radio.toml` to match:

```toml
input_device  = "AIOC_K6"
output_device = "AIOC_K6"
```

The working theory was that the udev rule had not applied. **It had.** On the bench:

```
/sys/class/sound/card2/id -> AIOC_K6
/proc/asound/cards:  2 [AIOC_K6        ]: USB-Audio - All-In-One-Cable
```

`udevadm test /sys/class/sound/card2` shows the rule matching and writing `ATTR{id}="AIOC_K6"`. The
rule was simply authored *after* the reboot it was tested against (boot ≈20:39, rules file mtime
20:41), so the first observation was stale.

## The real defect — a three-layer name mismatch

A USB sound card carries three distinct identifiers, and only the first is udev's to set:

| Layer | Value here | Set by |
|---|---|---|
| ALSA card **id** | `AIOC_K6` | udev `ATTR{id}` — what the rule changes |
| ALSA card **name** | `All-In-One-Cable` | the USB product string; **not** settable by udev |
| **PortAudio device name** | `All-In-One-Cable: USB Audio (hw:2,0)` | derived from the card *name* |

`sounddevice` resolves a string device by case-insensitive **substring match against PortAudio
names**. Those names are built from the card *name*, never the card *id*. So `"AIOC_K6"` matches
nothing and **sounddevice itself** raises `No input device matching 'AIOC_K6'` — which is why that
string appears nowhere in this repo.

The consequence worth stating plainly: **no udev rule can make this config work.** `ATTR{id}` is
the only lever udev has over a sound card's naming, and PortAudio does not read it. The instinct to
"fix the udev layer so it conforms to `radio.toml`" is unsatisfiable by construction.

This module already knew half of it — `soundcard.py` documented *"NOT by a raw ALSA string like
`hw:CARD=AllInOneCable`"* — but stopped short of the corollary that a card **id** is equally
unmatchable.

## Why not simply re-point `radio.toml` at a name that works

Two alternatives were rejected:

- **`input_device = "All-In-One-Cable: USB"`** (the shipped default) resolves today, but names the
  *product*, not the *cable*. A second AIOC arrives 2026-07-25 for the UV-5R; both present the
  identical PortAudio name, so the substring becomes ambiguous and which radio you get depends on
  enumeration order.
- **`input_device = 2`** (the raw index) is stable only until something re-enumerates.

The serial is the only per-cable discriminator, and the udev rule already projects it into a stable
card id. The gap was never the naming scheme — it was that nothing could *consume* it.

## Decision — teach the sound-card seam to resolve an ALSA card id

`resolve_device(sd, device, *, kind, sysfs_root=None)` in
[`backends/soundcard.py`](../../radio_server/backends/soundcard.py), the one seam both the `uvk5`
and `baofeng` backends already funnel through (`open_capture_stream` / `open_playout_stream`), plus
`doctor._check_audio`. Resolution order — **existing behaviour first**, so every config that works
today is untouched and the new branch only engages where sounddevice would already have failed:

1. `None` / `int` → unchanged.
2. The string substring-matches a PortAudio name → returned unchanged, for sounddevice to match as
   it always has.
3. Otherwise read it as an ALSA card id: `/sys/class/sound/card*/id` → card index `N` → the
   PortAudio device named `… (hw:N,…)` with channels in the direction being opened → its **integer
   index**.
4. Still nothing → the string is handed back, so sounddevice raises its own familiar error rather
   than one invented here.

`kind` (`"input"`/`"output"`) matters because one card can expose its two legs as separate PortAudio
entries; only the direction actually being opened should decide the pick. `sysfs_root` is read at
call time so tests can point it at a tmp dir.

Two deliberate softenings, both regression guards rather than defensive noise:

- **A seam without `query_devices` passes through.** The backends' injected test doubles expose only
  `RawInputStream`/`RawOutputStream`; reaching for `query_devices` unconditionally would
  `AttributeError` every existing backend test.
- **A `query_devices` failure passes through.** A PortAudio blow-up during *resolution* must not
  mask the real error the stream open would have raised.

Absent sysfs (CI, macOS) falls through at step 3, so the module stays hardware-free at import.

## The two-AIOC addressing scheme this completes

`ATTRS{serial}` → ALSA card id → PortAudio index. Each cable's serial pins a card id; each card id
now resolves to whichever index it landed on this boot. Saturday's second AIOC is one uncommented
line in the rules file plus `AIOC_UV5R` in its config block — no index chasing, no plug-order
dependency.

The rule was also tidied (host-side, no PR): `KERNEL=="card*"` scoping added, since `ATTR{id}` exists
only on the card node and without it udev attempts the write on every sound sub-device; and the
second-AIOC line commented out rather than live with a placeholder serial.

```
SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{serial}=="da3441ac", ATTR{id}="AIOC_K6"
```

## Bench evidence (2026-07-24, ubuntuserver)

- **Card id applied and rule proven firing:** `udevadm test` → `85-aioc-names.rules:11
  ATTR{id}="AIOC_K6"` on `card2`; `aplay -l` → `card 2: AIOC_K6 [All-In-One-Cable]`.
- **`AIOC_K6` in no PortAudio name** — `query_devices()` lists `All-In-One-Cable: USB Audio
  (hw:2,0)` and the PipeWire copies; the id is absent. This is the defect, observed.
- **Direction check:** with the card free, `(hw:2,0)` reports `in=1 out=1`. (It reads `in=0` while
  the service holds the card — the card is single-open — which is what made the mapping look
  playback-only at first.)
- **RX proven alive before the fix**, via a throwaway config copy naming the device the old way:
  **peak 5073 RMS (-16.2 dBFS), average 4143** — "RX audio path is ALIVE". A bare
  `arecord -D hw:CARD=AIOC_K6` on the same card reads **floor (67 RMS)**, because it never runs the
  dock's enter-HW-mode sequence that unmutes the receiver (`REG_47`→FM, ADR 0120/0122). Worth
  recording: on this radio a raw ALSA capture is *not* a valid RX test.
- **After the fix, with the shipped `radio.toml` unchanged** (`input_device = "AIOC_K6"`), run from a
  scratch tree at the same commit + this change: `doctor --rx-noise` → **peak 5299 RMS (-15.8 dBFS),
  average 4123 RMS (-18.0 dBFS)**, "RX audio path is ALIVE". `doctor --backend uvk5` → all serial and
  connect-probe checks PASS.

## Consequences

- `soundcard.py`: `ALSA_SYSFS_ROOT`, `_alsa_card_index`, `resolve_device`; both stream openers
  resolve. `doctor._check_audio` resolves and now *reports* the mapping, which is otherwise
  invisible to the operator.
- `radio.toml` is unchanged — the code conforms to the intended target state rather than the
  reverse. `config/spec.py` help for all four device keys documents the card id;
  `radio.toml.example` regenerated (comment text only).
- Tests: new `tests/test_soundcard_device.py` (15) against a fake `sd` + tmp-dir sysfs — card id →
  index, direction preference, name-substring passthrough (no regression), name-match-wins-over-card-id,
  unknown/int/None passthrough, missing sysfs, seam without `query_devices`, `query_devices` failure,
  and both stream openers; plus a `test_doctor.py` case asserting `_check_audio` resolves the id and
  reports the mapping. Full suite **1548 passed, 5 skipped**.
- No firmware change; `frames.py`/`transport.py` untouched (F2 / ADR 0119 invariant).

### Verify on hardware (guardrail 1)

1. The second AIOC's serial → `AIOC_UV5R` mapping, on arrival 2026-07-25. Until then that rules-file
   line ships commented with a placeholder.
2. `radio-server.service` reports `failed` after a clean `systemctl --user stop` (`status=143` is
   SIGTERM). Cosmetic — the app logs "Application shutdown complete" — but a `SuccessExitStatus=143`
   in the unit would stop it looking like a fault. Not changed here; noted in
   [server-notes.md](../server-notes.md).
