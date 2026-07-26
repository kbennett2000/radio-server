# Setting up D-STAR and the DVAPs

> **For operators, and it starts with a warning rather than a wiring diagram.** That ordering is
> deliberate: the part of this feature that connects a reflector to your transmitter has stuck the
> transmitter keyed on the air, more than once, and its bench re-proof has never passed. Read
> [The posture](#the-posture-read-this-before-you-set-dstarcallsign) before you configure anything.

---

## What this actually is — four separate things

They share two config blocks and two UI cards, which makes them look like one feature. They are not,
and they carry very different risk.

| | What it does | Keys your transmitter? |
|---|---|---|
| **1. The gateway link** | radio-server registers as a homebrew (DSRP) repeater endpoint on a G4KLX **ircDDBGateway** and links/unlinks reflectors | no, by itself |
| **2. Browser listen / talk** | you hear the reflector in your browser, and your computer's mic goes to the reflector | **no — never** |
| **3. The crossband** | reflector audio is decoded and **transmitted on RF**, and RF audio is encoded back to the reflector | **YES** |
| **4. The DVAPs** | link/unlink separate `dstarrepeater` daemons driving DV Access Point dongles, over the gateway's remote-control interface | no — radio-server carries no audio or PTT for these at all |

**The DVAPs (4) are the safe, boring one.** radio-server is only a remote control there: it sends a
link command to the gateway and reads the confirmed state back. Nothing of yours transmits. If all you
want is to point a DVAP at a reflector from the browser, you can skip most of this guide — see
[Just the DVAPs](#just-the-dvaps).

**Items 1, 2 and 3 arrive together.** That is the thing to understand before you start.

---

## The posture — read this before you set `dstar.callsign`

### There is no browser-only mode

An earlier design had a `dstar.operator_tx` switch that let you listen and talk from the browser
without ever keying RF. **It was removed** ([ADR 0089](adr/0089-dstar-folded-shared-dongle.md)) when
the crossband and the browser path were folded onto one shared DV Dongle, and nothing replaced it.

Concretely, in today's code: the bridge's `tx_to_rf` defaults to `True` and the app never passes
anything else. So:

> **Setting `dstar.callsign` and clicking Connect arms reflector→RF transmission.** The link *is* the
> arming switch. The only "off" is leaving `dstar.callsign` blank — which is the default, and is how
> this station currently ships.

### It has stuck the transmitter keyed, and the re-proof has never passed

This is not a theoretical hazard. Across three bench sessions the reflector→RF path stranded the
transmitter keyed **at least five times** (ADRs 0090, 0092, 0093, 0097, 0099). During that period:

- `POST /ptt {"on": false}` returned **`200`** and `status.transmitting` read **`false`** while the
  carrier stayed up. Only killing the process — closing the serial port — dropped PTT.
- A graceful `systemctl stop` once hung ~15 s before the transmitter unkeyed.

Two supervised dummy-load re-proofs have been attempted. **Both failed.** The first stuck-keyed; the
second put dead air on the air, stayed keyed, and hung the stop. Four further ADRs (0105–0108) then
fixed real, measured defects found *after* that second failure — none of which has been through a
dummy-load proof either.

**The standing gate, unchanged since ADR 0091 and restated in every D-STAR ADR since:**

> The crossband stays disabled on the live radios. Re-enabling is gated on a **joint** dummy-load
> re-proof — a human watching, a listen-only phase first, and a **cold-booted DV Dongle** (physically
> unplug and replug it; never reuse one across a service restart).

Nothing in this guide opens that gate. If you are bringing this up, do it on a **dummy load**, with
somebody watching the radio, and know how to stop the service before you start.

### Linking silently re-points your microphone

One more surprise worth knowing: while a reflector is linked, the browser's **Monitor** and
**Transmit** cards switch from RF to the reflector. There is no separate toggle — link state alone
does it. The card titles change to "Monitor — D-STAR" and "Transmit — D-STAR", and that is the only
signal you get.

### The safety bounds, in the order they fire

If you do run the crossband, these are what stand between a wedged decoder and an open carrier. Each
one is configurable and each accepts `0` to **disable it** — don't.

| Bound | Default | Cuts an over when |
|---|---|---|
| `dstar.tx_hang` | 1.0 s | frames stop arriving from the reflector |
| `dstar.dead_air_seconds` | 10 s | the over is keyed but nothing has passed the content gate |
| `dstar.max_over_seconds` | 60 s | a single over has simply run too long — armed at key-up, **never** reset per frame |
| `tx.tot` | 180 s | the absolute cap on continuous key, on **every** path, from its own timer thread |

The timer-thread detail matters: `tx.tot` fires even when the keying path is parked in a wedged
decode, which is exactly the failure that stranded the carrier. When it fires it publishes an
**`alarm`** event (`{"kind": "tx_timeout"}`) on `/events` — that is the one to alert on.

There is also a ground-truth check for "is it *really* unkeyed": `GET /diagnostics/ptt-line` reads the
kernel's view of the serial control line rather than the server's own bookkeeping. It exists because
the bookkeeping has lied.

---

## Hardware

**For the gateway link, browser audio and crossband (1–3):**

- A **DV Dongle** (Internet Labs) — a DVSI **AMBE2000** vocoder chip behind an FTDI serial port. This
  is what turns AMBE into PCM and back. radio-server opens it **exclusively**, so only one process can
  hold it; a second one gets a clean `503` rather than a corrupted stream.
- A **G4KLX ircDDBGateway**, with a repeater band configured for your callsign and module, pointed
  back at radio-server's `dstar.local_port`.
- Whichever radio backend will be keyed.

**For the DVAPs (4):**

- One or more **DV Access Point dongles**, each driven by its own `dstarrepeater` daemon — *not* by
  radio-server.
- The gateway's **remote control** enabled (`remoteEnabled=1`, `remotePort`, `remotePassword`).

---

## Configuration

Everything is off until `dstar.callsign` is set. It has no default.

```toml
[dstar]
callsign = "N0CALL"            # THE MASTER SWITCH. Blank (default) = the whole feature is off.
module = "A"                   # your module letter on the gateway; must not collide with a DVAP's
gateway_host = "127.0.0.1"
gateway_port = 20010           # the gateway's homebrew-repeater port
local_port = 20012             # must match the gateway's repeaterPort for this band
vocoder_port = "/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_…-if00-port0"
reflector = ""                 # optional boot auto-link; leave blank during bring-up
tx_hang = 1.0
max_over_seconds = 60.0        # safety bound — 0 disables it
dead_air_seconds = 10.0        # safety bound — 0 disables it
```

Pin `vocoder_port` to a `/dev/serial/by-id/…` path. A bare `/dev/ttyUSB1` moves between reboots and
between dongles, and on a bench with more than one FTDI device it will eventually point at the wrong
one.

```toml
[dvap]
host = "127.0.0.1"
port = 10022                   # the gateway's remote-control port

[[dvap.modules]]               # this array is the DVAP master switch
module = "B"
label = "DVAP B"
frequency_hz = 441600000

[[dvap.modules]]
module = "C"
label = "DVAP C"
frequency_hz = 441000000
```

The gateway's remote-control password is a **secret**, so it goes in `radio-secrets.toml` as
`dvap_remote_password`, never in `radio.toml`. If the modules are configured and the password is
missing, the server logs a warning and the DVAP card fails auth against the gateway.

Every one of these keys except `dstar.callsign` is an *advanced* setting, so the browser's Settings
screen files them behind the advanced toggle.

---

## Bringing it up

Each step is **no-RF until the last one**. Do not skip ahead — this order exists because the failures
above were found by skipping ahead.

| # | Command | Proves | Keys RF? |
|---|---|---|---|
| 1 | `python -m radio_server.doctor --vocoder-loopback --vocoder-port <path>` | the DV Dongle is alive and round-trips PCM ⇄ AMBE | no |
| 2 | `python -m radio_server.doctor --vocoder-latency` | the decode pipeline's lag, re-measured rather than assumed | no |
| 3 | `python -m radio_server.doctor --dstar-echo --dstar-host 127.0.0.2` | the DSRP wire format, against a **throwaway** echo gateway. Point it at `127.0.0.2` so production is untouched | no |
| 4 | `python -m radio_server.doctor --dstar-browser-echo` | the browser listen/talk path end to end | no |
| 5 | `scripts/bench/dstar_decode_selftest.py` | decoded audio actually comes out of `/audio/dstar/rx` | **`real` mode does** |
| 6 | the joint dummy-load re-proof | the crossband closes its over and drops PTT | **yes** |

**Step 5 is mandatory before handover** for any deploy that touches `radio_server/dstar/` or
`radio_server/vocoder/` ([ADR 0108](adr/0108-decode-starvation-alarm.md)). Its `real` mode feeds
pseudo-random AMBE, which decodes to full-scale noise, opens the content gate, and **keys the
crossband** — run it into a dummy load.

Then confirm registration: the gateway log should show your poll **accepted**, with no
`Unknown packet from the Repeater` lines, and `GET /dstar/status` should show `rx_frames` climbing on
an inbound over. `registered` in that block is **display-only** — nothing gates audio on it, and it
can legitimately read `false` on a quiet reflector.

### Just the DVAPs

If you only want DVAP control, you can have it without any of the above: configure `[dvap]` and
`[[dvap.modules]]`, put the password on the secrets channel, and **leave `dstar.callsign` blank**.
The DVAP card works, nothing of yours transmits, and the crossband is never constructed.

---

## The browser

- **D-STAR reflector** card — a status pill, two preset buttons, a free-text reflector box, Connect
  and Disconnect. Hidden entirely when D-STAR is unconfigured. Its footer says what it means:
  **the link state shown is what was last sent, because the gateway does not report it back.** A
  dropped command leaves the card confidently wrong.
- **DVAP** card — one row per module with a confirmed status pill, a reflector box and Connect /
  Disconnect. This one *is* read back from the gateway, so it can be trusted.
- **Reflector activity** — the stations heard on the linked reflector, your own overs marked `you`.

---

## Gotchas that have cost real time

- **The DVAPs go silently deaf.** Two ways: a roughly hourly USB re-enumeration that leaves
  `dstarrepeater` on a stale file descriptor, and a first-open-after-abrupt-close that *alternates*
  working and wedged — so a single unverified restart heals about half the time **and can wedge a
  healthy dongle**. `scripts/dvap-autoheal.sh` restarts until the log confirms the dongle actually
  opened. Install it ([ADR 0100](adr/0100-dvap-autoheal-usb-wedge.md)).
- **The DV Dongle wedges after an abrupt kill.** That is why the stop path is bounded and why
  `TimeoutStopSec=20` is in the unit — a SIGKILL severs the dongle mid-operation and poisons the next
  session. Cold-boot it (unplug/replug) before re-testing.
- **One process at a time on the dongle.** Stop any other radio-server instance before running the
  `doctor --vocoder-*` or `--dstar-*` checks; they all want it.
- **Check both ends are on the same reflector.** One "nothing heard" session turned out to be a DVAP
  linked to a different reflector than the one being watched, not a decode fault.
- **Don't transform the AMBE bytes.** The DVAP firmware already de-scrambles and de-interleaves them.
  Adding a transform "to fix the garbage decode" is the wrong fix — the real cause was pipeline
  ordering ([ADR 0098](adr/0098-dstar-reflector-decode-pipeline-alignment.md)).
- **Every layer here is tested against fakes.** [ADR 0092](adr/0092-dstar-parked-decode-ptt-safety.md)
  is the standing reminder that the fakes could not reproduce the hardware failure that stuck the key.
  A green suite is not a dummy-load proof.

---

## See also

- [api.md](api.md#d-star-adr-00860109) — the `/dstar/*` and `/dvap/*` routes and the D-STAR sockets.
- [configuration.md](configuration.md) — every other config block.
- [ADR 0086–0109](adr/) — the decisions, the failures, and what each fix actually measured.
