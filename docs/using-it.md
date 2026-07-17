# Using your station

There are three ways to use radio-server:

1. **From your browser** — a control panel on your home network, where you can listen, talk, and see
   what's happening.
2. **Over the air** — where you (or people you trust) call in from a handheld and hear spoken
   information back, like the current time.
3. **Linked to the world** — your station can join a voice channel on the internet (a "Mumble"
   channel — Mumble is a free voice-chat program, and you can think of the channel as an internet
   repeater). While it's linked, your handheld becomes a doorway to a worldwide conversation.

You can use any of these, or all three.

---

## The control panel (in your browser)

Open the control panel the same way as in [Try it first](getting-started.md): go to
`http://127.0.0.1:8000` and enter your password. Here's what the main controls do.

- **Monitor** — plays what the radio is hearing, through your computer's speakers. Browsers won't
  play sound until you ask them to, so you click **Monitor** once to start it. (Nothing plays until
  you do — that's normal.)
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

> **On a Baofeng, the tuning controls are greyed out.** That's expected — the cable doesn't control
> the dial, so you set the frequency by hand on the radio. Nothing is broken.

---

## Calling in over the air

This is the part that makes radio-server fun: someone with a handheld can key a few touch-tones and
hear spoken information read back over the air.

### First, one-time setup

Two things need to be in place (both covered in [Setting it up with your radio](install.md)):

- **Your callsign**, so the station identifies itself legally.
- **A login code**, using the free **Google Authenticator** app on your phone — the same kind of
  6-digit code many websites use. This is what stops just anyone from using your station.

### Logging in

On the calling radio, key your current **6-digit code** followed by the **`#`** key. For example, if
your app shows `123456`, you key:

```
1 2 3 4 5 6 #
```

The station answers with its ID, and you're logged in. (If you fumble a digit, key **`*`** to clear
and start the code again.) The code changes every 30 seconds, and each one only works once — so even
if someone overhears it, they can't reuse it.

### Asking for a service

Once you're logged in, key a **two-digit code** and **`#`** to make something happen. These are the
defaults — you can change which code does what (see [Changing the settings](configuration.md)):

| Key this | What happens |
|---|---|
| `0 1 #` | You hear the station ID |
| `0 2 #` | You hear the current time |
| `1 0 #` | Links your station to the **Radio Server Demo** Mumble channel (see below) |
| `9 8 #` | Drops the Mumble link |
| `9 9 #` | Logs you out |

You can also add your own spoken services — a weather report, club announcements, whatever you can
imagine — and give each one its own code; [Changing the settings](configuration.md) shows you how.

Your session stays open while you're using it and closes automatically after a few quiet minutes; just
log in again when you want back in.

> **A tip when keying:** hold each tone for about a second, and if your code has the same digit twice
> in a row (like `4 4`), pause briefly between them so both register.

---

## Talking to the world (the Mumble link)

Key `1 0 #` and something a little magical happens: your station joins a voice channel on the
internet — the **Radio Server Demo** channel. radio-server comes already pointed at it, so there's
nothing to set up. The station confirms out loud — *"Linked to Radio Server Demo."* — and from that
moment, what you say into your handheld goes to everyone in the channel, and their voices come back
to you over the air. Your little HT just reached the whole world.

When you're done, key `9 8 #` and the station says *"Link off."*

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

- **`9 8 #` works even when you're not logged in.** If your session timed out while you sat and
  listened to the channel, a bare `9 8 #` still drops the link. Hanging up never needs a login.
- **One link at a time.** If you've added more channels of your own, connecting to one switches away
  from whatever was linked before — like turning the channel knob on a radio.
- **It's a shared channel, so bring your on-air manners.** The demo channel is open to everyone
  trying radio-server. Treat it like a calling frequency: identify yourself, say hello, be friendly.

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
audio-level setting, not a real fault. The [bench setup & troubleshooting guide](hardware-bringup.md)
walks through it step by step.
