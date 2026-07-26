# 0139 — Opening a real repeater: the instrument, and the thing it measures

Status: Accepted

Executes the acceptance ADR [0138](0138-baofeng-mode-proven-on-the-uvk5.md) named and could not
run. Retires the "parked" `[baofeng]` block on the bench box.

## Context

ADR 0138 proved Baofeng mode on the bench: DTR keys the UV-K5 (witness RMS 1926.8 / 1970.5 vs 0.0),
and the real `AiocBaofeng` class puts audio on the air that comes back at 910–927 against a 13–171
control band. It also said, in as many words, what it had not done:

> **No repeater has been opened yet.** Everything here is bench frequencies into a witness inches
> away. The real acceptance is a courtesy tone coming back.

That is the only claim the operator ever asked for, and this ADR is about building an instrument
honest enough to make it.

Baofeng mode is also worth wanting for a reason ADR 0138 did not spell out: it retires all three of
ADR [0134](0134-repeater-keyup-in-the-field.md)'s surviving field hypotheses **by construction**.
The wrong-band PA bias, the split silently cleared before key-up and the TX power the dock cannot
set are all artefacts of the host driving the transmitter. In Baofeng mode the radio's own firmware
sets up TX from its own VFO, exactly as it does under a thumb, and none of the three can happen.

## The measurement

`scripts/bench/repeater_openup.py`. The witness (kv4p, a second radio-server on `:8091`) watches the
repeater's **output**; the station transmits on its **input**, 5 MHz down.

**The verdict comes from the window after our carrier drops.** Our transmitter is off, so anything
on the output frequency then is the machine's hang time, tail or courtesy tone, and nothing else can
produce it.

That is not fastidiousness. A witness sitting inches from a keyed HT can be desensed or
front-end-overloaded by a carrier 5 MHz away, so `busy` going True *while we transmit* is exactly
what a completely dead repeater would also show. It is the one measurement that cannot distinguish
success from failure, and it is the obvious one to reach for. `busy` still True *after* we stop
cannot be us.

`busy` is the SA818's SQ pin — a hardware carrier detect, not a level threshold this project chose,
so no constant of ours stands between the repeater and the verdict. It polls at ~560 Hz.

`t0` is when the transmit request returns, deliberately the late choice: `AiocBaofeng.transmit`
blocks until the clip drains and then drops PTT, so the response comes after un-key. A late `t0`
can only make the test harder to pass. A measurement whose bias points at "fail" is one you are
allowed to believe when it says "pass".

Three overs, ≥5 s apart, each preceded by its own 5 s quiet check; **≥2 of 3** tails to call it
opened, because one tail could be another station keying the machine while we listened.

**The over is the station ID itself** — `StationId.identify()`, CW, through the production path. Part
97-identified by construction, short, and it exercises code that matters instead of a synthetic tone.

## The pre-flight that turned a lie into a finding

The first run reported no carrier on the repeater's input and stopped. That is the whole reason it
exists.

ADR 0138 recorded "no read-back of anything… no path in this repo to read the radio's front-panel
VFO" and left it there — which quietly means *every* Baofeng-mode result has three causes and one
output. So before measuring the machine, this points the witness at the repeater's **input** and
keys once. Our own carrier, from a transmitter inches away, is unmissable, and its presence settles
three things nothing else here can ask:

- the radio is on this repeater's channel (the operator set it right);
- it is not wedged in the dock's `0x0870` loop ignoring its own PTT pin (ADR 0138);
- the AIOC's PTT line and audio path are alive *now*, not last week.

Desense is not a concern in that direction: the witness is supposed to be hearing us.

The unreadable front panel is therefore not unreadable after all. It cannot be read over the wire,
but it can be **measured over the air**, and one keyed ident answers it.

## What the first run measured

`--repeater K0PRA448.525`, station switched to `baofeng` through `POST /radio/select`:

| step | result |
|---|---|
| pre-flight, witness on the input 443.525 | **0.00 s** of carrier — refused to go further |
| verdict | **INCONCLUSIVE**, not NO RESPONSE |

Then the three causes were split by keying once per candidate frequency and asking the witness where
the carrier turned up:

| candidate | carrier | audio RMS |
|---|---|---|
| **445.800** — where ADR 0138 parked it | **1.21 s** | **3816.5** |
| 443.525 — K0PRA's input | 0.00 s | — |

**The radio was still on 445.800.** Two findings fall out of that:

1. **The live backend switch un-wedges the radio.** `POST /radio/select` reaches `Uvk5Radio.close()`
   (`backends/uvk5/radio.py:1189`), which sends `ExitHwMode` (`0x0871`) and returns the radio to
   standalone operation. The radio keyed on the first attempt afterwards. The ADR 0138 wedge is
   specific to a *service stop* landing mid-handshake; the API path does not have it. This matters
   because a wedge and a dead AIOC are indistinguishable at the witness, and the mode switch
   operators will actually use is the one that is safe.
2. `transmit()` blocked **1.51 s** — exactly the configured 1.0 s TX lead plus a 80 wpm CW "AE9S".
   The blocking contract and the audio path were both correct while the run was producing nothing,
   which is precisely the shape that reads as a broken transmitter.

Without the pre-flight this run would have reported **NO RESPONSE** for `K0PRA448.525` — a confident,
false claim about somebody's repeater, produced by a station that was transmitting perfectly well
5 MHz away. That is the ADR 0136 / 0137 / 0138 error class for the fourth time, and the first time
it was caught before it reached the output.

## Decision — the `[baofeng]` block is the same cable, and says so

The bench box's `[baofeng]` section was commented out and parked "until the second AIOC arrives", on
the reasoning that a `[baofeng]` block could only mean a second cable to a UV-5R, so pointing it at
the K6's AIOC "would key the WRONG radio". ADR 0138 inverted that premise: keying the K6 through its
own AIOC is the intended configuration.

It is now active, pointing at the same `serial_port` and the same `AIOC_K6` sound card as `[uvk5]`,
with a comment that says what is true — one cable, one radio, two mutually exclusive ways to drive
it, switched deliberately through `POST /radio/select`. `baofeng` is a configured switch target;
`server.backend` still rests at `uvk5`.

## Decision — the UI stops hiding where it transmits

In Baofeng mode `TuneControls` said only *"Not supported on this radio (audio-only backend)"*. True,
and useless: it describes what the UI cannot do and says nothing about what the radio **will** do.
Since the frequency is now the operator's front panel, and `set_frequency` being absent also hides
the radio face's LCD, a screen that merely omitted the frequency gave an operator no reason to think
one was involved — immediately above a button that transmits on it.

The notice now names the contract, and `StatusPanel` grows a **Transmits on — set on the radio, not
readable from here** row, marked as a warning rather than dropped. Hiding a fact the operator must
go and physically check is the one thing the capability-greying model must not do.

## What this does not claim

- **No repeater has been opened yet.** The instrument is built, proven to refuse rather than guess,
  and the station is confirmed transmitting — but on the wrong frequency for the test. The
  acceptance re-runs once the radio is on `K0PRA448.525`, and this ADR will be extended with the
  numbers, whatever they are.
- **Absolute RF power is still unmeasured** — ADR 0137's open gap, unchanged. If K0PRA opens, that
  retires the question in the only unit that matters without a wattmeter. If it does not, the gap is
  still there and still unaddressed.
- **Nothing on 2 m.** The kv4p is SA818-UHF (400–480 MHz), so the pre-flight, the tail measurement
  and the instrument check are all unavailable on the 15 VHF repeaters. They remain inferred.
- Whether the dock's full-control loop also starves the **keypad** scan is untested. It starves the
  main loop and the main loop scans both, so the station was left in Baofeng mode while the operator
  set the channel — a precaution, not a measurement.
