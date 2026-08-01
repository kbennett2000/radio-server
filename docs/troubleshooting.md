# "I hear nothing" — and other first fixes

You've connected a real radio, everything looks set up, and you hear nothing on **Monitor**. This is
by far the most common hiccup, and here's the reassuring part:

> **It's almost always audio levels, not a fault.** The radio, the cable, and the software are usually
> fine — some volume is just turned down too low, so the incoming audio never crosses the threshold the
> software uses to tell "real signal" from "background hiss." Nothing is broken. You just need to turn
> a couple of levels up.

Work through these in order — most people are fixed by the first one.

> **On a KV4P HT board?** Steps 1 and 2 don't apply to you. The kv4p has **no volume knob** and there's
> **no computer capture level** — its audio arrives already digitized over USB, so there's nothing to
> turn up. Skip to [step 3](#3-prove-the-audio-is-arriving-then-set-the-squelch) to prove audio is
> arriving with `doctor --backend kv4p --rx-level`. The kv4p's own signal gate is the SA818 hardware
> squelch (`kv4p.squelch`, a level 0–8) rather than an OS mixer; if you set `audio.squelch = "cat"` to
> use the board's carrier-detect pin, remember it needs a non-zero `kv4p.squelch`. See
> [Changing the settings](configuration.md) and [Setting up a KV4P HT board](kv4p-setup.md).

> **On a UV-K5?** Audio rides the AIOC sound card, so steps 1–3 apply as written (use `alsamixer` and
> `doctor --backend uvk5 --rx-level`). But if the radio won't respond **at all**, run
> `doctor --backend uvk5` first: it tells you whether the radio is on a **dock firmware** and **which
> level** of it, whether it is still on **stock** (usable — see the no-flash path in
> [Setting up a Quansheng UV-K5](uvk5-setup.md)), or simply not answering (wrong
> `/dev/serial/by-id` path). **Receiving silence on a dock that connects fine is a firmware level
> problem, not a wiring one** — pre-F3 builds leave the audio path shut. The UV-K5's signal gate is an RSSI level
> (`uvk5.squelch_threshold`), not an OS mixer; `uvk5.squelch_mode = "cat"` (its default, ADR 0121) is
> valid but needs a non-zero threshold.

---

## 1. Turn up the radio's volume knob

**Start here.** The AIOC cable listens on the radio's **speaker** line — the same audio the radio's own
speaker plays. So if the **volume knob on the radio** is low, the computer barely hears anything, the
incoming audio sits below the squelch threshold, and it gets gated out. You hear silence.

Turn the radio's volume knob **up** — noticeably, past where you'd normally set it for the built-in
speaker — and try **Monitor** again. This single knob is the most likely cause, and it's not a software
setting at all.

---

## 2. Turn up the capture level on your computer

Your computer also has its own volume control for the AIOC's microphone (capture) side, separate from
the radio's knob. If it's low, turn it up. How you do that depends on your operating system.

### Linux

Open the mixer:

```sh
alsamixer
```

Press **F6** and pick the **All-In-One-Cable** card, then raise the **capture** level (the recording
side) and the **playback** level (the transmit side). Use the arrow keys; press **Esc** when done.

### Windows — *not yet confirmed on hardware*

**Settings → System → Sound → Input**, choose the **All-In-One-Cable** device, open its
**Properties**, and raise the **input volume**. Do the same under **Output** for the transmit side.

> These Windows steps are our best guidance but haven't been confirmed on real hardware yet. If you try
> it, we'd love to hear how it goes.

### macOS — *not yet confirmed on hardware*

**System Settings → Sound → Input**, select the **All-In-One-Cable** device, and raise the input
level. For finer per-device control, open **Audio MIDI Setup** (in Applications → Utilities) and adjust
the device's input gain there.

> These macOS steps are expected to work but haven't been confirmed on real hardware yet. If you try
> it, we'd love to hear how it goes.

---

## 3. Prove the audio is arriving, then set the squelch

If a knob didn't do it, take the squelch out of the picture for a moment so you can see whether audio
is reaching the computer at all.

1. **Let everything through.** In your settings, set the squelch to **off** (`audio.squelch = "off"`),
   start the server, and click **Listen (receive audio)** on the Monitor card. With the gate wide open you should now hear the radio, and
   the level meter should move when a signal comes in. If you *do* — good, audio is arriving, and it
   was just being gated. If you hear nothing even with squelch off, go back to steps 1 and 2; the
   levels are still too low.

2. **Measure the level and set the threshold.** **Stop the server first** (see below), then run:

   ```sh
   uv run python -m radio_server.doctor --rx-level
   ```

   While a signal is coming in, this prints how loud the received audio actually is. It either tells
   you the audio is arriving but gated — and gives you the `vad_on_rms` / `vad_off_rms` numbers to use
   — or that almost nothing is arriving, which points back to a volume/mixer problem in steps 1–2.

3. **Set the numbers and turn the squelch back on.** Put the `audio.vad_on_rms` and `audio.vad_off_rms`
   values it suggests into your settings, then set the squelch back to **audio** (`audio.squelch =
   "audio"`). Now the gate opens for real signals and stays quiet on dead air.

---

## DTMF works on one radio but not another

If the server recognizes keypad tones (DTMF) from one radio but ignores them from a **different**
radio — same server, same cable — the second radio is almost certainly sending its DTMF tones
*lopsided*: the low tones much louder than the high ones. The decoder rejects that imbalance as noise
by default. Some inexpensive radios do this; the **Baofeng UV-5R Mini** is the known offender, while a
regular UV-5R is fine.

The fix is one setting — raise **`audio.dtmf_reverse_twist_db`** (in the `[audio]` section of your
settings file) from its default of `4` to about `10`, then restart the server and try again. That
widens how lopsided a tone pair the decoder will still accept. Leave it at the default for radios whose
DTMF already works — the tighter default is what keeps the decoder from mistaking voice for keypresses.

---

## A short buzz on Mumble right after I stop talking (AIOC)

Talking from Mumble on the phone, you release the key, the radio drops — and a moment later Mumble
plays a brief (~quarter-second) buzz. That's the **TX→RX turnaround**: on the AIOC/UV-5R the receiver
unmutes a hair before its squelch settles, so a burst of receiver hash comes out of the sound card and
— because Mumble relays everything with `audio.squelch = "off"` — you hear your own tail. (The kv4p
doesn't do this; its module squelches the transient in hardware.)

**Try your radio's own squelch first.** A touch more squelch on the UV-5R often clamps the turnaround
before it ever reaches the cable.

If you'd rather fix it in software, there's a short guard that mutes the RF→Mumble feed for a moment
after every transmit: **`mumble.rx_guard_seconds`** (in the `[mumble]` section, default `0.4`). Nudge
it up if a little buzz still slips through; nudge it down if it's clipping the start of a fast reply.
Set it to `0` to turn the guard off. It only affects the Mumble feed — browser **Listen** and
recordings are untouched.

---

## "Device or resource busy" when a bench script or `doctor` opens the radio

Expected, and it means the service has the port (ADR 0166). The server claims its serial ports
exclusively while it runs, because a second process reading the same tty can kill the server's reader
thread — after which the radio stops answering and, before ADR 0166, nothing said so.

Stop the service, do your thing, start it again:

```sh
systemctl --user stop radio-server
# ... run doctor or the bench script ...
systemctl --user start radio-server
```

This cannot strand you: the claim is released when the process exits, including if it is killed.

## The radio has stopped answering, and the page says so

A red banner across the top of the web UI — *"The radio has stopped answering"* — means the serial
reader thread is dead. Everything else on the page (frequency, mode, whether the transmitter is
ready) is the last thing the server knew, not what the radio is doing now. `GET /healthz` answers
**503** in this state; `GET /status` still answers 200 and carries the `transport` block with the
error text and the device path.

Usual causes, in order: the USB device re-enumerated (common, and nobody's fault), the cable was
disturbed, or something else opened the port.

**Reopen the link** from the banner, or:

```sh
curl -X POST -H "Authorization: Bearer $RADIO_API_TOKEN" https://<host>:8090/diagnostics/reconnect
```

It makes exactly one attempt and tells you what happened — `already_healthy`, `reopened`, or a 503
with the reason. It is deliberately not a retry loop: if another program is holding the port, it says
so rather than fighting for it. If the reopen keeps failing, find what else has the device
(`fuser /dev/ttyACM0`) or restart the service.

## `<redacted>` where I expected a word

The server rewrites credentials out of its own log lines (ADR 0165), so `journalctl` output is safe
to paste into a bug report or a chat. Two consequences worth knowing:

- **`?token=<redacted>` in every WebSocket line is normal.** That is the fix working. The line still
  tells you the client address, the path and whether the socket was accepted or refused.
- **If `<redacted>` shows up somewhere odd** — inside a file path, a channel name, a station ID —
  then one of your secrets in `radio-secrets.toml` is also an ordinary word that appears in log text,
  and it is being blanked everywhere it occurs. Change that secret to something that is not a word.
  The startup line tells you which secrets are involved, by name only:

  ```
  INFO: radio_server.logsafe: log redaction armed by value for: api_token, totp_secret
  ```

  A secret shorter than 12 characters is listed as `redacted by name only` instead: it is still
  removed from `token=`/`password=`/`Bearer` shapes, but not hunted down as a bare value, because
  blanking a short string everywhere would do more damage to the log than the exposure does to you.
  If your API token is in that list, it is short enough to be worth rotating for its own sake.

## Both links went silent, or the station is hearing a music station

Check the **Second receiver** card in the web UI before you check anything else. If it says
**ON**, the radio is in broadcast FM and everything below is expected behaviour rather than a fault.

The UV-K5 has a *second* receiver inside it — a commercial-FM chip for the 88–108 MHz broadcast band,
separate from the one that hears your channel. While it is running it holds the speaker line the
server listens on, so between overs the station hears a broadcast station and not its own channel.
Listening to it deliberately is a legitimate thing to want. It costs four things, all measured:

1. **Both links go silent.** Nothing this radio hears reaches Mumble or a D-STAR reflector while the
   second receiver is selected. That is deliberate — a commercial broadcast relayed onto somebody
   else's repeater is a §97.113(b) problem, not just untidy.
2. **Real overs on your own channel are withheld from the links too, even though the radio hears
   them.** The mute reads "broadcast FM is selected", which is coarser than "cannot hear", so it
   holds real traffic off the links for as long as the second receiver is on.
3. **Any transmission turns it back off.** The server takes the receiver back before every key-up, so
   Talk, a voice service and the automatic station ID all end it — and with a controller session open
   the periodic ID will do it without anybody asking. Your transmission is not blocked; it just ends
   the broadcast listening.
4. **The radio's keypad is not locked, it is repurposed.** Digits type a frequency into the
   *broadcast* receiver rather than moving the station, and `M` opens a save-to-channel prompt. If
   somebody walks up to the radio not knowing it is in broadcast FM, they will think they are tuning
   the station and they will not be.

**To get out of it:** press **Turn it off** on the Second receiver card — one click, no confirm, and
it always works even mid-over. At the radio itself, `F` then `0` does the same thing.

**To change stations, do not stop it.** Type the new frequency in the card's Frequency box and press
**Retune** (or just press Enter in the box). The receiver moves without stopping, and the line
underneath reports the frequency *the radio* came back with — if that is not what you typed, the
radio is telling you something. Frequencies are on a 100 kHz raster (88.7, 98.5 — not 98.55); an
off-raster number is refused with a message saying so rather than quietly rounded to the adjacent
station.

If the card says **has not been asked**, this server has never learned whether the second receiver is
running — usually a radio on pre-F8 firmware. That is not the same as "off", and it is why the card
does not say "off".

## Two things that look like faults but aren't

Before you go chasing a hardware problem, rule these out — both are working as intended:

- **Talk uses your *computer's* microphone, not the radio.** When you click **Talk**, you're speaking
  into the mic on your computer, and radio-server keys the transmitter and sends that audio. It is
  *not* re-transmitting what the radio hears.
- **Your own Monitor goes quiet while you key.** So you won't hear *yourself* when you transmit — that
  silence is deliberate (it stops an echo). To confirm your transmission is actually going out, listen
  on a **second radio**.

---

## One thing to remember: stop the server before running `doctor`

The AIOC sound card can only be used by **one program at a time**. While radio-server is running it
owns the card, so the check-up tool (`doctor --rx-level` and friends) can't open it — you'll get a
"no input device" style error. **Stop the server** (Ctrl+C in the window it's running in) before you
run any `doctor` command that touches the audio, then start it again afterward.

---

## Still stuck, or want the bench detail?

If you've been through all of the above and it's still not right — or you'd like the deeper,
step-by-step bench walkthrough (wiring, the check-up tool in full, verifying the push-to-talk line,
testing DTMF decode, and taming RF that gets into the USB cable) — see the
[AIOC bench reference](hardware-bringup.md).
