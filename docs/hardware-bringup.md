# Hardware wiring & bring-up guide

This guide covers bringing up the **AIOC/Baofeng** backend (ADR 0029). The **TM-V71A / SignaLink**
backend (`SignaLinkV71`) is still a `NotImplementedError` stub — its hardware hasn't arrived, and
its Hamlib rig model, `rigctl` serial speed, and `multimon-ng` flags stay verify-on-hardware
(guardrail 1); that section stays pending below.

Until a hardware backend is selected, everything runs against the mock
([architecture.md](architecture.md#backends)).

## AIOC / Baofeng (UV-5R)

The NA6D **AIOC** ("All-In-One-Cable") is a USB composite device that gives a UV-5R two things:
a **USB sound card** (audio in/out) and a **serial port** (PTT keying). There is **no CAT** — set
frequency by hand on the radio. The backend advertises only the shared caps; the API returns 501 for
any tuning call (guardrail 3), and the web UI greys out the tuning controls.

### What it enumerates (confirmed on this station)

| Piece | What you'll see |
|---|---|
| USB device | `1209:7388` "All-In-One-Cable", driver `cdc_acm` |
| PTT serial | `/dev/ttyACM0` — stable path `/dev/serial/by-id/usb-AIOC_All-In-One-Cable_<serial>-if04` |
| Sound card | ALSA `AllInOneCable` (`hw:CARD=AllInOneCable`, e.g. `hw:2`), 48 kHz-native, capture + playback |

Quick manual checks: `lsusb | grep 1209`, `cat /proc/asound/cards`, `ls /dev/serial/by-id/`.

### Prerequisites

1. **System audio library:** `sudo apt install libportaudio2` (PortAudio backs `sounddevice`).
2. **Python hardware extra:** `uv sync --extra hardware` (installs `pyserial` + `sounddevice`).
3. **Serial permissions:** your user must be in `dialout` —
   `sudo usermod -aG dialout $USER`, then log out/in. Check with `id -nG | grep dialout`.

### Diagnose before you transmit

```
python -m radio_server.doctor
```

Read-only: it enumerates the AIOC sound card (confirms 48 kHz capture/playback), checks the serial
port opens (holding both control lines **low** — no keying) and that you can reach it, and prints a
pass/fail table. Fix any `[FAIL]` line before continuing.

### Verify which line keys PTT (RTS vs DTR) — the one empirical fact

PTT is a serial control line. On the reference NA6D AIOC + UV-5R this was **confirmed to be DTR**
(cycle 29 bench test — RTS did not key), so `baofeng.ptt_line` defaults to `dtr`. It stays a
verify-on-hardware fact (guardrail 1): a different AIOC or radio may key on RTS. Confirm on your own
hardware with a **dummy load connected** (or otherwise certain it's safe to transmit):

```
python -m radio_server.doctor --key-test               # tests the configured line (default dtr)
python -m radio_server.doctor --key-test --ptt-line rts # test the other line if needed
```

This is the only path that keys the radio. It refuses to run non-interactively/in CI, prints a
safety banner, requires you to type `CONFIRM`, asserts the configured line for ~2 s (watch the TX
LED / dummy load), drops it, and asks which line keyed. Set `baofeng.ptt_line` to whichever keyed.

### Configure & run

In `radio.toml` (see `radio.toml.example` for every key + description):

```toml
[server]
backend = "baofeng"

[audio]
squelch = "audio"   # the UV-5R has no busy line; software VAD is the only gate (cat is rejected)

[baofeng]
serial_port  = "/dev/ttyACM0"          # or the stable /dev/serial/by-id/... path
ptt_line     = "dtr"                    # confirmed on the bench: DTR keys this AIOC (RTS did not)
input_device = "All-In-One-Cable: USB"  # sounddevice name substring (or an integer index)
output_device = "All-In-One-Cable: USB"
# blocksize = 960                       # 20 ms @ 48 kHz; verify-on-hardware
```

> **Audio device names:** `sounddevice`/PortAudio match a device by a substring of its *PortAudio*
> name (e.g. `All-In-One-Cable: USB Audio (hw:2,0)`) or an integer index — **not** by a raw ALSA
> `hw:CARD=...` string. If the default substring doesn't resolve on your box (or is ambiguous because
> a sound server also exposes the card), `python -m radio_server.doctor` lists every AIOC device with
> its index and tells you exactly what to set.

Then `python -m radio_server --config radio.toml` (a TOTP secret must be configured for the
controller/voice services — see the [config docs](../README.md)). Acceptance is empirical:
**plug it in, it keys up clean** — TX keys the radio with no clipped tail, RX audio streams to the
browser, and the station ID fires on the keyed over (Part 97).

### Audio levels & squelch (the "I hear nothing" step)

Keying works long before the *audio* is right — that part is all levels, and levels are
verify-on-hardware (guardrail 1). Two things bite first:

- **Squelch** is a software gate that only passes received audio once it's loud enough (so the
  gateway doesn't stream dead-air hiss). With `audio.squelch = "audio"` the gate opens at
  `audio.vad_on_rms` (default **500**). The AIOC taps the UV-5R's *speaker* line, so if the **radio
  volume knob** or the **AIOC capture level** is low, real audio sits under 500 and you hear
  **nothing** on Listen — the audio is arriving but gated.
- **"Talk" transmits your COMPUTER's microphone**, not the radio. And the browser's Listen monitor
  **mutes while you key** (you won't hear yourself — that's intended); verify TX on a second radio.

Bring-up flow:

1. **Relay everything first:** set `audio.squelch = "off"` so all received audio passes, then start
   the server and click **Listen** — you should now hear the radio (its meter moves).
2. **Set the levels** with `alsamixer` — press **F6**, pick the **All-In-One-Cable** card, and raise
   the **capture** (RX) and **playback** (TX) levels; also turn **up the UV-5R volume knob**.
3. **Measure the RX level** while a signal is coming in:
   ```
   python -m radio_server.doctor --rx-level
   ```
   It prints the received RMS/peak and either tells you the audio is arriving but gated (with the
   `vad_on_rms`/`vad_off_rms` values to set) or that almost nothing is arriving (a volume/mixer
   problem). Set `audio.vad_on_rms` / `audio.vad_off_rms` from it, then switch `audio.squelch` back
   to `"audio"`.
4. **Confirm TX audio** into the dummy load — proves the transmit path without the browser mic:
   ```
   python -m radio_server.doctor --tx-tone
   ```
   A second radio should hear the tone; if faint, raise the AIOC **playback** level in `alsamixer`.
5. **Talk through the gateway:** click **Talk** and speak into your computer mic — the far end hears
   you.

### Testing DTMF decode

DTMF is how over-the-air callers authenticate and trigger services (received audio → `multimon-ng`
→ digits → TOTP auth). Test it in three steps:

1. **Install the decoder** — the server shells out to it:
   ```
   sudo apt install multimon-ng
   ```
2. **Software self-test (no radio):**
   ```
   uv run pytest tests/test_dtmf.py
   ```
   With multimon installed this runs the real-decode test (a synthesized tone → decoded digit),
   confirming the decoder + multimon work on this box.
3. **From the radio** — stop the server first (single-open sound card), then:
   ```
   python -m radio_server.doctor --dtmf
   ```
   Key digits on the radio into the UV-5R: it prints each decoded digit, and a full **entry** is the
   digits followed by `#` (`*` clears a partial). If nothing decodes, confirm a strong RX signal with
   `--rx-level` first and hold each tone ~100 ms+.

> **Why the buffering:** the decoder needs ~40–200 ms of continuous tone to lock on, but the AIOC
> delivers ~20 ms audio blocks, so both `--dtmf` and the **live controller** now **accumulate ~0.5 s
> of audio** before each decode (ADR 0030). The window is tunable via `dtmf.buffer_seconds` (default
> `0.5`) — raise it if keyed digits don't decode, lower it for less latency.
>
> **Held keys count once:** multimon re-emits a digit for as long as a key is held, so a held tone
> collapses into a single keypress. A genuinely repeated key (e.g. `55`, or two identical adjacent
> digits **in a TOTP code**) only registers twice if you leave a short pause between the two presses —
> pause briefly between repeated digits when keying a code.
>
> **Over-RF auth now decodes:** the live controller buffers DTMF exactly like `--dtmf` (they share the
> same code), so with a TOTP secret + callsign configured, keying `<code>#` from a radio authenticates.
> `--dtmf` remains the isolated tool to confirm decode works on your hardware before wiring auth.

### Notes / gotchas

- **Card / port names aren't index-stable** across reboots/replugs — prefer the `sounddevice` name
  substring `All-In-One-Cable: USB` (audio) and the serial `by-id` path over `hw:2` / `ttyACM0`.
- **"Device busy" on open** usually means PulseAudio/PipeWire grabbed the card; `doctor` flags this,
  and enumerates every AIOC device (raw ALSA vs the sound-server copies) with the index to use.
- **One program at a time owns the sound card.** The AIOC capture is single-open, so `--rx-level`
  (and `--tx-tone`) cannot run while the server is using the card — **stop the running server first**.
  If you don't, the busy device drops out of the device list and you'll get "No input device
  matching …"; the doctor reports this cleanly and tells you to stop the server.
- **Never leave it keyed:** the backend holds both lines low on open and drops the line on `close()`
  / process exit (`atexit`), so a crash can't wedge the transmitter keyed.

## TM-V71A / SignaLink

**Status: pending.** `SignaLinkV71` is still a stub. The Hamlib rig model number, `rigctl` serial
speed, `multimon-ng` flags, and the SignaLink DATA-port wiring are verify-on-hardware facts
(guardrail 1) and will be filled in when that hardware is on the bench. PTT there is audio-triggered
by the SignaLink (self-keys off the DATA-port audio); CAT (Hamlib `rigctld`, TM-D710 backend) is for
tuning only and never keys the radio (guardrail 2).
