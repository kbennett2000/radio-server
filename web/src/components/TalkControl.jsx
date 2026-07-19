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

function readMode() {
  try {
    return window.localStorage.getItem(MODE_KEY) === "toggle" ? "toggle" : "hold";
  } catch {
    return "hold";
  }
}

export default function TalkControl({
  token,
  onAuthError,
  onTalkingChange,
  mumbleMode = false,
  dstarMode = false,
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
  useEffect(() => {
    if (mode !== "hold") return undefined;
    const isTyping = (t) =>
      t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    const down = (e) => {
      if (e.code !== "Space" || e.repeat || isTyping(e.target)) return;
      e.preventDefault();
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
  }, [mode, startTalk, stopTalk]);

  const requesting = status === "requesting";

  const talkVerb = { dstar: "Talk on the reflector", mumble: "Talk on Mumble", rf: "Talk (transmit)" }[
    source
  ];
  const holdLabel = talking
    ? "On air — release to stop"
    : requesting
      ? "Requesting mic…"
      : { dstar: "Hold to talk on the reflector", mumble: "Hold to talk on Mumble", rf: "Hold to talk" }[
          source
        ];
  const toggleLabel = talking ? "Stop talking" : requesting ? "Requesting mic…" : talkVerb;

  // Hold mode uses pointer capture: capturing on pointerdown routes the real release back to THIS
  // button even if the pointer slides off, and — critically — stops the browser from
  // firing a spurious `pointerleave` the instant `talking` flips true and the button re-renders,
  // which used to close the socket and unkey mid-hold. With capture, `lostpointercapture` is the
  // one authoritative stop: it fires on a genuine release AND on a real cancel/forced capture loss,
  // so stuck-key safety is preserved without a leave handler. The button stays enabled in hold mode
  // so the capture holds; toggle mode disables during the brief mic request as before.
  const holdProps =
    mode === "hold"
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

      <button type="button" className={`ptt talk ${talking ? "keyed" : ""}`} {...holdProps}>
        {mode === "hold" ? holdLabel : toggleLabel}
      </button>

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
