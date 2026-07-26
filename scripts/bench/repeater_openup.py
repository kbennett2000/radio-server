"""Does a real repeater come up when radio-server keys the UV-K5 in Baofeng mode? (ADR 0139)

ADR 0138 proved the mechanism on the bench: the AIOC's DTR line keys the UV-K5 and the real
``AiocBaofeng`` class puts recoverable audio on the air. What it could not prove is the only thing
the operator actually asked for -- that a **repeater opens**. Every number in ADR 0137 and 0138 is a
bench frequency into a witness sitting inches away, and a witness that close would hear a microwatt.

So this script transmits through a real machine and measures whether the machine answers.

THE MEASUREMENT, AND WHY IT IS THIS ONE
---------------------------------------
The witness (kv4p, a second radio-server on :8091) is tuned to the repeater's **OUTPUT**. The radio
under test transmits on the repeater's **INPUT**, 5 MHz down.

**The verdict comes from the window AFTER our carrier drops.** Our transmitter is off; anything on
the output frequency in that window is the machine's own hang time, tail or courtesy tone, and
nothing else can produce it.

That is not fussiness, it is the difference between a measurement and an artefact. A witness sitting
inches from a keyed HT can be desensed or front-end-overloaded by a carrier 5 MHz away, so ``busy``
going True *while we transmit* proves nothing at all -- it is exactly what a completely dead repeater
would also show. ``busy`` still True *after we stop* cannot be us. The whole design is arranged
around that one window; everything else in the output is diagnostics.

``t0`` is when the transmit request **returns** -- deliberately the late, conservative choice.
``AiocBaofeng.transmit`` blocks until the clip has drained and then drops PTT, so the response is
sent after un-key. Taking t0 any earlier would start counting tail while we were still transmitting;
taking it late can only ever make this test harder to pass, never easier, and a measurement whose
bias points at "fail" is the one you are allowed to believe when it says "pass".

Each over is also preceded by its own quiet check. A busy channel is not just discourteous to key
over, it voids the measurement -- somebody else's carrier in the tail window reads as a repeater.

WHY THIS SCRIPT IS NOT BEHIND ``bench_frequency_only``
------------------------------------------------------
Every other keying path in this bench refuses anything outside ``BENCH_TX_HZ`` and additionally
refuses any frequency a configured preset uses. **That guard is untouched and unweakened** -- this
script simply is not one of its callers, because its entire purpose is the thing the guard forbids.

What replaces it: two explicit flags (``--i-will-transmit`` and ``--i-am-the-licensed-operator``),
a named repeater resolved from the configured presets rather than a raw frequency, a hard cap on how
long and how often it keys, and the fact that the transmission **is the station ID** -- see below.
This is ordinary amateur operation by the licensed operator, under local control, with the operator
at the radio. It is not the unattended carrier on a repeater input that the guard exists to prevent.

PART 97
-------
The over this script sends is ``StationId.identify()`` -- a CW ident of the configured callsign,
through the production identification path. So the transmission is legally identified by
construction, it is short, and it exercises code that matters instead of a synthetic tone.

WHAT IT WILL NOT DO
-------------------
It never *enables* Baofeng mode -- it refuses unless the station is already there, because switching
a live station into a mode where it transmits on an unreadable front-panel frequency is a decision a
human makes. It will happily *restore* a backend on the way out (``--restore-backend``), because
returning to a known mode is fail-safe in a way that leaving one is not.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import deviation_probe as dp  # noqa: E402
from acceptance import KV4P_BASE, RADIO_BASE, TOKEN, api  # noqa: E402

#: The kv4p's SA818-UHF tunes 400-480 MHz (`backends/kv4p/radio.py`). A 2 m repeater cannot be
#: witnessed here at all, and pretending otherwise would produce a confident NO RESPONSE for a
#: machine the instrument simply cannot hear.
KV4P_MIN_HZ = 400_000_000
KV4P_MAX_HZ = 480_000_000

#: Seconds of quiet required on the repeater's output before each over. Both courtesy (do not key
#: over somebody) and control (their carrier in our tail window reads as the repeater answering).
PREFLIGHT_SECONDS = 5.0
#: How long after our carrier drops we keep watching the output for the machine's tail.
TAIL_WINDOW = 4.0
#: Dead band after t0 before tail counting starts, for the PTT line to release and the witness's
#: squelch to react to our carrier disappearing. Counted out of the window, not added to it.
TAIL_SETTLE = 0.25
#: Carrier this long inside the tail window means the repeater was transmitting. Short enough to
#: catch a bare squelch tail, long enough that a single stray poll cannot manufacture it.
MIN_TAIL_SECONDS = 0.3
#: Fraction of the tail window the poller must actually have observed for the over to count. Without
#: this a poller that died mid-run reports 0.0 s of tail, which is "no measurement" wearing "no
#: signal"'s clothes -- the error this bench has now made three times (ADR 0136/0137/0138).
MIN_COVERAGE = 0.8

DEFAULT_OVERS = 3
#: How many clean overs must show a tail. One is not enough: a single tail could be another station
#: keying the machine while we happened to be listening.
NEEDED_TAILS = 2
SPACING_SECONDS = 6.0
#: The operator leg's listening window -- long, because the operator is not in the same room as the
#: terminal (ADR 0136).
OPERATOR_WINDOW = 180.0

OPENED = "OPENED"
NO_RESPONSE = "NO RESPONSE"
INCONCLUSIVE = "INCONCLUSIVE"


# --- the verdict, as pure functions ------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One reading of the witness's hardware carrier-detect. ``t`` is monotonic seconds."""

    t: float
    busy: bool


@dataclass(frozen=True)
class Over:
    """What one transmission produced. Everything the verdict needs, and nothing live."""

    index: int
    keyed: bool
    clear_before: bool
    covered: float
    tail: float
    cw_rms: float = 0.0
    control_rms: float = 0.0


def _spans(samples: Sequence[Sample], start: float, end: float, *, busy_only: bool):
    """Yield the clipped ``(lo, hi)`` spans each sample covers inside ``[start, end)``.

    A sample is taken to hold until the *next* sample. The final sample therefore contributes
    nothing, since nothing observed what happened after it -- which biases both `busy_seconds` and
    `covered_seconds` down. Down is the safe direction for both: it can only under-report a tail
    and under-report coverage, never invent either.
    """
    for a, b in zip(samples, samples[1:]):
        if busy_only and not a.busy:
            continue
        lo, hi = max(a.t, start), min(b.t, end)
        if hi > lo:
            yield lo, hi


def busy_seconds(samples: Sequence[Sample], start: float, end: float) -> float:
    """Seconds the witness reported a carrier inside ``[start, end)``."""
    return sum(hi - lo for lo, hi in _spans(samples, start, end, busy_only=True))


def covered_seconds(samples: Sequence[Sample], start: float, end: float) -> float:
    """Seconds of ``[start, end)`` the poller actually observed, busy or not.

    Kept separate from :func:`busy_seconds` on purpose. "The repeater did not answer" and "we were
    not watching" are different findings that would otherwise share the number 0.0.
    """
    return sum(hi - lo for lo, hi in _spans(samples, start, end, busy_only=False))


def over_is_clean(over: Over, *, window: float = TAIL_WINDOW, min_coverage: float = MIN_COVERAGE) -> bool:
    """Did this over produce a measurement at all? Independent of what that measurement said."""
    return over.keyed and over.clear_before and over.covered >= window * min_coverage


def run_verdict(
    overs: Sequence[Over],
    *,
    needed: int = NEEDED_TAILS,
    min_tail: float = MIN_TAIL_SECONDS,
    window: float = TAIL_WINDOW,
    min_coverage: float = MIN_COVERAGE,
) -> tuple[str, str]:
    """``(verdict, reason)`` over a run's overs.

    Three outcomes, and the third is the one that keeps this honest: NO RESPONSE claims the repeater
    did not come up, and that claim is only available when enough overs actually put a carrier on
    its input while we were watching its output. Everything else is INCONCLUSIVE, including "one
    tail out of three" -- which is real evidence of *something* and not evidence of us.
    """
    if not overs:
        return INCONCLUSIVE, "no overs were attempted"
    if not any(o.keyed for o in overs):
        return INCONCLUSIVE, "the station never keyed, so nothing was tested"
    clean = [o for o in overs if over_is_clean(o, window=window, min_coverage=min_coverage)]
    if len(clean) < needed:
        busy = sum(1 for o in overs if o.keyed and not o.clear_before)
        blind = sum(1 for o in overs if o.keyed and o.covered < window * min_coverage)
        detail = []
        if busy:
            detail.append(f"{busy} had traffic on the output beforehand")
        if blind:
            detail.append(f"{blind} lost witness coverage")
        return INCONCLUSIVE, (
            f"only {len(clean)} of {len(overs)} overs were clean enough to judge "
            f"({'; '.join(detail) or 'the station did not key'})"
        )
    heard = [o for o in clean if o.tail >= min_tail]
    if len(heard) >= needed:
        return OPENED, (
            f"{len(heard)} of {len(clean)} clean overs were followed by a carrier on the "
            f"repeater's output after ours had stopped"
        )
    if heard:
        return INCONCLUSIVE, (
            f"only {len(heard)} of {len(clean)} clean overs showed a tail; one tail could be "
            f"another station keying the machine, so this is not a result either way"
        )
    return NO_RESPONSE, (
        f"{len(clean)} clean overs, no carrier on the repeater's output after any of them"
    )


# --- the repeater ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repeater:
    name: str
    output_hz: int
    input_hz: int
    tone: float | None

    @property
    def offset_mhz(self) -> float:
        return (self.input_hz - self.output_hz) / 1e6


def resolve_repeater(name: str) -> Repeater:
    """Look one up by preset name from the running station. Raises with a usable message."""
    code, body = api(RADIO_BASE, "GET", "/presets")
    if code != 200:
        raise RuntimeError(f"could not read /presets from {RADIO_BASE} (HTTP {code})")
    rows = body.get("presets") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise RuntimeError(f"/presets returned {type(rows).__name__}, not a list")
    wanted = name.strip().casefold()
    match = next((r for r in rows if str(r.get("name", "")).strip().casefold() == wanted), None)
    if match is None:
        match = next((r for r in rows if wanted in str(r.get("name", "")).casefold()), None)
    if match is None:
        names = ", ".join(sorted(str(r.get("name")) for r in rows))
        raise RuntimeError(f"no preset matches {name!r}. Configured presets: {names}")
    out, inp = match.get("frequency"), match.get("tx_frequency")
    if not isinstance(out, int) or not isinstance(inp, int):
        raise RuntimeError(
            f"preset {match.get('name')!r} has no TX leg (frequency={out}, tx_frequency={inp}) — "
            f"it is a simplex preset, not a repeater, and there is nothing to open"
        )
    if not KV4P_MIN_HZ <= out <= KV4P_MAX_HZ:
        raise RuntimeError(
            f"{match.get('name')!r} transmits its output on {out / 1e6:.4f} MHz, outside the kv4p "
            f"witness's {KV4P_MIN_HZ / 1e6:.0f}-{KV4P_MAX_HZ / 1e6:.0f} MHz range. The instrument "
            f"cannot hear this machine, so a NO RESPONSE here would be a lie. Pick a 70 cm repeater."
        )
    return Repeater(str(match.get("name")), out, inp, match.get("tx_tone"))


# --- the witness -------------------------------------------------------------------------------


class BusyWatch(threading.Thread):
    """Poll the witness's ``/status.busy`` as fast as it answers, on a persistent connection.

    ``busy`` is the SA818's SQ pin (`backends/kv4p/radio.py`) -- a hardware carrier detect, not a
    software threshold we chose. That matters here: the tail is being measured by the receiver's own
    squelch, so no level constant of ours is standing between the repeater and the verdict.
    """

    def __init__(self, base: str) -> None:
        super().__init__(daemon=True)
        self._base = base
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._samples: list[Sample] = []
        self.errors = 0
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed bench certs (ADR 0039)
        self._ctx = ctx
        host_port = base.split("://", 1)[1]
        self._host, _, port = host_port.partition(":")
        self._port = int(port or 443)

    @property
    def samples(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        conn = None
        while not self._stop.is_set():
            try:
                if conn is None:
                    conn = http.client.HTTPSConnection(
                        self._host, self._port, context=self._ctx, timeout=3
                    )
                conn.request("GET", "/status", headers={"Authorization": f"Bearer {TOKEN}"})
                state = json.loads(conn.getresponse().read())
                sample = Sample(time.monotonic(), bool(state.get("busy")))
            except Exception:
                self.errors += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(0.1)
                continue
            with self._lock:
                self._samples.append(sample)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def tune_witness(hz: int) -> int:
    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": hz})
    if code != 200:
        raise RuntimeError(f"could not tune the witness to {hz} Hz (HTTP {code} {body!r:.80})")
    return hz


def witness_frequency() -> int | None:
    code, state = api(KV4P_BASE, "GET", "/status")
    return state.get("frequency") if code == 200 and isinstance(state, dict) else None


def active_backend() -> str | None:
    code, body = api(RADIO_BASE, "GET", "/radio/backends")
    return body.get("active") if code == 200 and isinstance(body, dict) else None


# --- the run -----------------------------------------------------------------------------------


def _listen_in_background(seconds: float) -> tuple[threading.Thread, list[bytes]]:
    captured: list[bytes] = []

    def _go() -> None:
        try:
            captured.append(dp.listen(seconds))
        except Exception as exc:  # noqa: BLE001 — a dead listener must not look like a dead repeater
            print(f"    (witness audio capture failed: {type(exc).__name__}: {exc})")

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    return thread, captured


def one_over(index: int, watch: BusyWatch, cw_tone: float, control_tone: float) -> Over:
    """Quiet check, transmit the station ID, then watch the output after our carrier stops."""
    print(f"\n  over {index}:")
    quiet_from = time.monotonic()
    time.sleep(PREFLIGHT_SECONDS)
    quiet_to = time.monotonic()
    before = busy_seconds(watch.samples, quiet_from, quiet_to)
    clear = before < 0.2
    print(f"    channel check: {before:.2f}s of carrier in {PREFLIGHT_SECONDS:.0f}s "
          f"({'clear' if clear else 'BUSY — this over cannot be judged'})")

    # Capture across the over and the tail. Audio is corroboration only: /audio/rx delivers nothing
    # while the witness's squelch gate is shut, so a silent capture is not evidence of anything.
    audio_thread, captured = _listen_in_background(TAIL_WINDOW + 12.0)
    time.sleep(0.4)

    print("    transmitting the station ID (CW) through the production path...")
    started = time.monotonic()
    # No body, and note it runs ON the event loop (`app.py` run_service) -- the whole :8090 API is
    # blocked for the duration of the over. That is exactly why t0 is the response, and why the
    # witness has to be a separate process on :8091 to be watching at all.
    code, body = api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    t0 = time.monotonic()
    keyed = code == 200 and isinstance(body, dict) and bool(body.get("transmitted", True))
    print(f"    /services/01 -> HTTP {code} after {t0 - started:.2f}s"
          f"{'' if keyed else f'  {body!r:.120}'}")

    window_from, window_to = t0 + TAIL_SETTLE, t0 + TAIL_WINDOW
    while time.monotonic() < window_to + 0.3:
        time.sleep(0.05)
    samples = watch.samples
    tail = busy_seconds(samples, window_from, window_to)
    covered = covered_seconds(samples, window_from, window_to)
    during = busy_seconds(samples, started, t0)
    print(f"    during our over:  {during:.2f}s carrier "
          f"(diagnostic only — could be desense from our own transmitter)")
    print(f"    AFTER we stopped: {tail:.2f}s carrier in the "
          f"{window_to - window_from:.2f}s window  [witness saw {covered:.2f}s of it]")

    audio_thread.join(timeout=20.0)
    pcm = captured[0] if captured else b""
    cw_rms = control_rms = 0.0
    if pcm:
        best = dp.loudest_slice(pcm, min(3.0, len(pcm) / (dp.CANONICAL_RATE * 2)))
        cw_rms = dp.band_rms(best, cw_tone, width=dp.AUDIO_WIDTH_HZ)
        control_rms = dp.band_rms(best, control_tone, width=dp.AUDIO_WIDTH_HZ)
        print(f"    relayed audio: {cw_tone:.0f} Hz {cw_rms:8.2f} vs control "
              f"{control_tone:.0f} Hz {control_rms:8.2f}   ({len(pcm)} bytes)")
    else:
        print("    relayed audio: none (witness squelch stayed shut — not evidence either way)")

    return Over(
        index=index,
        keyed=keyed,
        clear_before=clear,
        covered=covered,
        tail=tail,
        cw_rms=cw_rms,
        control_rms=control_rms,
    )


def confirm_on_the_input(watch: BusyWatch, repeater: Repeater) -> bool:
    """Before measuring the machine, prove we are keying its INPUT at all.

    ADR 0138 called the front panel unreadable and left it there, which made two very different
    failures produce one output. Point the witness at the repeater's **input** instead of its output
    and key once: our own carrier, from a transmitter inches away, is unmissable. If it appears we
    know three things at once that nothing else in this repo can tell us --

      * the radio is on this repeater's channel (the operator set it right),
      * it is not wedged in the dock's full-control loop ignoring its own PTT pin (ADR 0138), and
      * the AIOC's DTR line and the audio path are both alive right now, not just last week.

    -- and if it does not appear, a silent output frequency afterwards would have meant nothing at
    all. Desense is not a worry here: the witness is *supposed* to be hearing our transmitter.

    The over is the station ID, so this costs one legal, identified transmission.
    """
    print(f"\n  pre-flight: witness -> the INPUT, {repeater.input_hz / 1e6:.4f} MHz — is that")
    print("  actually where this station transmits? Nothing else can answer that.")
    tune_witness(repeater.input_hz)
    time.sleep(2.0)
    started = time.monotonic()
    code, _ = api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    t0 = time.monotonic()
    time.sleep(0.3)
    span = t0 - started
    heard = busy_seconds(watch.samples, started, t0)
    share = heard / span if span > 0 else 0.0
    print(f"    keyed for {span:.2f}s (HTTP {code}); witness saw {heard:.2f}s of carrier "
          f"on the input ({share * 100:.0f}%)")
    if share >= 0.5 and heard >= 0.5:
        print("    => confirmed: the radio is keying, and it is keying THIS repeater's input.")
        return True
    print("\n    => the witness did not hear us on the input. THREE causes, not one answer:")
    print("       1. the radio is not on this repeater's channel (nothing can read its panel);")
    print("       2. it is wedged in the dock's 0x0870 loop, deaf to its own PTT pin (ADR 0138);")
    print("       3. the AIOC's PTT or audio path is genuinely dead.")
    print("       Measuring the output now would report NO RESPONSE for any of them.")
    return False


def operator_leg(watch: BusyWatch, repeater: Repeater, window: float) -> int:
    """Instrument check: can the witness hear this machine's output AT ALL?

    Run this when the server leg finds nothing. Without it, "the UV-K5 does not reach the repeater"
    and "the witness cannot hear the repeater" are the same output, and they are not the same
    finding.
    """
    print("\n" + "=" * 78)
    print(f"  ==> PICK UP A RADIO YOU KNOW OPENS {repeater.name} AND KEY IT FOR A FEW SECONDS.")
    print(f"      Any radio, any power. It must be on {repeater.name} — transmitting on")
    print(f"      {repeater.input_hz / 1e6:.4f} MHz. You have {window:.0f} seconds; say your callsign.")
    print("=" * 78)
    start = time.monotonic()
    while time.monotonic() - start < window:
        time.sleep(0.5)
        heard = busy_seconds(watch.samples, start, time.monotonic())
        if heard >= 1.0:
            print(f"\n  the witness heard {heard:.1f}s of carrier on "
                  f"{repeater.output_hz / 1e6:.4f} — it CAN hear this repeater.")
            print("  So a NO RESPONSE from the server leg is about the UV-K5, not the instrument.")
            return 0
    heard = busy_seconds(watch.samples, start, time.monotonic())
    print(f"\n  the witness heard {heard:.1f}s of carrier in {window:.0f}s.")
    print(f"  => THE INSTRUMENT CANNOT HEAR {repeater.name}. Every server-leg result is void:")
    print("     nothing here can tell a silent repeater from a repeater we cannot receive.")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeater", required=True, help="preset name, e.g. 'K0PRA448.525'")
    ap.add_argument("--i-will-transmit", action="store_true")
    ap.add_argument("--i-am-the-licensed-operator", action="store_true",
                    help="required: this keys a REAL repeater's input, not a bench frequency")
    ap.add_argument("--source", choices=("server", "operator"), default="server")
    ap.add_argument("--overs", type=int, default=DEFAULT_OVERS)
    ap.add_argument("--cw-tone", type=float, default=600.0, help="station.cw_tone_hz")
    ap.add_argument("--control-tone", type=float, default=1500.0, help="a band we never send")
    ap.add_argument("--operator-window", type=float, default=OPERATOR_WINDOW)
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip confirming we key the repeater's input (weakens every result)")
    ap.add_argument("--restore-backend", default=None,
                    help="select this backend on the way out (e.g. uvk5) — fail-safe, never enables")
    args = ap.parse_args(argv)

    if args.source == "server" and not (args.i_will_transmit and args.i_am_the_licensed_operator):
        print("refusing: pass --i-will-transmit AND --i-am-the-licensed-operator.\n"
              "This transmits on a real repeater's input, which every other script here forbids.",
              file=sys.stderr)
        return 2

    try:
        repeater = resolve_repeater(args.repeater)
    except RuntimeError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2

    print(f"\n  {repeater.name}")
    print(f"    output (we listen)   {repeater.output_hz / 1e6:9.4f} MHz")
    print(f"    input  (we transmit) {repeater.input_hz / 1e6:9.4f} MHz  "
          f"({repeater.offset_mhz:+.3f} MHz, tone {repeater.tone})")

    if args.source == "server":
        backend = active_backend()
        if backend != "baofeng":
            print(f"\nrefusing: the station is on backend {backend!r}, not 'baofeng'.\n"
                  "In dock mode the host picks the frequency and this test means nothing; in\n"
                  "Baofeng mode the radio's front panel does. Switch it deliberately:\n"
                  "  curl -sk -X POST -H \"Authorization: Bearer $RADIO_API_TOKEN\" \\\n"
                  "    -H 'Content-Type: application/json' -d '{\"backend\":\"baofeng\"}' \\\n"
                  f"    {RADIO_BASE}/radio/select", file=sys.stderr)
            return 2
        print("\n  the station is in Baofeng mode: it will transmit on whatever the UV-K5's front")
        print(f"  panel is set to. Nothing here can read that back — it must be on {repeater.name}.")

    home = witness_frequency()
    watch = BusyWatch(KV4P_BASE)
    overs: list[Over] = []
    rc = 2
    try:
        tune_witness(repeater.output_hz)
        time.sleep(2.0)
        watch.start()
        time.sleep(1.0)

        if args.source == "operator":
            rc = operator_leg(watch, repeater, args.operator_window)
        elif not args.skip_preflight and not confirm_on_the_input(watch, repeater):
            print("\n" + "=" * 78)
            print("  => INCONCLUSIVE: this station is not putting a carrier on "
                  f"{repeater.input_hz / 1e6:.4f} MHz,")
            print("     so nothing measured on the output would be about the repeater.")
            print("=" * 78)
            rc = 2
        else:
            tune_witness(repeater.output_hz)
            time.sleep(2.0)
            for index in range(1, args.overs + 1):
                overs.append(one_over(index, watch, args.cw_tone, args.control_tone))
                if index < args.overs:
                    time.sleep(SPACING_SECONDS)
            verdict, reason = run_verdict(overs)
            print("\n" + "=" * 78)
            print(f"  => {verdict}: {reason}.")
            if verdict == OPENED:
                print(f"\n     radio-server opened {repeater.name}. The UV-K5 held the channel, its")
                print("     own firmware set up TX, and the machine came back. Baofeng mode works")
                print("     on a real repeater, not just into a witness on the bench.")
            elif verdict == NO_RESPONSE:
                print("\n     Before believing this, run the instrument check — the witness may")
                print("     simply not hear this machine's output from where it sits:")
                print(f"       {Path(__file__).name} --repeater {args.repeater!r} --source operator")
            print("=" * 78)
            rc = {OPENED: 0, NO_RESPONSE: 1}.get(verdict, 2)
            if watch.errors:
                print(f"\n  ({watch.errors} witness poll errors during the run)")
    finally:
        watch.stop()
        if home is not None:
            try:
                tune_witness(home)
                print(f"\n  witness restored to {home / 1e6:.4f} MHz")
            except Exception as exc:  # noqa: BLE001
                print(f"\n  COULD NOT restore the witness to {home} Hz: {exc}", file=sys.stderr)
        if args.restore_backend:
            code, body = api(RADIO_BASE, "POST", "/radio/select",
                             body={"backend": args.restore_backend}, timeout=60.0)
            print(f"  backend restore -> {args.restore_backend!r}: HTTP {code}")
            print(f"  active backend is now: {active_backend()!r}")
        elif args.source == "server":
            print("\n  the station is STILL IN BAOFENG MODE. Put it back deliberately:")
            print("    curl -sk -X POST -H \"Authorization: Bearer $RADIO_API_TOKEN\" \\")
            print("      -H 'Content-Type: application/json' -d '{\"backend\":\"uvk5\"}' \\")
            print(f"      {RADIO_BASE}/radio/select")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
