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
  dstarMode = false,
  onAuthError,
}) {
  // ADR 0050/0088: with a Mumble link or a D-STAR reflector as the browser's audio target, the
  // monitor plays that channel instead of RF — same hook, different source endpoint. D-STAR wins if
  // both apply. The `paused` half-duplex note is RF-only (network peers never blind the monitor on TX).
  const source = dstarMode ? "dstar" : mumbleMode ? "mumble" : "rf";
  const { listening, conn, muted, volume, setVolume, listen, stop, toggleMute } = useRxAudio(token, {
    onAuthError,
    forceMute: suspendedLocally,
    path: { dstar: "/audio/dstar/rx", mumble: "/audio/mumble/rx", rf: "/audio/rx" }[source],
  });

  // On a source change (link connect/disconnect, or a D-STAR node) the endpoint changes; stop the
  // stream so the next Listen re-opens against the right one (the operator re-clicks — simplest switch).
  const prevSource = useRef(source);
  useEffect(() => {
    if (prevSource.current !== source) {
      prevSource.current = source;
      if (listening) stop();
    }
  }, [source, listening, stop]);

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
    source === "rf" && listening && (suspendedLocally || transmitting || arbiter === "transmitting");
  const title = { dstar: "Monitor — D-STAR", mumble: "Monitor — Mumble", rf: "Monitor" }[source];

  return (
    <div className="card">
      <div className="log-head">
        <h2>{title}</h2>
        <span className="head-tools">
          {listening && <StreamBadge conn={conn} />}
          <label className="rx-volume" title="Playback volume (headroom against clipping)">
            <span className="rx-volume-label">Vol</span>
            <input
              type="range"
              min="0"
              max="100"
              value={Math.round(volume * 100)}
              onChange={(e) => setVolume(Number(e.target.value) / 100)}
              aria-label="Playback volume"
            />
          </label>
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
          : { dstar: "Listen to the reflector", mumble: "Listen to Mumble", rf: "Listen (receive audio)" }[
              source
            ]}
      </button>

      {paused && (
        <div className="notice notice-rx-paused" role="status">
          Receive paused — transmitting
        </div>
      )}
      {!listening && (
        <div className="muted">
          {
            {
              dstar: "Click Listen to hear the linked D-STAR reflector.",
              mumble: "Click Listen to hear the Mumble channel.",
              rf: "Click Listen to play what the radio hears.",
            }[source]
          }
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
