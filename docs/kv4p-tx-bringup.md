# Bringing up transmit on a KV4P HT board

Everything up to here has proved the board **receives** — it connects, reports its state, and hands
you audio. This guide is the other half: proving it **transmits** cleanly before you ever key on the
air. It's a short, ordered bench session with the check-up tool doing the work; you just watch and
write down what you see.

You do this once, into a **dummy load**, with a **second receiver** nearby tuned to the board's
frequency. If you don't have a dummy load, stop here and get one — these steps key a real transmitter.

> **Before you start:** if radio-server is running, **stop it first** (`systemctl stop radio-server`,
> or just quit it). The board's USB port is single-open — the running server owns it, and the check-up
> tool can't share it. Opening the port also reboots the board (that's normal, see the setup guide).

The check-up tool will not let this go wrong quietly. Every transmitting step **refuses to run unless
you're sitting at the keyboard** (it won't key from a script or a scheduled job), makes you **type
`CONFIRM`** first, and **holds the transmitter for only a couple of seconds** no matter what. Those
guardrails are deliberate — don't work around them.

---

## Step 1 — Does it key at all?

```sh
uv run python -m radio_server.doctor --backend kv4p --key-test
```

This asks the board to transmit into the dummy load for ~2 seconds and confirms it actually did. A
clean run prints **`TX_ACTIVE confirmed`** with how long the key-up took (e.g. *keyed in 40 ms*), then
**`unkeyed cleanly`**.

If instead you see **`keying REFUSED by the device`**, the board accepted the request but never
transmitted. Almost always that's the **transmit-allowed gate**: the firmware keeps a "TX allowed"
flag that defaults *off*, and radio-server leaves it off until you turn it on. Set `kv4p.tx_allowed =
true` in your settings and try again. If it still refuses, the RF module itself needs checking.

**Watch the dummy load's power meter / the board's TX LED** — seeing it key is the real confirmation;
the printed line only says the board *reported* it keyed.

---

## Step 2 — Does audio actually reach the air?

With a second receiver on the board's frequency:

```sh
uv run python -m radio_server.doctor --backend kv4p --tx-tone --freq 1000 --seconds 3
```

This keys the board and plays a 1000 Hz test tone. You should **hear the tone on the other radio** —
that's the acceptance test; the tool sending bytes only proves it *tried*.

Afterward it prints a **TX telemetry** summary — the numbers this bring-up exists to capture:

```
TX telemetry (75 Opus frames over ~3.0s):
  encoded bytes/frame : min 28  mean 41.6  max 63
  on-wire bytes/frame : mean 50.6  (escaped + FENDs — what the window spends)
  frames per 2048-byte window : ~40.5
  window never blocked (min credits 1997) — the write timeout was never neared.
```

- **encoded bytes/frame** — how big each 40 ms Opus packet actually is (narrowband, variable-rate).
- **frames per window** — how many such frames fit the board's flow-control buffer.
- **window never blocked** vs **window BLOCKED on N frame(s)** — whether the buffer ever filled and
  made the tool wait. "Never blocked" is what you want; if it *did* block, the board's real buffer is
  smaller than assumed or it's slow to acknowledge frames, and that's the thing to chase.

If you hear nothing, the tool tells you what to check (did it key at all — Step 1; is `kv4p.tx_allowed`
on; is the second receiver really on the board's frequency).

---

## Step 3 — Settle the key-up lead-in

When the board keys, the transmitter takes a moment to stabilize. radio-server plays a short burst of
silence first (`kv4p.tx_lead_seconds`) so the *start* of your audio isn't clipped off. The right value
is a hardware fact — too short and the first syllable is cut, too long and you waste air. Find it by
sweeping, listening for a clean tone onset on the second receiver:

```sh
uv run python -m radio_server.doctor --backend kv4p --tx-tone --tx-lead 0.05
uv run python -m radio_server.doctor --backend kv4p --tx-tone --tx-lead 0.10
uv run python -m radio_server.doctor --backend kv4p --tx-tone --tx-lead 0.20
```

`--tx-lead` overrides the setting for that one run, so you can sweep without editing your config
between keyings. The smallest value whose tone begins cleanly (no clipped start) is the one to put in
`kv4p.tx_lead_seconds`.

---

## What to write down

These are the numbers that turn marked guesses into bench facts:

- Did it key, and the **key-up latency** (Step 1).
- **Encoded bytes/frame** and **frames per window**, and **whether the window ever blocked** (Step 2).
- What the **second receiver heard** — clean tone, distorted, silent (Step 2).
- The **`tx_lead` value** that stopped clipping the start (Step 3).

---

## Surprising-but-normal

- **The board reboots when the tool connects.** Opening the USB port resets the ESP32 — expected.
- **The board self-drops a stuck key after ~200 s.** The firmware has its own runaway-transmit cutoff,
  well above these 2–5 second tests. You'll never reach it here; it's a backstop, not something to
  fight.

## Where to go next

- **[Setting up a KV4P HT board](kv4p-setup.md)** — flashing and first connect (do this first).
- **[Changing the settings](configuration.md)** — where `kv4p.tx_allowed` and `kv4p.tx_lead_seconds`
  live.
