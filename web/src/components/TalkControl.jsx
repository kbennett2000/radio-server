// Talk control (ADR 0024; ADR 0037 adds selectable trigger): capture the mic and transmit through
// the gateway.
//
// Two trigger styles, switchable and remembered per browser (localStorage `talkMode`):
//   - "hold"   (default): press-and-hold the button — or hold the Spacebar — to key, release to stop.
//                          The radio-mic feel. The button captures the pointer on press, so the real
//                          release always returns to it (a pointer that slides off can never leave the
//                          transmitter stuck keyed) and no spurious pointerleave unkeys mid-hold.
//   - "toggle": click to start, click again to stop (the original behavior).
//
// A user gesture is required to start either way (getUserMedia needs one and an AudioContext starts
// suspended). Once talking, the card shows a mic level meter and an "on air" state; the button is red
// (`.ptt.keyed`). It reports its talking state up to ControlPanel so the local RX monitor mutes while
// you key (you don't hear yourself gate in/out through the ~500 ms RX jitter buffer).

import { useCallback, useEffect, useRef, useState } from "react";
import { useTxAudio } from "../useTxAudio.js";

const MODE_KEY = "radio.talkMode";

function fmtMHz(hz) {
  return (hz / 1e6).toFixed(4);
}

function readMode() {
  try {
    return window.localStorage.getItem(MODE_KEY) === "toggle" ? "toggle" : "hold";
  } catch {
    return "hold";
  }
}

// Count a serial TX lockout down locally, so the button re-enables itself.
//
// `tx_ready_in` arrives as a number of seconds on the status event a tune pushes (ADR 0144): the
// UV-K5 mutes its transmitter for six seconds after any EEPROM write, refusing a key-up and cutting
// an over already in progress. The server enforces that wait regardless — this only stops the
// operator being invited into it, because a browser must never be the thing keeping RF correct.
//
// Ticking locally rather than polling matters: nothing else would push a status event while the
// radio sits there muted, so a button waiting to be told it may go would simply stay dead.
function useTxLockout(seconds) {
  const [remaining, setRemaining] = useState(() => (seconds > 0 ? seconds : 0));

  useEffect(() => {
    if (!(seconds > 0)) {
      setRemaining(0);
      return undefined;
    }
    // Absolute deadline, not repeated subtraction: a backgrounded tab throttles timers, and a
    // counter that drifts would leave the button dead after the radio was ready again.
    const until = Date.now() + seconds * 1000;
    setRemaining(seconds);
    const id = setInterval(() => {
      const left = (until - Date.now()) / 1000;
      setRemaining(left > 0 ? left : 0);
      if (left <= 0) clearInterval(id);
    }, 250);
    return () => clearInterval(id);
  }, [seconds]);

  return remaining;
}

export default function TalkControl({
  token,
  onAuthError,
  onTalkingChange,
  mumbleMode = false,
  dstarMode = false,
  state = null,
  hasCap = () => true,
}) {
  // ADR 0050/0088: while a Mumble link or a D-STAR reflector is the browser's target, Talk streams
  // the mic to that channel instead of keying the radio — same hook, different endpoint (no RF keyed).
  // D-STAR wins if both apply.
  const source = dstarMode ? "dstar" : mumbleMode ? "mumble" : "rf";
  const { status, talking, error, startTalk, stopTalk } = useTxAudio(token, {
    onAuthError,
    path: { dstar: "/audio/dstar/tx", mumble: "/audio/mumble/tx", rf: "/audio/tx" }[source],
  });
  const [mode, setMode] = useState(readMode);
  const splitArmed = state?.tx_frequency != null;
  const scanning = !!state?.scan?.running;
  const txReadyIn = useTxLockout(state?.tx_ready_in);

  // Two reasons the RADIO can refuse a key-up, and they are different KINDS of reason (ADR 0154).
  //
  // `tx_ok: false` is a standing station condition: this firmware is built without
  // ENABLE_TX_WHEN_AM, so it sets VFO_STATE_TX_DISABLE in any non-FM modulation — the very path the
  // AIOC's DTR line keys through. It never clears on its own; only an operator changing the
  // demodulator clears it. `tx_ready_in` is transient and counts itself down.
  //
  // `=== false`, never falsy, and it is the same rule the backend enforces in
  // `_refuse_if_tx_disabled`: only a MEASURED false refuses. `null` is "nobody has asked this
  // radio" — every backend but a dock-tuned UV-K5 reports it — and a transmitter disabled by an
  // unknown is a worse failure than the one being prevented.
  const txRefused = state?.tx_ok === false;
  // A THIRD reason, and a different fault again (ADR 0158). The radio's second receiver — a
  // separate commercial-FM chip — holds the speaker line, so the station hears nothing on its own
  // channel.
  //
  // ADR 0158 said this lockout was "not predicting hardware, it is enforcing the server's own
  // refusal", because the radio transmitted perfectly happily while deaf. On F9 firmware that is no
  // longer true — the radio refuses too — so this is now a prediction AND a policy, and the sub-line
  // below stops claiming the host is the only thing in the way. What reaching this state means also
  // changed: since ADR 0161 the server re-reads before every key-up, so `on: true` here is a radio
  // that was asked to stop and did not, rather than a boot-time memory nothing ever refreshed.
  //
  // `=== true`, never truthy, and the mirror of `txRefused`'s `=== false` for the same reason: the
  // block is absent (`null`) on every backend without a dock tuner and on any radio the server
  // never got an answer from, and an unmeasured field must never disable a transmitter.
  const deafened = state?.broadcast_fm?.on === true;
  // The parentheses are load-bearing. `&&` binds tighter than `||`, so without them `txRefused`
  // escapes the `source === "rf" && !talking` guard: Talk-on-Mumble would go dead over a radio
  // nobody is using, and a refusal arriving mid-over would strip the release handlers and strand
  // the key — the exact half-state the pointer-capture comment below exists to prevent.
  const lockedOut = source === "rf" && !talking && (deafened || txRefused || txReadyIn > 0);

  useEffect(() => {
    onTalkingChange?.(talking);
  }, [talking, onTalkingChange]);

  // The TX socket has no reconnect; if the target endpoint changes (source switch) while keyed, stop
  // so the next key opens against the right one.
  const prevSource = useRef(source);
  useEffect(() => {
    if (prevSource.current !== source) {
      prevSource.current = source;
      if (talking) stopTalk();
    }
  }, [source, talking, stopTalk]);

  const setModePersisted = useCallback((next) => {
    setMode(next);
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch {
      /* storage unavailable — mode still applies for this session */
    }
  }, []);

  // Hold-mode Spacebar: keydown keys, keyup drops. Ignore auto-repeat and typing in a field, and
  // preventDefault so Space doesn't scroll the page or re-activate a focused button.
  //
  // The keydown MUST consult the lockout (ADR 0154). It did not until this cycle: `holdProps` only
  // ever stripped the POINTER handlers off the button, so a greyed Talk button still keyed from the
  // keyboard. That was cosmetic while the only lockout lasted six seconds — but an AM refusal never
  // expires, so the keyboard would have kept keying a radio that cannot transmit, indefinitely, and
  // the refusal surfaces to the operator as "Transmit connection dropped." The lockout is the only
  // place AM is ever named, so a bypass makes it useless.
  //
  // `lockedOut` is in the deps on purpose: without it the closure captures a stale value from the
  // render that registered the listener. Re-registering is safe — these are window-level listeners,
  // so a keyup can never be dropped mid-hold — and `lockedOut` implies `!talking`, so the guard can
  // never appear part-way through a live over.
  //
  // The keyup is deliberately NOT guarded. It can stop an over the Spacebar did not start (a stray
  // tap during a pointer-held over closes the socket), but refusing to stop is the dangerous
  // direction in a transmitter: a redundant unkey is harmless, a missed one is a stuck key.
  useEffect(() => {
    if (mode !== "hold") return undefined;
    const isTyping = (t) =>
      t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    const down = (e) => {
      if (e.code !== "Space" || e.repeat || isTyping(e.target)) return;
      e.preventDefault();
      if (lockedOut) return;
      startTalk();
    };
    const up = (e) => {
      if (e.code !== "Space" || isTyping(e.target)) return;
      e.preventDefault();
      stopTalk();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [mode, startTalk, stopTalk, lockedOut]);

  const requesting = status === "requesting";

  const talkVerb = { dstar: "Talk on the reflector", mumble: "Talk on Mumble", rf: "Talk (transmit)" }[
    source
  ];
  // The standing reason is named first when both apply: the countdown clears itself, AM does not, so
  // a label that expired into a still-dead button would be the silent failure over again.
  //
  // `tx_ok: false` means NON-FM, not AM — the wire reserves USB — so the demodulator is named only
  // when the radio actually reported one. `null` gets the weaker sentence that is still true.
  // Deafness outranks the demodulator, matching the order the backend refuses in. It is not a
  // preference: an operator told only about AM would set FM and get a station that now DOES
  // transmit and still cannot hear — strictly worse than where they started.
  const lockoutLabel = deafened
    ? "Transmit disabled — radio is on broadcast FM"
    : txRefused
      ? state?.modulation
        ? `Transmit disabled — radio is on ${state.modulation}`
        : "Transmit disabled — radio is not on FM"
      : `Radio ready in ${Math.ceil(txReadyIn)}s…`;

  const holdLabel = talking
    ? "On air — release to stop"
    : lockedOut
      ? lockoutLabel
      : requesting
        ? "Requesting mic…"
        : { dstar: "Hold to talk on the reflector", mumble: "Hold to talk on Mumble", rf: "Hold to talk" }[
            source
          ];
  const toggleLabel = talking
    ? "Stop talking"
    : lockedOut
      ? lockoutLabel
      : requesting
        ? "Requesting mic…"
        : talkVerb;

  // Hold mode uses pointer capture: capturing on pointerdown routes the real release back to THIS
  // button even if the pointer slides off, and — critically — stops the browser from
  // firing a spurious `pointerleave` the instant `talking` flips true and the button re-renders,
  // which used to close the socket and unkey mid-hold. With capture, `lostpointercapture` is the
  // one authoritative stop: it fires on a genuine release AND on a real cancel/forced capture loss,
  // so stuck-key safety is preserved without a leave handler. The button stays enabled in hold mode
  // so the capture holds; toggle mode disables during the brief mic request as before.
  const holdProps =
    lockedOut
      ? // No handlers at all while locked out: a pointerdown that starts a capture on a disabled
        // button is the kind of half-state that strands a key.
        { disabled: true }
      : mode === "hold"
      ? {
          onPointerDown: (e) => {
            e.preventDefault();
            try {
              e.currentTarget.setPointerCapture(e.pointerId);
            } catch {
              /* older engine without pointer capture — the pointerup fallback still stops */
            }
            startTalk();
          },
          onPointerUp: stopTalk, // fallback stop when pointer capture is unsupported
          onLostPointerCapture: stopTalk, // authoritative stop: real release OR real cancel
          onContextMenu: (e) => e.preventDefault(), // suppress the touch long-press menu
        }
      : { onClick: talking ? stopTalk : startTalk, disabled: requesting };

  return (
    <div className="card">
      <div className="log-head">
        <h2>{{ dstar: "Transmit — D-STAR", mumble: "Transmit — Mumble", rf: "Transmit" }[source]}</h2>
        <span className="head-tools">
          {talking && (
            <span className="conn conn-onair">
              <span className="conn-dot" aria-hidden="true" />
              on air
            </span>
          )}
          <div className="segmented" role="group" aria-label="Talk trigger mode">
            <button
              type="button"
              className={`seg${mode === "hold" ? " active" : ""}`}
              onClick={() => setModePersisted("hold")}
            >
              Hold
            </button>
            <button
              type="button"
              className={`seg${mode === "toggle" ? " active" : ""}`}
              onClick={() => setModePersisted("toggle")}
            >
              Toggle
            </button>
          </div>
        </span>
      </div>

      <button
        type="button"
        className={`ptt talk ${talking ? "keyed" : ""}${lockedOut ? " locked-out" : ""}`}
        {...holdProps}
      >
        {mode === "hold" ? holdLabel : toggleLabel}
      </button>

      {/* Why the button is dead, in the operator's terms. Saying so beats a greyed button — and the
          two reasons need different sentences, because one waits itself out and the other needs the
          operator to go and do something. The tune-mute line is ADR 0144's: the radio mutes itself
          for six seconds after the channel is written to its memory.

          The AM line has to be ACTIONABLE, not merely explanatory, and it names the cost as well as
          the cause: the remedy lives in a different card from this button, the condition never
          clears on its own, and what stops is not only the over — the station ID Part 97 requires
          and every voice service go with it (ADR 0154).

          The broadcast-FM line still carries its own LIMIT, and ADR 0161 rewrote both halves of it
          because the old ones stopped being true.

          It used to say: restart the server, because the second receiver is read ONCE at startup;
          and an enabled Talk button is not proof the radio is hearing. Both were honest and both
          are now wrong. The server re-reads the receiver — and clears it — immediately before every
          key-up, so pressing EXIT IS the whole remedy, and telling an operator to reboot a station
          that would have worked on the next press is worse than saying nothing. And with F9 the
          radio refuses to transmit while deaf on its own, so a sub-line implying the host is the
          only thing standing between the station and a blind carrier misdescribes which mechanism
          is doing the work.

          What replaces them is a smaller, truer claim: this button reflects the LAST KEY-UP rather
          than this instant. The server deliberately does not poll — the only frame that reads this
          state is the one that also switches the receiver off, and a status poll that reaches into
          the radio and changes it is not a status poll. So the honest limit is staleness, not
          ignorance, and it comes with the two things that now bound it. */}
      {lockedOut && (
        <div className="talk-target">
          {deafened
            ? "The radio's second receiver is tuned to a broadcast station and holds the speaker, so this radio hears nothing on this channel — a transmission would go out blind, station ID included. Clear it on the radio (press EXIT); the server re-reads and clears the receiver before every key-up, so the next press picks it up on its own. This button reflects the last key-up rather than this instant, but on this firmware the radio refuses to transmit while it cannot hear itself."
            : txRefused
              ? "This radio's firmware disables its own transmit path in anything but FM, so a key-up would be silence — no over, no station ID, no voice services. Set Demodulation to FM in the Tune card."
              : "Channel stored — the radio mutes its transmitter briefly after that."}
        </div>
      )}

      {/* Where this button will actually transmit, stated positively and both ways (ADR 0134).
          The armed split is process-local and a scan hop, a manual retune or a service restart all
          clear it silently — before this, the ONLY signal was a Status row that disappeared, which
          is not something an operator holding the button can notice. Saying SIMPLEX out loud is the
          load-bearing half: it is what a repeater preset looks like once its TX leg is gone. */}
      {source === "rf" && hasCap("set_split") && state?.frequency != null && (
        <div className={`talk-target${splitArmed ? " split" : ""}`}>
          {scanning
            ? // A scan retunes on every hop and emits only `scan` frames, so `state.frequency` is
              // frozen at whatever the last status snapshot said. Naming a stale number here would
              // be worse than naming none — and a scan clears the split, so simplex is certain.
              "SIMPLEX — scanning, transmits on the current hop"
            : splitArmed
              ? `SPLIT — transmits on ${fmtMHz(state.tx_frequency)} MHz`
              : `SIMPLEX — transmits on ${fmtMHz(state.frequency)} MHz`}
        </div>
      )}

      {error && (
        <div className={status === "busy" ? "notice" : "error"} role="alert">
          {error}
        </div>
      )}
      {status === "idle" && (
        <div className="muted">
          {source === "dstar"
            ? mode === "hold"
              ? "Hold the button (or Spacebar) to talk on the linked D-STAR reflector."
              : "Click Talk to talk on the linked D-STAR reflector."
            : source === "mumble"
              ? mode === "hold"
                ? "Hold the button (or Spacebar) to talk on the Mumble channel."
                : "Click Talk to talk on the Mumble channel."
              : mode === "hold"
                ? "Hold the button (or Spacebar) to key the radio and speak through the gateway."
                : "Click Talk to key the radio and speak through the gateway."}
        </div>
      )}
    </div>
  );
}
