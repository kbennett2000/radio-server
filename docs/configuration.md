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

**Murmur server passwords** (if your Mumble servers need them) are kept the same way — one per
entry, named `mumble_password_<entry name>` in the secrets file (or the
`RADIO_MUMBLE_PASSWORD_<NAME>` environment variable), never in the settings file. The **Mumble
servers** section of the Settings tab has a write-only password box per entry.

---

## Linking to Mumble servers (optional)

You can bridge your radio to self-hosted [Murmur](https://www.mumble.info/) (Mumble) servers, so an
RF channel and a Mumble channel share audio — handy for an impromptu net. Define your destinations
as `[[mumble.servers]]` entries in the settings file — several servers, or several channels on one
server — each with a `name`, `host`, and optionally `port`/`channel`/`dtmf`/`tx_to_rf`/
`autoconnect` (see [radio.toml.example](../radio.toml.example)). The **Mumble servers** section of
the Settings tab edits the same list from the browser (restart to apply, like every setting).
On every server the station appears as **`<callsign> (radio-server)`** (from your **Callsign**
setting) — the nick isn't configurable, because the station always identifies as the licensee.

**One link is active at a time** — connecting another entry switches to it. While linked, the
server relays received RF audio into the Mumble channel and — unless that entry sets
`tx_to_rf = false` for receive-only monitoring — transmits Mumble voice back over the air **under
your callsign, automatically identified** (Part 97). Three ways to connect:

- **The Control screen**: the **Mumble link** card lists every entry with its state (server,
  channel, peers) and a per-entry Connect/Disconnect; hidden when no entries are configured.
- **Over the air (DTMF)**: give an entry a `dtmf` combo (e.g. `13`) and, in an authenticated
  session, key `13#` to connect it — the station speaks a confirmation ("linked to home"). Key
  `73#` (configurable: `mumble.disconnect_dtmf`) to disconnect ("link off"). Both spoken
  confirmations are settings: `mumble.link_announcement` (a template — `{name}` becomes the
  entry's name, underscores spoken as spaces) and `mumble.link_off_announcement`; leave either
  blank to act silently. The combos are listed on the Control screen's **Services** card with
  the rest of the keypad, and their Transmit buttons fire them from the browser too.
- **On boot**: set `autoconnect = true` on (at most) one entry.

The API equivalent is `POST /link {"entry": "home", "on": true}`.

Linking needs the extra Mumble support installed: add `--extra mumble` to your `uv sync` command
(alongside the other extras you use — sync installs exactly what's listed; see
[install.md](install.md)) plus the system `libopus0` library. To check an entry before going live, run
`python -m radio_server.doctor --link <name>` (the name is optional with a single entry) — it
connects to that Murmur (read-only, never touches the radio) and reports pass/fail with the
channel and peer count.

> **Upgrading from the single-server config?** The old flat `[mumble]` keys (`enabled`, `host`,
> `channel`, …) moved into `[[mumble.servers]]` entries; the server fails at startup with the
> exact replacement snippet if it finds the old form. The old `RADIO_MUMBLE_PASSWORD` secret is
> now per-entry: `RADIO_MUMBLE_PASSWORD_<NAME>`.

---

## Where to go next

- **[Using your station](using-it.md)** — the controls and the over-the-air services.
- **[Bench setup & troubleshooting](hardware-bringup.md)** — setting audio levels the reliable way.
- **[radio.toml.example](../radio.toml.example)** — every setting, with descriptions.
