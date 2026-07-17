// Monitor (Listen) control (ADR 0023): live receive audio in the browser.
//
// A user gesture (the Listen button) is required to start audio — browsers hold a fresh AudioContext
// suspended, so nothing can auto-play. Once listening, the card shows a stream-connection badge, a
// mute toggle, and a level meter driven by the incoming PCM. When the arbiter suspends RX during TX
// (half-duplex — keying blinds the receiver), `/audio/rx` simply stops delivering frames; the player
// glides to silence and we surface a "receive paused — transmitting" note, driven off the same
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
  mumbleMode = false,
  onAuthError,
}) {
  // ADR 0050: while a Mumble link is active, the monitor plays the Mumble channel instead of RF —
  // same hook, different source endpoint. The `paused` half-duplex note is RF-only (Mumble is
  // full-duplex and never blinds on TX).
  const { listening, conn, muted, listen, stop, toggleMute } = useRxAudio(token, {
    onAuthError,
    forceMute: suspendedLocally,
    path: mumbleMode ? "/audio/mumble/rx" : "/audio/rx",
  });

  // On a link connect/disconnect the source endpoint changes; stop the stream so the next Listen
  // re-opens against the right one (the operator re-clicks — simplest robust switch).
  const prevMode = useRef(mumbleMode);
  useEffect(() => {
    if (prevMode.current !== mumbleMode) {
      prevMode.current = mumbleMode;
      if (listening) stop();
    }
  }, [mumbleMode, listening, stop]);

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

  const paused =
    !mumbleMode && listening && (suspendedLocally || transmitting || arbiter === "transmitting");

  return (
    <div className="card">
      <div className="log-head">
        <h2>{mumbleMode ? "Monitor — Mumble" : "Monitor"}</h2>
        <span className="head-tools">
          {listening && <StreamBadge conn={conn} />}
          <button type="button" onClick={toggleMute} disabled={!listening}>
            {muted ? "Unmute" : "Mute"}
          </button>
        </span>
      </div>

      <button
        type="button"
        className={`ptt ${listening ? "keyed-listen" : ""}`}
        onClick={listening ? stop : listen}
      >
        {listening
          ? "Stop listening"
          : mumbleMode
            ? "Listen to Mumble"
            : "Listen (receive audio)"}
      </button>

      {paused && (
        <div className="notice notice-rx-paused" role="status">
          Receive paused — transmitting
        </div>
      )}
      {!listening && (
        <div className="muted">
          {mumbleMode
            ? "Click Listen to hear the Mumble channel."
            : "Click Listen to play what the radio hears."}
        </div>
      )}
    </div>
  );
}

function StreamBadge({ conn }) {
  const label = { open: "live", connecting: "connecting…", reconnecting: "reconnecting…" }[conn];
  return (
    <span className={`conn conn-${conn}`}>
      <span className="conn-dot" aria-hidden="true" />
      {label ?? conn}
    </span>
  );
}
