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

**Mumble servers**
- The list of internet voice channels your station can link to. It ships already pointed at the
  public demo server — you don't set anything up to try it. Add entries here for your own or your
  club's server (the [run your own Mumble server](mumble-server/) guide shows how to get one).

**The voice**
- **Voice file** — the "Piper" voice used for spoken services and voice identification. You download it
  once. A good default is **`en_US-amy-medium`** from
  [the Piper voices collection](https://huggingface.co/rhasspy/piper-voices) (samples at
  [piper-samples](https://rhasspy.github.io/piper-samples/)). It comes as **two** files — the `.onnx`
  and its `.onnx.json` sidecar — download **both** into one folder, then point this setting at the
  `.onnx` (the sidecar must sit beside it, or the server won't start). The direct download links are in
  [Getting a voice](install.md#getting-a-voice).

**The time service**
- **Time zone** — which zone the spoken time uses (for example, `America/Denver`).

**The keypad layout** — see below.

**Where it listens**
- By default radio-server only answers on the computer it's running on. To reach it from your phone or
  another computer on your home network, set the address to `0.0.0.0`. (Running it as an always-on
  server is covered in the [deployment guide](deployment.md).)

**Sound levels**
- The squelch and volume-threshold settings decide when received audio is treated as a real signal
  versus background hiss. These are best set with the check-up tool rather than by guessing — see the
  [Troubleshooting guide](troubleshooting.md).
- **`audio.squelch`** picks *how* the server decides a signal is live: `off` (relay everything),
  `audio` (software voice-activity detection, using the `vad_*` thresholds), or `cat` (trust the
  radio's own hardware carrier-detect line). `cat` is valid on the **TM-V71A, the kv4p, and the
  UV-K5** — radios that have a real busy line — and is **rejected on the Baofeng**, which has none.
- **`audio.dtmf_reverse_twist_db`** — how much louder the decoder tolerates a DTMF keypad's *low*
  tones being than its *high* tones before it rejects the digit as unbalanced, in dB. **Leave it alone
  unless one radio's DTMF isn't being recognized while another's is.** Some inexpensive radios (the
  **UV-5R Mini** is the known one) transmit DTMF with the low tones much louder than the high, and the
  decoder treats that imbalance as noise by default. If that's your symptom, bump this to about `10`;
  the default keeps compliant radios well protected against false triggers. See the
  [Troubleshooting guide](troubleshooting.md).

**KV4P HT board** (only when your radio type is `kv4p`)
- **`kv4p.serial_port`, `kv4p.module_type` (vhf/uhf), `kv4p.frequency`** — the port, the band, and the
  start frequency. The board has no tuning knob, so set `kv4p.frequency` rather than key on whatever it
  powered up on. Full walkthrough in [Setting up a KV4P HT board](kv4p-setup.md).
- **`kv4p.squelch` is not the same setting as `audio.squelch`** — the names collide but they're
  different knobs:
  - **`kv4p.squelch`** is the SA818 radio module's own **hardware** carrier gate, a **level from 0 to
    8**. It decides how strong a signal has to be before the board's busy line reports "carrier
    present."
  - **`audio.squelch`** is the **server's** activity gate *mode* (`off` / `audio` / `cat`), described
    above.
  - They work together: if you set `audio.squelch = "cat"` (let the server trust the board's busy
    line), you must give `kv4p.squelch` a **non-zero** level — at level 0 the busy line never asserts,
    so the server would think a signal is present forever.
- **`kv4p.tx_gain` — turn this down if your kv4p transmissions sound overmodulated or distorted.** It
  is a TX audio-level multiplier (default `1.0`, no change). The kv4p has no sound card, so unlike the
  AIOC/Baofeng — where you tame TX level with `alsamixer`'s playback slider — there is no analog knob
  to lower an over-driven signal. `kv4p.tx_gain` is that knob, in software: it scales everything the
  server transmits (announcements, browser mic, Mumble). If your audio is overmodulated, lower it
  until clean — **a good starting point is `0.5`**. Values above `1.0` are allowed but clamp to full
  scale rather than distorting further.

**UV-K5 (Quansheng Dock)** (only when your radio type is `uvk5`)
- **`uvk5.serial_port` and `uvk5.frequency` are both required** — there is no default for either. The
  AIOC enumerates as an ambiguous `/dev/ttyACM*`, so give a stable `/dev/serial/by-id/…All-In-One-Cable…`
  path; and in full-control mode the host owns tuning with no radio-side value to preserve, so an unset
  frequency fails loud rather than putting a made-up one on the air. Full walkthrough in
  [Setting up a UV-K5 (Quansheng Dock)](uvk5-setup.md).
- **`uvk5.tone` / `uvk5.mode`** — the initial TX CTCSS tone (Hz; omit for none) and the bandwidth
  (`FM` wide / `NFM` narrow). Both are also changeable live via the API.
- **`uvk5.tx_allowed`** — set `false` for a genuinely receive-only node. Unlike the kv4p's firmware
  gate this is a software refuse-to-key (full-control keying is a direct register write): a keying
  attempt then fails loud rather than going out as dead air.
- **`uvk5.squelch_threshold`** is not the same setting as `audio.squelch` — the UV-K5 has a real RSSI
  busy line, so `audio.squelch = "cat"` is valid, but it reads `uvk5.squelch_threshold` (the RSSI level
  at or above which the radio reports busy). At `0` the gate reads busy forever, so `cat` with a `0`
  threshold is rejected — pair `cat` with a non-zero threshold.
- **`uvk5.input_device` / `uvk5.output_device` / `uvk5.blocksize` / `uvk5.tx_lead_seconds`** — the AIOC
  sound card and its tuning, the same shape as the Baofeng's audio settings (bench-verify the device
  name and the 0.5 s TX lead-in on your radio).
- **`uvk5.tot`** — the UV-K5's **mandatory** transmitter time-out in seconds (default `180`). The UV-K5
  has no device-side stuck-key cutoff of its own, so radio-server force-unkeys a stuck key for it. You
  may **shorten** this but **not disable** it: `0` is rejected (unlike the global `tx.tot`, which allows
  `0` to disable it for backends that have their own firmware/radio TOT), and so is any value above the
  `180` s default. It is in-process only — it cannot cover a host `SIGKILL`/power loss; see the
  [stuck-key warning](uvk5-setup.md#-stuck-key-warning--a-mandatory-server-time-out-and-its-one-residual-gap).

For the complete list — recording, scanning, timeouts, and everything else — see
[radio.toml.example](../radio.toml.example).

---

## Changing which key does what

The touch-tone keypad (the `01#` for the station ID, `02#` for the time, and so on from
[Using your station](using-it.md)) is fully rearrangeable. In the settings file it's the `[services]`
section, and out of the box it looks like this:

```toml
[services]
01 = "station-id"
02 = "time"
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

The two Mumble link codes are the one exception: `10#` (connect the demo server) and `98#` (link
off) live in the `[mumble]` section, not here — each server entry has its own `dtmf` code, and the
link-off code is the `disconnect_dtmf` setting. See "Linking to Mumble servers," below.

---

## Add your own services

The built-in keypad is deliberately small — station ID, time, log out — because the interesting
services are the ones *you* dream up. A weather report, your club's net schedule, a daily quote:
anything that can be spoken can be a service.

Here's the whole story:

1. There's a folder called `local_services/` next to the program. radio-server checks it every time
   it starts, and any service file it finds there — one you wrote, or one you downloaded — joins the
   station. Five ready-to-run examples ship in [`examples/local_services/`](../examples/local_services/)
   (weather, astronomy, quote, battery, bible); copy the ones you want into `local_services/` and
   they're live at the next restart.
2. Give the new service a key: add a line for it in the `[services]` section above, just like the
   built-ins.
3. If the service has settings of its own (say, the address of a weather source), they go in a
   `[plugins.<name>]` table — **not** a plain top-level table. The `[plugins.` prefix is the whole
   point: it's the one place the config parser leaves unvalidated for you.

   ```toml
   # ✗ wrong — a bare top-level table fails loud at startup:
   #   "unknown config table(s): [weather] (weather.base_url) -> [plugins.weather]"
   [weather]
   base_url = "http://weather.lan/api"

   # ✓ right — the same keys under [plugins.<name>]:
   [plugins.weather]
   base_url = "http://weather.lan/api"
   ```

   The plugin's **code doesn't change** either way — it reads `weather.base_url` regardless; only the
   TOML nesting moves. (This split arrived in ADR 0051; a config still carrying the old flat table
   gets that exact migration message pointing at its `[plugins.…]` home.)

The folder is **yours**: updating radio-server never touches it, so the services you add stay put.
The nuts and bolts of writing one — what a service file looks like inside — are in the
[architecture guide](architecture.md), and the five files in
[`examples/local_services/`](../examples/local_services/) are working references.

---

## Your password and login code (kept separate)

Two things are **not** in the settings file, on purpose, for safety:

- **The control-panel password** (the token you type in the browser).
- **The over-the-air login code** (the Google Authenticator secret).

These live in their own protected place (a separate file, or handed to the program privately when it
starts). You can rotate the password and re-enroll the login code right from the **Settings** tab in
the browser — no file editing needed. Setting up the login code the first time is covered in
[Using your station](using-it.md).

### Fixed login code (an option — less secure)

By default the over-the-air login code is a **rotating** TOTP code (it changes every 30 seconds), and
each accepted code is single-use so it can't be replayed. If you'd rather not use an authenticator
app, you can switch to a **fixed** 6-digit code you set once and key every time:

1. Turn on **`auth.fixed_code`** in the Settings tab (it's in the **auth** section, off by default).
2. Set the code itself with the write-only **Fixed login code** box under **Secrets** (6 digits). Like
   every credential it's kept in the protected place (`fixed_code` in the secrets file, or the
   `RADIO_FIXED_CODE` environment variable) — never in `radio.toml`.
3. Restart the server to apply.

> **Security warning.** A fixed code never changes, so anyone who overhears it over the air can reuse
> it indefinitely — it gets **none** of the single-use protection the rotating code has. It's a
> convenience, not a secure option. Leave `auth.fixed_code` **off** unless you specifically want this
> trade-off. It only takes effect when a login is required (`auth.totp_enabled` on) *and* a code has
> been set; with no code set, over-the-air login is unavailable (as if unconfigured).

**Mumble server passwords** come in two flavors, and only one of them is a real secret:

- A **join password** that's really a public gate code — like the demo server's, which is printed in
  the docs on purpose. That kind goes right in the settings file (each entry has a `password` field),
  and the Settings tab has a plain "Join password" box for it.
- A **private password**, for a server that's genuinely members-only. That kind is kept the protected
  way, like the two above — named `mumble_password_<name>` in the secrets file (or the
  `RADIO_MUMBLE_PASSWORD_<NAME>` environment variable), where `<name>` is the entry's name in
  lowercase with spaces and punctuation turned into `_`. The Settings tab has a write-only
  **Private password** box per entry, so you never have to touch the file.

If both are set for the same entry, the private one wins.

---

## Linking to Mumble servers

You can bridge your radio to [Mumble](https://www.mumble.info/) voice servers on the internet, so a
radio channel and a Mumble channel share audio — handy for an impromptu net, or for reaching friends
far outside simplex range. **You start with one already set up**: the public **Radio Server Demo**
entry ships switched on, so keying `10#` links to it out of the box. (Don't want it? Just delete
that entry.)

Each destination is a `[[mumble.servers]]` entry in the settings file — several servers, or several
channels on one server — with a `name` (any text you like, such as `"Radio Server Demo"` or
`"Club Net"`), a `host`, and optionally `port`/`channel`/`dtmf`/`password`/`tx_to_rf`/`autoconnect`
(see [radio.toml.example](../radio.toml.example)). The **Mumble servers** section of the Settings
tab edits the same list from the browser — including each entry's join password and private password
(explained above) — and like every setting, changes to the list take effect at the next restart.
On every server the station appears as **`<callsign> (radio-server)`** (from your **Callsign**
setting) — the nick isn't configurable, because the station always identifies as the licensee.
And if you'd like a server of your own, the [run your own Mumble server](mumble-server/) guide
gets you there for about two dollars a month.

**One link is active at a time** — connecting another entry switches to it. While linked, the
server relays received RF audio into the Mumble channel and — unless that entry sets
`tx_to_rf = false` for receive-only monitoring — transmits Mumble voice back over the air **under
your callsign, automatically identified** (Part 97). Three ways to connect:

- **The Control screen**: the **Mumble link** card lists every entry with its state (server,
  channel, who's there) and a per-entry Connect/Disconnect; hidden when no entries are configured.
- **Over the air (DTMF)**: give an entry a `dtmf` combo — the demo entry ships with `10` — and, in
  an authenticated session, key `10#` to connect it; the station speaks a confirmation ("Linked to
  Radio Server Demo."). Key `98#` (configurable: `mumble.disconnect_dtmf`) to disconnect ("Link
  off."). **Disconnecting needs no login** (ADR 0043): if your session times out while you sit and
  listen to a net, a bare `98#` still drops the link — it's the one un-gated combo, allowed because
  it only ever *removes* capability (connecting stays behind the login). Both spoken
  confirmations are settings: `mumble.link_announcement` (a template — `{name}` becomes the
  entry's name, underscores spoken as spaces) and `mumble.link_off_announcement`; leave either
  blank to act silently. The combos are listed on the Control screen's **Services** card with
  the rest of the keypad, and their Transmit buttons fire them from the browser too.
- **On boot**: set `autoconnect = true` on (at most) one entry.

The API equivalent is `POST /link {"entry": "Radio Server Demo", "on": true}`.

Linking needs the extra Mumble support installed: add `--extra mumble` to your `uv sync` command
(alongside the other extras you use — sync installs exactly what's listed; see
[install.md](install.md)). The Opus codec it needs rides along in that extra (ADR 0057), so there's no
separate system library to install. To check an entry before going live, run
`uv run python -m radio_server.doctor --link <name>` (the name is optional with a single entry) — it
connects to that Murmur (read-only, never touches the radio) and reports pass/fail with the
channel and peer count.

> **Upgrading from the single-server config?** The old flat `[mumble]` keys (`enabled`, `host`,
> `channel`, …) moved into `[[mumble.servers]]` entries; the server fails at startup with the
> exact replacement snippet if it finds the old form. The old `RADIO_MUMBLE_PASSWORD` secret is
> now per-entry: `RADIO_MUMBLE_PASSWORD_<NAME>`.

---

## Channel presets

If your radio supports tuning (a KV4P HT, a UV-K5 on Dock firmware, or the practice radio — not a
plain Baofeng on an AIOC cable, which has no CAT control), you can name a few **channel presets** and
recall one by name — handy for parking on a repeater's output frequency to monitor it from the browser.

Each preset is a `[[presets]]` block in the settings file:

```toml
[[presets]]
name = "2m Simplex"
frequency = 146520000       # Hz

[[presets]]
name = "Club Repeater Output"
frequency = 146940000       # Hz
tone = 100.0                # optional CTCSS tone in Hz (a standard tone; omit for none)
mode = "FM"                 # FM (default) or NFM
```

- **`name`** (required) — any text; how you apply it. Names must be unique.
- **`frequency`** (required) — the simplex frequency in **Hz** (146.520 MHz → `146520000`).
- **`tone`** (optional) — a standard CTCSS tone in Hz (e.g. `100.0`); omit for no tone.
- **`mode`** — `FM` (the default) or `NFM` (narrow).

Presets are **simplex** (receive and transmit on the same frequency) — enough to monitor a repeater's
output. Transmitting *through* a repeater (a split/offset) isn't supported yet.

A bad preset (an unknown tone, a duplicate name, a malformed frequency) **stops the server at startup
with a clear message** rather than being silently ignored. An empty or absent `[[presets]]` list simply
turns the feature off.

There's no web UI for presets yet — apply one over the API (the browser controls come in a later
update):

```sh
# List the configured presets (and which fields the active radio can honour):
curl -H "Authorization: Bearer $RADIO_API_TOKEN" http://127.0.0.1:8000/presets

# Apply one by name:
curl -H "Authorization: Bearer $RADIO_API_TOKEN" -X POST \
     http://127.0.0.1:8000/presets/apply -d '{"name": "Club Repeater Output"}'
```

If the active radio can't honour a field (say it has no CTCSS control), the preset applies what it can
and the response lists what it **skipped** — never a silent partial change. On a radio that can't tune
at all (a plain Baofeng), applying a preset returns a clear "unsupported in this mode" instead.

---

## Where to go next

- **[Using your station](using-it.md)** — the controls and the over-the-air services.
- **[Troubleshooting — "I hear nothing"](troubleshooting.md)** — setting audio levels the reliable way.
- **[radio.toml.example](../radio.toml.example)** — every setting, with descriptions.
