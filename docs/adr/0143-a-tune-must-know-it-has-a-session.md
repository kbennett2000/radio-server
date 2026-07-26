# 0143 — A tune must know it has a session

Status: Accepted

Fixes the fault shipped by ADR [0142](0142-the-server-picks-the-repeater.md). Corrects that ADR's
acceptance, which passed 80/80 while the feature was broken in the operator's hands.

## Context

ADR 0142 shipped tuning and reported a differential gate at **80/80, zero failures**. Within hours
the operator could not set a repeater frequency, or a simplex frequency, from the web UI. Every
`POST /presets/apply` returned **500 Internal Server Error**:

```
radio_server.backends.uvk5.tuner.TuneError: no answer to an EEPROM read at 0x8808   (446.000, band 5)
radio_server.backends.uvk5.tuner.TuneError: no answer to an EEPROM read at 0x8800   (146.520, band 2)
```

Measured, not assumed: the AIOC symlink was unmoved and still `ttyACM0`, the service still held the
fd, `dmesg` showed no re-enumeration, and both addresses were the correct attribute words for their
bands. The link was fine. The radio was fine — a direct probe answered `HELLO` with
`F4HWN v5.7.0`, no lock screen, and read `0xA000` on the first ask.

### The firmware's contract, which we were not honouring

Every EEPROM handler in stock `app/uart.c` opens with the same four lines:

```c
if (pCmd->Timestamp != Timestamp)
    return;                    // no reply, no error, nothing on the wire
```

The radio answers EEPROM frames only inside a session that `0x0514 HELLO` establishes, and it
refuses **silently**. So "no answer" is not evidence of a broken cable. It is the only way this
firmware can say *you do not have a session* — and it is indistinguishable from a dead link unless
the host goes and asks.

### Our half of it

```python
def _hello(self) -> None:
    self._tp.send(f.Hello(timestamp=SESSION_TIMESTAMP))
    self._sleep(0.4)
    self._hello_sent = True        # set even though nothing was verified
```

Three faults in five lines:

1. **The handshake was never verified.** `0x0515 IM_HERE` was already decoded in `frames.py`; the
   code used `send()` where it should have used `request()`. A HELLO that never landed — radio off,
   mid-reboot, sitting in the DFU bootloader after a flash — was invisible.
2. **`_hello_sent` latched for the life of the process.** So a *transient* condition became
   **permanent**. The service restarted while the radio was being flashed, sent one HELLO into a
   radio that could not answer, latched `True`, and failed every tune for the next three hours —
   including long after the radio came back. The only cure was restarting the service.
3. **A radio rebooting underneath the server was treated as impossible.** It is ordinary: a firmware
   flash, a battery swap, the operator switching it off and on.

### Why the 80/80 gate could not see it

The gate ran as one continuous session against a warm radio that had already completed a handshake,
and it re-established that handshake itself after each of its own resets. Every number it produced
was true. It simply never asked what happens when the session goes away for a reason the server did
not cause — so it measured the happy path eighty times and none of the ways the product actually
fails. **A gate that cannot fail the way the product fails is not a gate**, and a pass count is not
evidence about a case the run never entered.

### What the operator saw

`TuneError` was caught nowhere — every route handled `UnsupportedCapability` and nothing else. A
hardware fault therefore became a 500 and a stack trace in the journal, and the web UI rendered
`Request failed (500): Internal Server Error`. The one person standing next to the radio, who could
have fixed it in seconds, was told nothing.

## Decision

**1. A session is a thing that can be lost, not a flag that gets set.**

`_hello()` sends `0x0514` and waits for `0x0515`, and believes nothing until it arrives. It also
refuses a locked radio with a custom AES key, because such a radio still answers reads — with
zeroed data (`if (!bLocked) EEPROM_ReadBuffer(...)`) — which would otherwise surface as a baffling
read-back mismatch.

**2. Silence costs one handshake, not a service restart.**

Every EEPROM read and write goes through `_exchange()`: on no answer it re-establishes the session
**once** and retries. Only a second silence is an error. A radio that reboots underneath the server
is handled as the ordinary event it is.

**3. Pre-flight before a sequence that reboots the radio.**

`apply()` opens with a verified handshake, so a dead radio is reported as a dead radio rather than
as a mystery timeout at an EEPROM address the operator cannot act on.

**4. Coming back from a reset is retried, not assumed.**

How long a UV-K5 takes to boot is not a constant, so the post-reset handshake is offered
`REBOOT_HELLO_ATTEMPTS` times.

**5. A hardware fault reaches the operator as a sentence.**

New `backends.base.RadioUnavailable` — *the radio itself could not do something it does support*,
as distinct from `UnsupportedCapability`, which is about what a mode can do and is answerable from
config alone. `TuneError` subclasses it, and one app-wide FastAPI handler renders it as **503 with
the message**. Registered app-wide rather than per-route on purpose: the tuning routes are not the
only ones that touch hardware, and the next route added should not have to remember.

## Consequences

An operator who has switched the radio off now gets *"the radio did not answer the handshake — check
it is powered on, is not in the bootloader, and that the AIOC cable is seated"* instead of
`Internal Server Error`. A radio power-cycled between tunes costs one extra handshake instead of a
service restart. Every tune pays one verified HELLO up front — a single round trip against a
sequence that already takes the better part of fourteen seconds.

The retry is deliberately bounded at one. Retrying until something works hides a real fault, which
is the failure mode this whole ADR exists to correct.

## Acceptance

`scripts/bench/tune_survives_a_reboot.py` — on hardware, service stopped, restarted in a `finally`:

| Row | Setup | Requires |
|---|---|---|
| cold | a tuner that has never spoken to this radio | tunes, and the record reads back |
| recovery | tune → bare `Reset()` out-of-band → tune again | the second tune succeeds |

Row 2 is the incident. The old code fails it every time; that is the point of writing it.

Unit coverage of the same shape, hardware-free: the fake transport in `tests/test_uvk5_tuner.py` now
models the session gate — no HELLO means requests time out silently, and a `Reset` clears the
session exactly as the radio does. Without that, a tuner that never checked its handshake passed
every test, which is precisely what happened.

`tests/test_presets.py` proves the operator-facing half: a radio that is not answering returns 503
with the reason, on `/presets/apply` and on `/frequency`.

## Notes

The `setvfo` (`0x0873`) path is not session-gated and is unchanged, but its "no reply" message was
wrong in a way worth fixing: it asserted "this firmware is pre-F6" when silence equally means the
radio is not there. It now says both, because they are not distinguishable from the host and
guessing the more interesting one is how an operator ends up reflashing a radio that was merely
switched off.
