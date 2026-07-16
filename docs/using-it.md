# Using your station

There are two ways to use radio-server:

1. **From your browser** — a control panel on your home network, where you can listen, talk, and see
   what's happening.
2. **Over the air** — where you (or people you trust) call in from a handheld and hear spoken
   information back, like the current time.

You can use either, or both.

---

## The control panel (in your browser)

Open the control panel the same way as in [Try it first](getting-started.md): go to
`http://127.0.0.1:8090` and enter your password. Here's what the main controls do.

- **Listen** — plays what the radio is hearing, through your computer's speakers. Browsers won't play
  sound until you ask them to, so you click **Listen** once to start it. (Nothing plays until you do —
  that's normal.)
- **Talk** — transmits by letting you speak into **your computer's microphone**. Click and hold to
  talk; radio-server keys the transmitter for you. Two things worth knowing:
  - Talk uses your **computer's** microphone, not the radio's.
  - While you're talking, your own **Listen** goes quiet so you don't hear an echo of yourself. That's
    intended — if you want to check your transmission, listen on a second radio.
- **PTT** — keys the transmitter directly (PTT is ham shorthand for "push to talk").
- **Status** — shows whether the radio is transmitting, receiving, or idle.
- **Services** — lists the spoken services that are switched on (see "Over the air," below).
- **Log** — a running list of what the station has done (transmissions, logins, and so on).
- **Settings** — change any setting right here in the browser, no file editing needed. See
  [Changing the settings](configuration.md).

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

Once you're logged in, key a **single digit** and **`#`** to hear something. These are the defaults —
you can change which digit does what (see [Changing the settings](configuration.md)):

| Key this | You'll hear |
|---|---|
| `1 #` | The current time |
| `2 #` | The weather |
| `3 #` | Sunrise, sunset, and moon information |
| `4 #` | The station ID |
| `5 #` | A random quote |
| `6 #` | Battery status |
| `7 #` | A Bible verse |
| `99 #` | Log out |

The weather, quote, battery, and Bible services read from other services on your home network, so
they only work if you've pointed radio-server at them ([Changing the settings](configuration.md)
explains how). If one isn't set up, its key simply does nothing — the time and station ID always work.

Your session stays open while you're using it and closes automatically after a few quiet minutes; just
log in again when you want back in.

> **A tip when keying:** hold each tone for about a second, and if your code has the same digit twice
> in a row (like `4 4`), pause briefly between them so both register.

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
there so only you can *use* your station's services, and so an overheard code can't be reused.

The sensible rule: match how much you trust a service to what it can do. Announcing the time is
harmless. Treat anything that keys your transmitter as the thing worth guarding most.

For the full detail on how login, identification, and the operating log work, see the
[operating guide](operating.md).

---

## When something isn't working

The most common hiccup is **"everything's connected but I hear nothing."** That's almost always an
audio-level setting, not a real fault. The [bench setup & troubleshooting guide](hardware-bringup.md)
walks through it step by step.
