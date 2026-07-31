# Using your station

There are three ways to use radio-server:

1. **From your browser** — a control panel on your home network, where you can listen, talk, and see
   what's happening.
2. **Over the air** — where you (or people you trust) call in from a handheld and hear spoken
   information back, like the current time.
3. **Linked to the world** — your station can join a voice channel on the internet (a "Mumble"
   channel — Mumble is a free voice-chat program, and you can think of the channel as an internet
   repeater). While it's linked, your handheld becomes a doorway to that channel — one your phone, your
   friends, or a whole club can join from anywhere on Earth.

You can use any of these, or all three.

---

## The control panel (in your browser)

Open the control panel the same way as in [Try it first](getting-started.md): go to
`http://127.0.0.1:8000` and enter your password. Here's what the main controls do.

- **Monitor** — plays what the radio is hearing, through your computer's speakers. Browsers won't
  play sound until you ask them to, so you click **Listen (receive audio)** once to start it.
  (Nothing plays until you do — that's normal.)
- **Talk** — transmits by letting you speak into **your computer's microphone**. Click and hold to
  talk; radio-server keys the transmitter for you. Two things worth knowing:
  - Talk uses your **computer's** microphone, not the radio's.
  - While you're talking, your own **Monitor** goes quiet so you don't hear an echo of yourself.
    That's intended — if you want to check your transmission, listen on a second radio.
- **PTT** — keys the transmitter directly (PTT is ham shorthand for "push to talk").
- **Status** — shows whether the radio is transmitting, receiving, or idle.
- **Services** — lists the spoken services that are switched on (see "Over the air," below).
- **Log** — a running list of what the station has done (transmissions, logins, and so on).
- **Settings** — change any setting right here in the browser, no file editing needed. See
  [Changing the settings](configuration.md).

There's also a **Mumble link** card. It lists each Mumble channel your station knows about, with a
Connect and Disconnect button for each, and — while you're linked — shows who's in the channel and
lights up whoever is talking right now. While a link is active, **Monitor** and **Talk** work the
channel too: you hear the channel in your browser, and holding Talk speaks into it. In other words,
the browser becomes your Mumble client — nothing extra to install. (More on all this in
"Talking to the world," below.)

> **On a plain Baofeng, the tuning controls are greyed out.** That's expected — a UV-5R holds its own
> dial, so you set the frequency by hand on the radio. Nothing is broken. **If the radio on the other
> end of that AIOC cable is a UV-K5**, it doesn't have to be that way: set `baofeng.uvk5_tuner` and the
> server tunes it over the same cable, controls and all. See
> [Changing the settings](configuration.md#tuning-a-uv-k5-over-the-aioc-cable).

### Tune

On a radio the server can tune, a **Tune** card appears. Everything on it takes effect on the next
transmission, and the card only shows the controls your radio actually supports.

- **Frequency** — where the radio listens.
- **Repeater split** — a separate transmit frequency, for working through a repeater. Leave it off for
  simplex.
- **Tone** — the CTCSS tone a repeater wants to hear before it will open up.
- **Bandwidth** — how much spectrum the radio listens across: **Wide (FM)** or **Narrow (NFM)**.
  Those are the raw names you'll see in `radio.toml` and in a preset, which is why they're in
  brackets here.
- **Demodulation** — what *kind* of signal the radio expects to find: **FM** or **AM**. It's the
  setting that makes airband receivable at all, and it's a different thing from Bandwidth even
  though both of them spell one of their choices "FM". Only appears on a radio that can change it —
  a UV-K5 on F7 firmware over the AIOC cable.

  > **Setting AM stops this station transmitting.** Not a bug and not a limitation of this software:
  > the radio's firmware disables its own transmit path in anything but FM. The Talk button greys
  > out and says so, and the fix is to set Demodulation back to FM.

  **Restarting radio-server puts the radio back on FM.** The demodulator is the one setting the
  radio can't be *asked* about, so rather than guess, the server states FM when it starts — which is
  also the only setting this station can transmit in. If you were listening to airband, tap the
  airband channel again (or set Demodulation to AM) after a restart.

- **Transmit power** — **low**, **mid** or **high**. The radio works out what each one means for the
  band you're on, from its own factory calibration; radio-server doesn't claim a wattage anywhere,
  because it can't read that calibration. Turn it down for a repeater you can hit easily, a crowded
  site, or battery life. The highlighted level is the one the radio **confirmed**, not the one you
  clicked — before the first tune nothing is lit, because the radio is on whatever its front panel
  says and the server can't see that.

> **The Talk button sometimes goes dead for about six seconds after you tune.** That's the radio, not
> a bug. Reading or writing a UV-K5's memory puts its firmware into a serial-configuration state that
> refuses to transmit — and cuts an over already in progress — for six seconds. radio-server knows the
> deadline and greys the button out rather than letting you key into a refusal. It waits it out for
> you when a transmission needs it.
>
> **The other reason Talk goes dead is AM**, and that one won't clear on its own — the button says
> which it is. Set Demodulation back to FM in the Tune card.

### Switching radios

If you have **more than one radio configured** (say a Baofeng on an AIOC cable *and* a KV4P HT), a
**Radio** card appears with a dropdown to pick which one is live. Choose the other radio and the whole
panel follows: the tuning and scan controls appear if the new radio supports them (the KV4P does) or grey
out if it doesn't (the Baofeng) — no page reload needed. The choice sticks across a restart, so the
station comes back up on the radio you last picked.

A couple of things worth knowing:

- **A KV4P switch takes a moment.** The board reboots when it's opened, so expect a short "Switching…"
  pause. If a switch fails cleanly, the panel stays on the radio you had and tells you so.
- **⚠ One transition is known to crash the server: Baofeng → UV-K5.** It doesn't fail cleanly — it
  takes the whole process down (recorded and still open in
  [ADR 0140](adr/0140-the-first-key-is-always-lost.md)). If you run it under systemd it comes straight
  back, but anything in flight is lost. Until it's fixed, make that particular change by setting
  `server.backend` in the settings file and restarting, not from the dropdown.
- **Switching drops whatever you're transmitting.** Changing radios tears down the current one, which
  releases the transmitter. Don't switch mid-transmission unless you mean to cut it off.
- **Both radios must be set up in your config.** A radio only shows up in the dropdown if it has its own
  block in `radio.toml` — see [Changing the settings](configuration.md). If a radio you expect isn't
  listed, its block is missing.

### Channels (presets)

If your radio can tune (a KV4P HT, a UV-K5 — on Dock firmware as its own backend, or over an AIOC with
`baofeng.uvk5_tuner` set — or the practice radio; not a plain UV-5R, which has no tuning control) and
you've named some **channels** in the settings file, a **Channels** card
appears with one button per channel. Tap one and the radio tunes to it — handy for parking on a repeater's
output to listen from the desk. The button for the channel you're currently on lights up; tune somewhere
else (from the Tune card) and it clears on its own.

- The card only shows on a radio that can tune, and only when you've defined at least one channel.
- If a channel carries a setting your radio can't do (say a tone on a radio without tone control), it
  tunes what it can and tells you what it skipped — it never silently half-applies. The one thing it
  *doesn't* announce is a field no backend has ever honoured, such as a receive tone imported from
  CHIRP: that fires on most channels, every single tap, and there is nothing you could do about it
  (ADR 0145).
- **"Save to radio"** decides whether a channel tap is written into the radio's memory or just set
  live. It's **off** by default, which is the fast path — the tune takes effect immediately and the
  Talk button isn't locked out. Turned on, the channel survives a power-cycle, at the cost of a
  six-second lockout after each change. Either way, radio-server re-asserts the channel just before it
  keys, so an over never goes out on a frequency you didn't choose.
- Applying a channel updates **every** browser you have open, live — no reload.
- **Channels are edited in the settings file, not here.** Add a `[[presets]]` block per channel — see
  [Changing the settings](configuration.md#channel-presets). This card is for *using* them.

---

## Calling in over the air

This is the part that makes radio-server fun: someone with a handheld can key a few touch-tones and
hear spoken information read back over the air.

### First, one-time setup

Two things need to be in place before anyone can call in: your **callsign** and a **login code** (a
rolling 6-digit code from a free authenticator app on your phone, the same kind websites use — it's
what stops just anyone from using your station). Setting both up is a quick, one-time job, walked
through step by step in **[Setting it up with your radio](install.md#set-your-callsign-and-login-code)**.
Once that's done, come back here.

### Logging in

On the calling radio, key your current **6-digit code** followed by the **`#`** key. For example, if
your app shows `123456`, you key:

```
123456#
```

The station answers with its ID, and you're logged in. (If you fumble a digit, key **`*`** to clear
and start the code again.) The code changes every 30 seconds, and each one only works once — so even
if someone overhears it, they can't reuse it.

> **Prefer a code that never changes?** You can switch to a **fixed** 6-digit code you pick yourself
> (no phone app needed) — turn on `auth.fixed_code` in the Settings tab and set the code under
> Secrets. Be aware this is **less secure**: a fixed code *can* be reused by anyone who overhears it,
> since it doesn't rotate or expire. See [Changing the settings](configuration.md#fixed-login-code-an-option--less-secure).
> The masthead shows a small padlock chip when a fixed code is in use.

### Asking for a service

Once you're logged in, key a **two-digit code** and **`#`** to make something happen. These are the
defaults — you can change which code does what (see [Changing the settings](configuration.md)):

| Key this | What happens |
|---|---|
| `01#` | You hear the station ID |
| `02#` | You hear the current time |
| `10#` | Links your station to the **Radio Server Demo** Mumble channel (see below) |
| `98#` | Drops the Mumble link |
| `99#` | Logs you out |

You can also add your own spoken services — a weather report, club announcements, whatever you can
imagine — and give each one its own code; [Changing the settings](configuration.md) shows you how.

Your session stays open while you're using it and closes automatically after a few quiet minutes; just
log in again when you want back in.

> **A tip when keying:** hold each tone for about a second.

---

## Talking to the world (the Mumble link)

Key `10#` and something a little magical happens: your station joins a voice channel on the
internet — the **Radio Server Demo** channel. radio-server comes already pointed at it, so there's
nothing to set up. The station confirms out loud — *"Linked to Radio Server Demo."*

Here's the proof, and you can do it tonight, entirely on your own: install the free Mumble app on your
phone, join that same channel (the details are just below), and key `10#` on your handheld. A moment
later your own voice comes out of your phone — your HT to your computer to a server to your pocket,
from anywhere on Earth. When other people are on the channel, they hear you and you hear them the same
way, all over the air.

When you're done, key `98#` and the station says *"Link off."*

> **You can join the same channel from a computer or phone, too.** Install the free
> [Mumble](https://www.mumble.info/) app and connect with:
>
> - **Server:** `104.168.125.41`
> - **Port:** `64738`
> - **Password:** `github.com/kbennett2000/radio-server`
>
> Yes, the password is printed right here on purpose. It's a gate code to keep random bots out, not
> a secret — everyone using radio-server shares it.

A few things worth knowing:

- **`98#` works even when you're not logged in.** If your session timed out while you sat and
  listened to the channel, a bare `98#` still drops the link. Hanging up never needs a login.
- **One link at a time.** If you've added more channels of your own, connecting to one switches away
  from whatever was linked before — like turning the channel knob on a radio.
- **The demo channel is new — it may be quiet, and that's the point.** It's the one server
  radio-server advertises to everybody, on purpose: one shared room that fills up beats fifty
  half-built ones that never do — that's exactly what left DStar, DMR, and Fusion with so many empty
  rooms. So don't wait for a crowd to show up; go call. It's open to everyone trying radio-server —
  treat it like a calling frequency: identify yourself, say hello, be friendly. You're early, not
  alone.

Want a channel of your own — for your club, your family, your weekly net? You can run your own
Mumble server for about two dollars a month, and the [run your own Mumble server](mumble-server/)
guide walks you through it step by step.

---

## Staying legal, automatically

radio-server takes care of the Part 97 basics for you:

- It **identifies your station with your callsign** on the required schedule and when a session ends —
  in Morse code or a spoken voice, your choice. You don't have to remember to ID.
- It won't transmit at all until you've set a callsign, so it can never go on the air unidentified.

---

## A note on privacy (nothing over the air is secret)

This is normal for amateur radio, but worth saying plainly: **everything sent over the air is in the
open.** Anyone with a receiver can hear it. The login code isn't there to keep things secret — it's
there so only you can *use* your station's services, and so an overheard code can't be reused. And
while a Mumble link is active, remember it works both ways: what goes over the air is also heard by
everyone in the internet channel, and what's said in the channel goes out over the air.

The sensible rule: match how much you trust a service to what it can do. Announcing the time is
harmless. Treat anything that keys your transmitter as the thing worth guarding most.

For the full detail on how login, identification, and the operating log work, see the
[operating guide](operating.md).

---

## When something isn't working

The most common hiccup is **"everything's connected but I hear nothing."** That's almost always an
audio-level setting, not a real fault. The [Troubleshooting guide](troubleshooting.md) walks through it
step by step — including how to set your audio levels on Windows, macOS, and Linux.
