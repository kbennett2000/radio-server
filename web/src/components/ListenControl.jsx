// Listen control (ADR 0023): live receive audio in the browser.
//
// A user gesture (the Listen button) is required to start audio — browsers hold a fresh AudioContext
// suspended, so nothing can auto-play. Once listening, the card shows a stream-connection badge, a
// mute toggle, and a level meter driven by the incoming PCM. When the arbiter suspends RX during TX
// (half-duplex — keying blinds the receiver), `/audio/rx` simply stops delivering frames; the player
// glides to silence and we surface a "receiving paused (transmitting)" note, driven off the same
// `/events` state (`transmitting` / `arbiter`) the rest of the panel already folds.
//
// `suspendedLocally` (ADR 0024) is true while THIS operator is talking: we force-mute the monitor
// the instant we key so the ~500 ms of already-buffered RX audio doesn't play us hearing ourselves.

import { useEffect, useRef } from "react";
import { useRxAudio } from "../useRxAudio.js";

export default function ListenControl({
  token,
  transmitting,
  arbiter,
  suspendedLocally = false,
  autoStart = false,
  onAuthError,
}) {
  const { listening, conn, muted, level, listen, stop, toggleMute } = useRxAudio(token, {
    onAuthError,
    forceMute: suspendedLocally,
  });

  // Auto-listen (ADR 0037): start once when the preference first arrives, riding the login gesture's
  // sticky activation so the browser lets audio play. A ref makes it fire only once — a later manual
  // Stop must not be undone, and re-renders must not re-key. `listen()` itself guards double-start.
  const autoStarted = useRef(false);
  useEffect(() => {
    if (autoStart && !autoStarted.current && !listening) {
      autoStarted.current = true;
      listen();
    }
  }, [autoStart, listening, listen]);

  const paused = listening && (suspendedLocally || transmitting || arbiter === "transmitting");
  const pct = Math.min(100, Math.round(level * 100));

  return (
    <div className="card">
      <div className="log-head">
        <h2>Listen</h2>
        {listening && <StreamBadge conn={conn} />}
      </div>

      <button
        type="button"
        className={`ptt ${listening ? "keyed-listen" : ""}`}
        onClick={listening ? stop : listen}
      >
        {listening ? "Stop listening" : "Listen (receive audio)"}
      </button>

      <div className="btn-row listen-row">
        <button type="button" onClick={toggleMute} disabled={!listening}>
          {muted ? "Unmute" : "Mute"}
        </button>
        <div className="meter" aria-label="receive level" title="receive level">
          <div className={`meter-fill ${muted ? "meter-muted" : ""}`} style={{ width: `${pct}%` }} />
        </div>
      </div>

      {paused && (
        <div className="notice" role="status">
          Receiving paused (transmitting)
        </div>
      )}
      {!listening && (
        <div className="muted">Click Listen to play what the radio hears.</div>
      )}
    </div>
  );
}

function StreamBadge({ conn }) {
  const label = { open: "live", connecting: "connecting…", reconnecting: "reconnecting…" }[conn];
  return <span className={`conn conn-${conn}`}>● {label ?? conn}</span>;
}
