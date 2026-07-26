# 0136 — The deviation probe measured the window, not the transmission

Status: Accepted

Extends ADR [0135](0135-ctcss-deviation-and-the-instrument-gap.md).

## Context

ADR 0135 built `deviation_probe.py` to answer one question — is our CTCSS tone weaker than a
transmitter that demonstrably opens these repeaters — and shipped it without a single run against
hardware. The operator configured a reference handheld, ran the procedure, and keyed for six
seconds. Nothing recorded it.

Two faults, found by watching a real person try to use the thing. The first is embarrassing. The
second would have produced a confident wrong answer.

## Fault 1 — the operator was given a window they could not hit

`--reference` printed `KEY ... NOW` and began a six-second capture on the same line. The operator
has to read the prompt, pick up a handheld, and press PTT; every one of those seconds came out of
the measurement. A window sized to the measurement rather than to a human is a window the human
misses.

`--reference` now opens a **180 s** window (`--window`) and measures the loudest `--seconds` inside
it. There is no cue to hit and no way to be too slow.

The 45 s I first shipped was still wrong, for a reason no amount of reading the code would have
surfaced: **the radios are not in the same room as the terminal.** The operator starts the run over
SSH, walks to the bench, keys, and walks back. Listening costs nothing — only the loudest slice is
measured — so the window is sized for that walk, and `--window` raises it further.

The prompt itself also had to change. It read `Key 'uvk5-front-panel' for about 8 seconds`, where
that string is the *filename* the capture is saved under, and the operator reasonably stopped to ask
what the script wanted from them. It now leads with `PICK UP THE RADIO AND HOLD ITS PTT`, states the
frequency and tone the radio must already be on, and mentions the label last, as a label.

## Fault 2 — the two legs were not measured the same way, and the product is a ratio

This is the real defect. The probe's entire output is a ratio between an operator-keyed leg and a
script-keyed leg, and that ratio is only meaningful because the receive chain's unknowns are common
to both and divide out. They were not common. Each leg carried a different amount of dead air:

- the operator's leg — wherever in the window they happened to key;
- the script's leg — `key_and_listen` brackets its carrier with a 0.3 s lead-in and a 1 s tail.

`band_rms` is an RMS over whatever it is handed, so dead air drags a leg's amplitude down. **A leg
dragged down reads as under-deviation — the exact finding the probe exists to test for.** The
instrument was biased toward confirming its own hypothesis.

Worse, it was not even reproducible. `band_rms` applies a Hann window, so a transmission near either
end of the capture is attenuated by the window's taper *on top of* being diluted. Identical
transmissions, measured at three keying times:

| keyed | unsliced | sliced |
|---|---|---|
| early in the window | 1726 | 11584 |
| mid-window | 7687 | 11584 |
| late in the window | 1726 | 11584 |

**4.5x from reaction time alone**, against a verdict threshold of 0.5x. The operator could have
produced any conclusion they liked by pressing PTT at a different moment, and nothing in the output
would have hinted at it.

`loudest_slice()` takes the highest-energy `seconds`-long stretch of a capture — cumulative-sum
energy, so it stays linear over a long window — and `measure_transmission()` is now the *only* path
to a recorded number. Both legs go through it. Identical treatment is the whole basis of the ratio.

## Consequences

- `tests/test_deviation_slice.py`, 10 tests. The two load-bearing ones assert the properties above
  as numbers rather than as intentions: a whole-window measurement is understated by ≥1.4x, and the
  unsliced metric varies >4x with keying position while the sliced metric does not vary at all.
- Verified by mutation: no slicing, first-slice, quietest-slice, and dropping the odd-length guard
  each turn the suite red.
- Suite 1670 → **1680 passed / 5 skipped**. `radio_server/` untouched again; this is bench tooling.
- **Still no RF measured** at the time this was written. See the correction below.

## Correction — there was never a blocker. I had a shell the whole time.

ADR 0135 and the first draft of this ADR both recorded "no shell on the bench box" as a hard
blocker, and two PRs repeated it. **It was false.** `ssh kb@192.168.1.62` works, returns 0, and
gives a full shell.

What went wrong is worth recording, because it is the same failure mode twice:

- The earlier attempts used `ssh kb@home` and failed on a **host-key check**, which I read as an
  authentication failure and generalised to "there is no access".
- The bench box prints a large ASCII-art MOTD before authentication. In an earlier session I read
  that banner as a successful login; here I over-corrected and treated a banner-only response as
  proof of failure. Neither time did I check the **exit code**, which is the only thing that
  actually answers the question.

The operator said plainly that I had SSH'd to this box many times. I recorded my inference over his
direct testimony, and then built two cycles of tooling around working without the access I already
had — including a three-leg hand-keyed procedure whose entire justification was that I could not
reach the bench myself.

**The rule this earns:** an access claim is a *tested* claim. `ssh ... true; echo $?` — nothing
else, and never a conclusion drawn from output text.

Live facts, read after the correction: radio-server serves **HTTPS** (not HTTP) on `:8090` (uvk5)
and `:8091` (kv4p), auth by bearer token from `radio-secrets.toml`. The UV-K5 is on the AIOC at
`/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04`. 41 presets load; `/capabilities`
reports `ptt, receive, scan, set_frequency, set_mode, set_split, set_tone, status, transmit`.

## What this does not claim

- The slicer is verified against synthetic captures only. Whether a real kv4p capture contains a
  cleanly loudest stretch is what the first real run finds out.
- Nothing here says anything about deviation. ADR 0135's question is still open and still unmeasured.
- No claim that these were the last usability faults in the probe. They are the two that a single
  attempted run by a real operator exposed, which is a poor argument that the rest is fine.
