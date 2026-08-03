# ADR 0176 — The broadcast-FM cadence gets the same transmit exception, and what the 18/18 experiment never measured

**Status:** Accepted · 2026-08-03 · generalises [ADR 0175](0175-signal-strength-on-the-deployed-backend.md) ·
narrows the scope of the 18/18 claim from [ADR 0142](0142-the-server-picks-the-repeater.md)/0144 ·
the cadence itself is [ADR 0163](0163-a-cadence-for-the-probe.md)

## Context

ADR 0175 gave `RssiPoller` a `paused` hook after measuring that a register cadence running through
an over cut a 4.4 s transmission to 0.70 s at the witness. It fixed the cadence it had just built
and left the older one alone. Its own finding 1 said what should have happened next: *"Anything else
that puts steady serial traffic on the AIOC needs the same treatment or the same measurement."*

`BroadcastFmPoller` is that anything else. It polls `0x0879` on the same handle every 2.0 s, it has
no pause of any kind, and its lifecycle is the worst possible one for this hazard: the refcount is
raised on **any bridge connect** — unconditionally, not gated on `tx_to_rf` (`link/bridge.py:267`) —
and held for the life of the bridge. So it runs straight through every relayed over and every
unattended station ID, which is to say **precisely the transmissions a link exists to carry**.

## The numbers did not transfer, so they were measured

ADR 0175's figures are for a different frame at a different rate. `build_frame` costs
`12 + len(params)` on the wire:

| | request | reply | per exchange | interval | steady state |
|---|---|---|---|---|---|
| `ReadRegisters` (ADR 0175) | 16 B | 16 B | **32 B** | 0.5 s | 64 B/s |
| `ProbeBroadcastFm` | 18 B | 20 B | **38 B** | 2.0 s | 19 B/s |

Larger per exchange, a quarter as often — so if the hazard scaled with bytes per second it might not
reproduce at all. ADR 0163 B4 records that the cadence *"was not running during any acceptance
run"*, so nobody had ever looked.

**Three arms, because two would not have settled it.** This cadence cannot be switched on without a
bridge, and a running bridge is itself a confound. The lever was a Mumble server already running in
Docker on the box and the station's own `Toby Jones LAN Mumble Server` entry — no external service,
no reflector. D-STAR could not be used: it raises the refcount only inside its `rx_to_reflector`
branch, and crossband has been disabled on this station since ADR 0099.

| arm | code | link | cadence | `tx` 1000 Hz | `tx` audio | announcement | speech |
|---|---|---|---|---|---|---|---|
| **B** baseline | master `e3d0515` | down | not running | 0.989 | **4.42 s** | 5.4 s | 0.98 |
| **A** hazard | master `e3d0515` | **up** | running, unpaused | 0.979 / 0.984 | **2.01 / 2.61 s** | 0.8 / 1.6 s | 0.45 / 0.72 |
| **A′** control | this branch | **up** | **paused while keyed** | 0.989 | **4.42 s** | 5.2 s | 0.97 |

`A` was run twice because the first result had to be shown to be reproducible rather than a fluke.
**`A′` vs `A` is what settles it** — same bridge, same link, same code everywhere else, only the
pause differing — and A′ lands on B's numbers to the byte (434852 B against 434854 B). The bridge is
exonerated; the cadence is convicted.

**The failure has a different shape from ADR 0175's, and the shape is informative.** There, tone
recovery collapsed to 0.026 — the audio that arrived was corrupted. Here recovery stays at
0.979-0.984 while barely half the audio arrives: the transmission is **cut short**, not garbled.
Four contention events per over rather than nine, each landing hard enough to end the stream.

The cadence's own counters corroborate the mechanism from the inside: across arm A its `unknown`
climbed 8 → 13, and in A′ it sat at 3. Those unknowns *were* the mid-over polls.

## Skipping costs nothing, and here the claim is exact

ADR 0175's version of this argument is that `status()` reports `None` while keyed anyway. That is
about a different consumer and could not be carried across, so it was checked from source. It comes
out **stronger**, on three legs:

1. **No key-up path can reach the poller.** `_key_on` → `_clear_if_deafened` ends at
   `refuse_if_deafened(getattr(tuner, "broadcast_fm", None))` — the *tuner's* block. The poller
   holds two closures over `radio` and has no reference to the tuner at all, and
   `clear_broadcast_fm` is documented in two places as the sole writer of that block precisely so
   that no amount of polling can refuse a key-up.
2. **The mute is RF→network only.** `_deafened()` has exactly three call sites —
   `link/bridge.py:340`, `:356` (both in `_rx_to_mumble`) and `dstar/bridge.py:986` (in
   `_rf_to_reflector`). Neither `_mumble_to_rf` nor `_reflector_to_rf` consults it. A transmitting
   station is not receiving, so there are no RF frames to relay and nothing to mute.
3. **The decisive one: a poll during TX cannot learn anything.** The firmware refuses `0x0879` with
   `ERR_TX` for the whole time the station is keyed (`dock.c`'s `ctx->tx_on`), which
   `broadcast_fm_poll.py` has always said in a comment. Every skipped poll could only ever have
   returned `None`, incremented `_unknown`, and held the reading it already had.

**Leg 3 kills the trailing-edge objection this ADR expected to have to price.** A paused cadence and
an unpaused one reach the first post-over poll in *identical* states — previous reading held,
nothing learned. The front-panel `F+0` window does not widen, because master was not detecting a
mid-over `F+0` either. Pausing is **information-identical and strictly better on the wire**.

**Not done: polling once on resume.** It would make the reading fresh sooner than the next tick and
close a window master also has — an improvement beyond the fault, in a cycle that should not mix the
two. Available, and deliberately not taken.

## Decision

1. **`BroadcastFmPoller` takes a `paused` hook**, checked before anything touches the wire — not a
   request, not a non-blocking lock acquire. Skipped rounds counted apart from `unknown`, the
   convention `RssiPoller` set: a deliberate skip and a refusal mean different things, and the ratio
   is how an operator sees that a quiet cadence means a busy transmitter. A hook that raises fails
   *toward* polling, because a mute that silently stops updating is the fault class this repo keeps
   closing.
2. **`AiocBaofeng` grows a public `transmitting` property**, documented as a plain flag read with no
   I/O. This is not decoration: the poller is built one layer up in `create_app` over a generic
   `radio`, and both obvious spellings do I/O — `status().transmitting` performs a **serial register
   read** on the `uvk5` backend, and `ptt_line_asserted` reads the kernel's line state off the
   handle. **A pause check that does I/O to decide whether to do I/O rebuilds the fault one layer
   up.** It reads the flag `_drop_line` clears unconditionally, so a desync can only ever leave the
   cadence quieter, never talking over a transmission.
3. **`app.py` reaches it through `getattr`**, over the same rebound `radio` closure the adjacent
   probe and fallback use, so a `/radio/select` swap is picked up and a backend without the flag is
   inert.

## The 18/18 citation, corrected

`aioc_baofeng.py` cited `uart_while_streaming.py` in two places for a broader claim than it
measured. That experiment asked **one direction of harm** — does a running sound card break the
UART? — and every round trip answered, 18/18. It never asked the other direction, and the other
direction is where the damage is.

Both sites now say so. What the experiment licenses is **sharing the handle**, which is all it ever
claimed: a tune is a handful of frames at a moment nothing is keyed. It does not license putting
frames on this wire *while the transmitter is up*.

**The experiment's own record is left intact** — its docstring is accurately scoped, and ADR 0142's
index row describes what 0142 claimed at the time. That row gets a status annotation in the existing
idiom rather than a rewrite. It measured what it measured.

*(Also corrected: ADR 0175 called the register exchange "12-byte" throughout. 12 is `build_frame`'s
overhead with the parameters excluded; the exchange is 16 bytes out and 16 back. It changes no
conclusion, and it was wrong as written.)*

## Audit — every scheduled source of serial traffic on this handle

Reported, not fixed. Config audited: `backend = baofeng`, `uvk5_tuner = hybrid`.

| source | cadence / trigger | frames | can overlap TX? | inhibit |
|---|---|---|---|---|
| `RssiPoller` | 0.5 s, from construction to `close()` — no listener needed | `0x0851`/`0x0951` | **narrow race only** (below) | `paused` (ADR 0175) |
| `BroadcastFmPoller` | 2.0 s while any bridge is connected | `0x0879`/`0x087A` | **yes — the subject of this ADR** | `paused` (this ADR) |
| key-up frames | every key-up: `_clear_if_deafened` (2 round trips) + `_reassert_channel` (2) | `0x0879`, `0x0877`, `0x0873` | **no** — all land before the DTR assert, by construction | n/a |
| boot asserts | once per backend construction | `0x0879` + `0x0877` | no (nothing keyed yet) | n/a |
| `Uvk5Transport` reader thread | continuous | **none — it never writes** | n/a | n/a |
| `Uvk5Transport.reconnect` | route-only, nothing schedules it | **none** | only if a human presses it | n/a |
| `TransportWatcher` | 2.0 s | **none** — `Thread.is_alive()`, deliberately never polls the radio | n/a | n/a |
| `RxPump`, TOT timer, TX pacer, D-STAR watchdog/keepalive | various | **none on this handle** — the worst any does is the un-key DTR `setattr` | n/a | n/a |
| scan loop | `POST /scan` | would send `0x0873` per channel | **unreachable** — `AiocBaofeng` never advertises `Capability.SCAN`, so `ScanEngine` raises | n/a |
| `PolledGate`/`CatBusyGate` | 0.2 s | would reach `status()` | **unreachable** — `baofeng.squelch_mode = cat` is rejected at load | n/a |

Two things fall out of this that are worth stating rather than burying:

- **The two pollers are the only scheduled writers, and both are now inhibited.** Scan and the CAT
  gate are structurally unreachable on this backend, not merely unused, so the audit closes rather
  than deferring them.
- **Steady-state traffic on this cable is higher than anyone decided.** `RssiPoller` starts in
  `__init__` and needs no listener, bridge or session, so the baseline is 2 exchanges/s from process
  start, plus 0.5/s whenever a link is up.

## Findings, recorded rather than fixed

1. **`RssiPoller`'s guard is check-then-act, and the window is real.** `poll_once` reads `paused()`
   and then issues a request with a 1.0 s timeout, while `_transmitting` is only set *after* the
   dock frames and immediately before the DTR assert. A poll that passes the check microseconds
   before a key-up can still be holding the wire as the line goes high. Narrow — every key-up frame
   completes and releases `_wire` first, so the window is the gap between the last of those and the
   assert — but not closed. Closing it means asserting DTR while holding `_wire`, which is a change
   to the keying path and deserves its own measurement. **This is a defect in ADR 0175's own fix,
   found by auditing it.**
2. **`BroadcastFmPoller` starts on any bridge connect, not on any bridge that relays RF.**
   `link/bridge.py:267` is unconditional; a receive-only entry (`tx_to_rf: false`) raises the same
   refcount. Not wrong — the mute it feeds is RF→network, which a receive-only link still does — but
   worth knowing when reasoning about what puts frames on the wire.
3. **`controller.poll` is a dead config key on this deployment.** Its only consumer,
   `ControllerRunner`, is never instantiated; `RxPump` drives `Controller.step` per audio frame
   instead.
4. **The `uvk5` backend has the same physical hazard and no pause anywhere.** Its `status()` does a
   register read per call, so any per-frame consumer of `status()` on that backend is ADR 0125's
   fault with ADR 0175's consequences. Not the deployed mode; not touched.

## Acceptance

- Red run **8 failed / 21 passed** on master's poller. pytest **2330 passed / 5 skipped**, from
  2322/5. vitest **14 files / 155 tests**, untouched.
- Bench: the three arms above, on the deployed station, driving the real Mumble bridge against a
  real local Murmur. `acceptance.py` full run: exit 1 on the known witness `kv4p /healthz` 404,
  every other stage PASS.
- Station restored to **145.145 / TX 144.545 / 107.2 / FM / low** and verified; the link left
  disconnected as found; `uvk5_tune_persist` reported **as found (`true`)**, not flipped.

## Out of scope

The firmware fork, the witness checkout, EventHub's cap, the ADR 0172 mock audit items. No UI.
