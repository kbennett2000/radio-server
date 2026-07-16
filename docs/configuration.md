# Changing the settings

There are two ways to change how radio-server works. Most people never need to touch a file.

1. **In the browser (easiest).** Open the control panel, go to the **Settings** tab, and change what
   you like. Every field has a short description right next to it. This is the recommended way.
2. **In a settings file.** If you prefer, all the same settings live in a plain text file called
   `radio.toml`.

Either way, **changes take effect the next time you start radio-server** — change a setting, then stop
and start it again.

---

## The settings file

If you'd rather edit a file, the settings live in `radio.toml` next to the program. To get started,
copy the fully-commented example:

```sh
cp radio.toml.example radio.toml
```

[radio.toml.example](../radio.toml.example) lists **every** setting with a plain description and its
normal value — it's the complete reference. You only need to include the lines you want to change;
anything you leave out keeps its normal value.

---

## The settings you're most likely to change

You don't need all of these — here are the ones that matter most, in plain terms.

**Your station**
- **Callsign** — required before the station will transmit.
- **How it identifies** — Morse code or a spoken voice, and how fast the Morse is sent.

**The voice**
- **Voice file** — the "Piper" voice used for spoken services and voice identification. This is a file
  you download once; point this setting at it.

**The time service**
- **Time zone** — which zone the spoken time uses (for example, `America/Denver`).

**The over-the-air spoken services**
- The weather, quote, battery, and Bible services each read from another service on your home network.
  For each one you want, set its address (for example, `http://192.168.1.62:8005`). Leave it blank to
  turn that service off.

**The keypad layout** — see below.

**Where it listens**
- By default radio-server only answers on the computer it's running on. To reach it from your phone or
  another computer on your home network, set the address to `0.0.0.0`. (Running it as an always-on
  server is covered in the [deployment guide](deployment.md).)

**Sound levels**
- The squelch and volume-threshold settings decide when received audio is treated as a real signal
  versus background hiss. These are best set with the check-up tool rather than by guessing — see the
  [bench setup guide](hardware-bringup.md).

For the complete list — recording, scanning, timeouts, and everything else — see
[radio.toml.example](../radio.toml.example).

---

## Changing which key does what

The touch-tone keypad (the `1 #` for time, `2 #` for weather, and so on from
[Using your station](using-it.md)) is fully rearrangeable. In the settings file it's the `[services]`
section, and it looks like this:

```toml
[services]
1 = "time"
2 = "weather"
3 = "astronomy"
4 = "station-id"
5 = "quote"
6 = "battery"
7 = "bible"
99 = "logout"
```

To move something, change the number on the left. For example, to put the station ID on `5` and log-out
on `0`:

```toml
[services]
5 = "station-id"
0 = "logout"
1 = "time"
```

Whatever you list here becomes the complete keypad. If you leave a service off the list, its key simply
does nothing — the automatic station identification and the session timeout keep working regardless.

---

## Your password and login code (kept separate)

Two things are **not** in the settings file, on purpose, for safety:

- **The control-panel password** (the token you type in the browser).
- **The over-the-air login code** (the Google Authenticator secret).

These live in their own protected place (a separate file, or handed to the program privately when it
starts). You can rotate the password and re-enroll the login code right from the **Settings** tab in
the browser — no file editing needed. Setting up the login code the first time is covered in
[Using your station](using-it.md).

The **Murmur server password** (if your Mumble server needs one) is a third secret kept the same way
(`RADIO_MUMBLE_PASSWORD`), never in the settings file.

---

## Linking to a Mumble server (optional)

You can bridge your radio to a self-hosted [Murmur](https://www.mumble.info/) (Mumble) server, so an
RF channel and a Mumble channel share audio — handy for an impromptu net. Turn it on with the
`[mumble]` group in the settings file (`mumble.enabled`, `mumble.host`, `mumble.channel`, …; see
[radio.toml.example](../radio.toml.example) for every key). Once linked, the server relays received
RF audio into the Mumble channel and — unless you set `mumble.tx_to_rf = false` for receive-only
monitoring — transmits Mumble voice back over the air **under your callsign, automatically
identified** (Part 97). Connect/disconnect at runtime via the API (`POST /link`).

Linking needs the extra Mumble support installed (`pip install '.[mumble]'`, which also needs the
system `libopus0` library). **The network client is still being brought up** — the settings, the
bridge, and the API are in place and tested, but connecting to a live Murmur lands in a follow-up
cycle; enabling the link before then fails loud at startup rather than silently doing nothing.

---

## Where to go next

- **[Using your station](using-it.md)** — the controls and the over-the-air services.
- **[Bench setup & troubleshooting](hardware-bringup.md)** — setting audio levels the reliable way.
- **[radio.toml.example](../radio.toml.example)** — every setting, with descriptions.
