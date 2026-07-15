// Talk control (ADR 0024; ADR 0037 adds selectable trigger): capture the mic and transmit through
// the gateway.
//
// Two trigger styles, switchable and remembered per browser (localStorage `talkMode`):
//   - "hold"   (default): press-and-hold the button — or hold the Spacebar — to key, release to stop.
//                          The radio-mic feel. Pointer up / leave / cancel all drop PTT so a pointer
//                          that slides off the button can never leave the transmitter stuck keyed.
//   - "toggle": click to start, click again to stop (the original behavior).
//
// A user gesture is required to start either way (getUserMedia needs one and an AudioContext starts
// suspended). Once talking, the card shows a mic level meter and an "on air" state; the button is red
// (`.ptt.keyed`). It reports its talking state up to ControlPanel so the local RX monitor mutes while
// you key (you don't hear yourself gate in/out through the ~500 ms RX jitter buffer).

import { useCallback, useEffect, useState } from "react";
import { useTxAudio } from "../useTxAudio.js";

const MODE_KEY = "radio.talkMode";

function readMode() {
  try {
    return window.localStorage.getItem(MODE_KEY) === "toggle" ? "toggle" : "hold";
  } catch {
    return "hold";
  }
}

export default function TalkControl({ token, onAuthError, onTalkingChange }) {
  const { status, talking, level, error, startTalk, stopTalk } = useTxAudio(token, { onAuthError });
  const [mode, setMode] = useState(readMode);

  useEffect(() => {
    onTalkingChange?.(talking);
  }, [talking, onTalkingChange]);

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
  const pct = Math.min(100, Math.round(level * 100));

  const holdLabel = talking ? "On air — release to stop" : requesting ? "Requesting mic…" : "Hold to talk";
  const toggleLabel = talking ? "Stop talking" : requesting ? "Requesting mic…" : "Talk (transmit)";

  // In hold mode the button must stay enabled so pointerup/leave can drop PTT even mid-request; in
  // toggle mode we disable during the brief mic request as before.
  const holdProps =
    mode === "hold"
      ? {
          onPointerDown: (e) => {
            e.preventDefault();
            startTalk();
          },
          onPointerUp: stopTalk,
          onPointerLeave: () => talking && stopTalk(),
          onPointerCancel: stopTalk,
        }
      : { onClick: talking ? stopTalk : startTalk, disabled: requesting };

  return (
    <div className="card">
      <div className="log-head">
        <h2>Talk</h2>
        {talking && <span className="conn conn-open">● on air</span>}
      </div>

      <div className="segmented" role="group" aria-label="Talk trigger mode">
        <button
          type="button"
          className={`seg${mode === "hold" ? " active" : ""}`}
          onClick={() => setModePersisted("hold")}
        >
          Hold to talk
        </button>
        <button
          type="button"
          className={`seg${mode === "toggle" ? " active" : ""}`}
          onClick={() => setModePersisted("toggle")}
        >
          Click to toggle
        </button>
      </div>

      <button type="button" className={`ptt talk ${talking ? "keyed" : ""}`} {...holdProps}>
        {mode === "hold" ? holdLabel : toggleLabel}
      </button>

      <div className="btn-row listen-row">
        <div className="meter" aria-label="microphone level" title="microphone level">
          <div className="meter-fill meter-tx" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {error && (
        <div className={status === "busy" ? "notice" : "error"} role="alert">
          {error}
        </div>
      )}
      {status === "idle" && (
        <div className="muted">
          {mode === "hold"
            ? "Hold the button (or Spacebar) to key the radio and speak through the gateway."
            : "Click Talk to key the radio and speak through the gateway."}
        </div>
      )}
    </div>
  );
}
