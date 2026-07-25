# Server ops notes

Dated notes for the LAN server (`ubuntuserver`, `kb@192.168.1.62`) that hosts the radios and
dongles. Ops-only changes made directly on the box live here — things that are *not* in the repo's
deploy path and would otherwise be lost. Newest first.

---

## 2026-07-24 — F4 Chain B (TX): the stack transmits; using the kv4p as an RF sniffer

Follow-up to the RX triage below. The F4 report included "browser TX not working." **It transmits
fine — proven with a second radio, not the HT.**

### The HT was the unreliable instrument; the kv4p is the objective one

Every "no tone / no key-up" observation this session came through Kris's HT, which also produced the
contradictory RX captures. The bench's **kv4p (UHF SA818, service on 8091)** has a hardware
carrier-detect: `status().busy` is the SQ/COS pin (`backends/kv4p/radio.py:562` — "a real carrier
detect… an open squelch (carrier present) reads busy"). Tuned to the UV-K5's TX frequency inches
away, it is a software RF sniffer.

### The frequency confound

The failing tests were on **147.555 (VHF)** — the bench freq pinned for RX work (below). The kv4p is
**UHF-only**, so `radio.toml` was reverted to **445.800** (the deployment's operating freq), which
also put the UV-K5 where the kv4p can hear it.

### What the kv4p measured (445.800)

- `doctor --tx-tone` → kv4p SQ pin opened for **~4.7 s** (the 5 s tone). Carrier confirmed: the radio
  **does** radiate when register-keyed.
- **Browser Talk** (the actual reported symptom) → kv4p carrier opened on each press **and** its RX
  audio showed strong modulated voice (window RMS to **7427**, dominant 480–944 Hz + harmonics). Full
  chain browser → AIOC → modulator → RF → receiver works end to end.

### Reusable RF-loopback recipe (left in `/tmp`)

To verify UV-K5 TX in software without an HT, on **445.800** (UHF, in kv4p band):
1. `radio.toml` freq on the UV-K5 set to a UHF value; tune the kv4p to the same freq.
2. `/tmp/kv4p_carrier_watch.py <sec>` (polls 8091 `status().busy` = carrier detect) — run while keying.
3. `/tmp/kv4p_audio_probe.py <sec>` (captures kv4p `/audio/rx`, per-window RMS + dominant Hz) —
   detects whether the carrier is modulated.
   Key the UV-K5 via `doctor --tx-tone` (TTY CONFIRM) or the browser PTT; watch for busy→True + audio.

### Notes for a possible firmware cycle (not radio-server code)

- **VHF (147.555) TX is untested** with an objective receiver (no VHF RX on the bench). Unknown
  whether dock-mode TX works on VHF; not an operational blocker (deployment is UHF).
- **Key-test first-attempt flake:** the first `--key-test` after backend construction refused
  (`reg 0x30=0xbff1`, want `0xc1fe`); the retry passed (keyed 99 ms). A settle/first-key race worth a
  look if VHF TX is ever investigated.

### Frequency restored

`radio.toml` `[uvk5] frequency` reverted **147555000 → 445800000** — the F4 VHF pin is gone and the
deployment is back on its operating frequency. (The `radio.toml.bak-f4` backup can be removed.)

---

## 2026-07-24 — F4 RX triage: clipping knob, frequency pin, a doctor false-positive

Bench session that produced [ADR 0125](adr/0125-uvk5-v3-rx-pump-cat-gate-decouple.md) (the pump
CAT-gate decouple, a code fix, shipped as a PR). These are the **ops** findings from the same
session — level/config, not code.

### RX audio was clipping — the volume knob, not the host mixer

`doctor --rx-level` on idle noise read **peak 32752 / 32767 (−0.0 dBFS)** — the receiver audio was
slamming clip. The host capture mixer (`AIOC Audio In`) is already at 100 % / 0.00 dB (ADR 0124), so
the **radio's volume knob** is the only remaining level lever, and it had been turned too far up.
Clipping matters for DTMF: the harmonics a clipped waveform generates trip the native decoder's
2nd-harmonic / dominance gates, so `1234#` would not decode even though a carrier was plainly present.

**Fix (on the box):** turned the knob down to **peak 28032 (−1.4 dBFS)**, average ~8800 RMS. At that
level `doctor --dtmf` decoded **`1234#1234#`** cleanly — two perfect entries. **Leave the knob near
here** (idle-noise peak in the high-20k's, not pinned at 32767). If DTMF ever stops decoding, check
this first with `doctor --rx-level` before touching anything else.

Note this is only *half* of why DTMF failed on the server — the other half was the RX-pump
starvation fixed in code by ADR 0125. Both had to clear: the knob so `doctor` (which bypasses the
pump) decodes, the pump fix so the **live server** decodes.

### `radio.toml` frequency pinned to 147.555 MHz (was 445.800)

For the F4 bench work `[uvk5] frequency` was changed **445800000 → 147555000** to match the HTs on
the bench (both were on 147.550, then 147.555). Backup of the pre-change file is at
`~/applications/radio-server/radio.toml.bak-f4`.

**This is a bench setting, not the operating frequency.** When bench testing is done, restore the
intended operating frequency (445.800 or whatever the deployment should sit on) — the backend
re-applies `radio.toml`'s frequency on every construction, so the live value follows this key. The
web UI can also set it at runtime (`POST /frequency`), which is what the operating log showed during
this session.

### A `doctor --rx-capture` VERDICT(2) false-positive (filed, not fixed)

On a **pure-noise** capture (no DTMF present), `--rx-capture` emitted
`VERDICT (2): DTMF tones present but OFF-FREQUENCY … nudge kv4p.sample_rate_correction by ~×1.0236`
while every individual analysis window said "(no clean pair)". Two problems: it claimed tones on
noise, and it named **`kv4p.sample_rate_correction`** on a **uvk5** run (wrong backend's config key).
Minor doctor defect — the verdict logic over-reaches on noise and the remediation hint is
backend-blind. Worth a future doctor cycle; harmless (advice only), so not fixed here.

**Symptom.** `doctor --rx-noise` failed with `No input device matching 'AIOC_K6'` while the connect
probe passed ALL. The working theory was that the udev rule naming the card had not applied.

**It had.** The card was already renamed:

```
/sys/class/sound/card2/id -> AIOC_K6
/proc/asound/cards:  2 [AIOC_K6        ]: USB-Audio - All-In-One-Cable
```

The rules file had simply been written *after* the reboot it was tested against (boot ≈20:39, file
mtime 20:41), so the first look at it was stale.

**Actual root cause — a layer mismatch, not a udev fault.** A USB sound card has three names and
udev only owns the first:

| Layer | Value | Set by |
|---|---|---|
| ALSA card **id** | `AIOC_K6` | udev `ATTR{id}` |
| ALSA card **name** | `All-In-One-Cable` | USB product string — **not** settable by udev |
| PortAudio device name | `All-In-One-Cable: USB Audio (hw:2,0)` | derived from the card *name* |

`sounddevice` matches a string device against **PortAudio names**, which come from the card *name*,
never the card *id*. So `input_device = "AIOC_K6"` could never resolve — **no udev rule can fix
this**, because `ATTR{id}` is the only naming lever udev has and PortAudio does not read it. The
error text comes from the sounddevice library itself, not radio-server.

**Fix:** radio-server now resolves an ALSA card id to a PortAudio index itself
(`resolve_device` in `radio_server/backends/soundcard.py`) — see [ADR 0124](adr/0124-aioc-alsa-card-id-device-resolution.md).
`radio.toml` stays as-is; the code conforms to it.

### Final working rule — `/etc/udev/rules.d/85-aioc-names.rules`

```
# AIOC USB sound cards — stable per-cable ALSA card ids, keyed on the USB serial.
#
# Keyed on ATTRS{serial} (never card index / plug order) so a second AIOC cannot steal a name.
# KERNEL=="card*" scopes the match to the card node — ATTR{id} exists only there; without it
# udev attempts the write on every sound sub-device (controlC*, pcmC*D*).
#
# NOTE: this sets the ALSA card *id* (the aplay -l bracket field, and hw:CARD=AIOC_K6). It does
# NOT change the card *name* ("All-In-One-Cable", from the USB product string), which is what
# PortAudio/sounddevice reports. radio-server resolves the id itself — see ADR 0124.

SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{serial}=="da3441ac", ATTR{id}="AIOC_K6"

# AIOC #2 (UV-5R) — arriving 2026-07-25. Find its serial with:
#   ls -l /dev/serial/by-id/     ->  usb-AIOC_All-In-One-Cable_<SERIAL>-if04
# then paste <SERIAL> below and uncomment the line.
# SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{serial}=="<uv5r-aioc-serial>", ATTR{id}="AIOC_UV5R"
```

Apply with `sudo udevadm control --reload && sudo udevadm trigger -s sound`. Verify the rule
actually matches with `sudo udevadm test /sys/class/sound/card2` (look for the
`ATTR{id}="AIOC_K6"` line) — `aplay -l` alone won't tell you *why* a name stuck.

**Saturday (AIOC #2):** paste the new serial into the commented line, uncomment, reload+trigger,
then set that radio's config block to `input_device = "AIOC_UV5R"` / `output_device = "AIOC_UV5R"`.
No index chasing — both cables share the PortAudio name `All-In-One-Cable`, so the card id is the
only stable discriminator.

### Bench numbers (2026-07-24)

- `doctor --rx-noise` with `radio.toml`'s `AIOC_K6`, post-fix: **peak 5299 RMS (-15.8 dBFS),
  average 4123** — RX ALIVE.
- **A bare `arecord -D hw:CARD=AIOC_K6,DEV=0` reads floor (67 RMS) and that is *not* a fault.**
  The UV-K5 dock only un-mutes its receiver when radio-server runs the enter-HW-mode sequence
  (`REG_47`→FM, ADR 0120/0122). A raw ALSA capture skips that, so it always looks dead. Use
  `doctor --rx-noise` to test RX on this radio, never `arecord`.

### Two cosmetic warts noticed, deliberately not changed

- `systemctl --user stop radio-server` leaves the unit in state **`failed`** (`status=143` = SIGTERM).
  The app shuts down cleanly ("Application shutdown complete"); systemd flags it only because the
  unit lacks `SuccessExitStatus=143`. Adding that line to
  `~/.config/systemd/user/radio-server.service` would silence it.
- The deployed checkout `/home/kb/applications/radio-server` carries an **uncommitted local edit** to
  `radio_server/audio/dtmf.py`. Deploy drift — worth reconciling or reverting before the next pull.

### Operational reminders

- The AIOC sound card is **single-open**: stop `radio-server` before any capture test and start it
  straight after. There are three checkouts under `~/applications/` (`radio-server`,
  `-kv4p`, `-dstar`); the user service runs the plain `radio-server` one.
- To test an unmerged change without disturbing the deployed tree: `cp -a` the checkout to `/tmp`,
  overlay the changed files, and run with
  `cd /tmp/<copy> && uv run --project ~/applications/radio-server ... python -m radio_server.doctor`
  — cwd wins on `sys.path` and `radio.toml` is discovered from cwd, so the scratch tree's code and
  config are used against the deployed venv.
