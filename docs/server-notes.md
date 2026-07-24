# Server ops notes

Dated notes for the LAN server (`ubuntuserver`, `kb@192.168.1.62`) that hosts the radios and
dongles. Ops-only changes made directly on the box live here — things that are *not* in the repo's
deploy path and would otherwise be lost. Newest first.

---

## 2026-07-24 — AIOC ALSA card naming (`AIOC_K6`) and why `--rx-noise` was failing

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
