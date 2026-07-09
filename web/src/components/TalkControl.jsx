// Talk control (ADR 0024): capture the mic and transmit through the gateway.
//
// The TX pair to ListenControl. A user gesture (the Talk button) is required to start — getUserMedia
// needs one and an AudioContext starts suspended. Once talking, the card shows a mic level meter and
// an "on air" state; the button is red (`.ptt.keyed`) to distinguish it from Listen's blue. It
// reports its talking state up to ControlPanel so the local RX monitor mutes immediately while you
// key (you don't hear yourself gate in/out through the ~500 ms RX jitter buffer). Clear states for a
// denied mic and a taken single-talker slot ("radio busy"), never a hang.

import { useEffect } from "react";
import { useTxAudio } from "../useTxAudio.js";

export default function TalkControl({ token, onAuthError, onTalkingChange }) {
  const { status, talking, level, error, startTalk, stopTalk } = useTxAudio(token, { onAuthError });

  useEffect(() => {
    onTalkingChange?.(talking);
  }, [talking, onTalkingChange]);

  const requesting = status === "requesting";
  const pct = Math.min(100, Math.round(level * 100));
  const label = talking
    ? "Stop talking"
    : requesting
      ? "Requesting mic…"
      : "Talk (transmit)";

  return (
    <div className="card">
      <div className="log-head">
        <h2>Talk</h2>
        {talking && <span className="conn conn-open">● on air</span>}
      </div>

      <button
        type="button"
        className={`ptt ${talking ? "keyed" : ""}`}
        onClick={talking ? stopTalk : startTalk}
        disabled={requesting}
      >
        {label}
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
        <div className="muted">Click Talk to key the radio and speak through the gateway.</div>
      )}
    </div>
  );
}
