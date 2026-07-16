// The Mumble link card (ADR 0041 Cycle D): the link's live state + a connect/disconnect toggle.
//
// Rendered only when the deployment configured the link (`state.link` is an object; `null` means
// mumble.enabled is off) — the TuneControls hide-when-unconfigured pattern (ADR 0037): an unusable
// control is just noise.
//
// State plumbing: the WS-folded `state.link` (from the `status` frames) is the baseline, but link
// connect is deliberately non-blocking on the server and no WS event fires when the Mumble
// connection later syncs (or drops) — the snapshot published by POST /link usually still says
// `connected: false`. So while the link is running, this card gently polls GET /link/status
// (POLL_MS) and prefers its own fresher snapshot over the WS prop. A dedicated `link` WS event
// would remove the poll — noted as a follow-up in HANDOFF.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

const POLL_MS = 5000;

// One prominent state, the StatusPanel pill idiom: connected wins, then "still trying", then off.
function linkState(l) {
  if (l.running && l.connected) return { label: "Linked", cls: "state-rx" };
  if (l.running) return { label: "Connecting…", cls: "state-warn" };
  return { label: "Off", cls: "state-idle" };
}

function Row({ label, value }) {
  return (
    <div className="status-row">
      <span className="status-label">{label}</span>
      <span className="status-value">{value}</span>
    </div>
  );
}

export default function LinkPanel({ client, link, onAuthError }) {
  // The freshest snapshot we've seen: the POST /link response or a poll result. Cleared whenever
  // the WS prop changes shape so a server-pushed status frame is never shadowed by stale local data.
  const [fresh, setFresh] = useState(null);
  const { run, pending, error } = useAction({ onAuthError });

  const l = fresh ?? link;
  const configured = l != null;
  const running = configured && l.running;

  // Poll while the link is running (cheap: one small JSON GET). Also catches the connect that
  // completes after POST /link returned, and a Mumble-side drop while we sit "Linked".
  useEffect(() => {
    if (!configured || !running) return undefined;
    let live = true;
    const tick = () => {
      client
        .linkStatus()
        .then((body) => {
          if (live && body?.link) setFresh(body.link);
        })
        .catch(() => {
          /* non-fatal: the next status frame or poll refreshes */
        });
    };
    const id = setInterval(tick, POLL_MS);
    tick(); // immediate first refresh so "Connecting…" resolves quickly
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client, configured, running]);

  // A pushed status frame is authoritative going forward — drop the local override.
  useEffect(() => {
    setFresh(null);
  }, [link]);

  if (!configured) return null;

  const st = linkState(l);
  const toggle = () =>
    run(async () => {
      const body = await client.setLink(!running);
      // POST /link returns the fresh link block — reflect it immediately.
      if (body?.link) setFresh(body.link);
    });

  return (
    <div className="card">
      <h2>Mumble link</h2>
      <div className={`state-pill ${st.cls}`} role="status">
        <span className="state-dot" aria-hidden="true" />
        {st.label}
      </div>
      <Row label="Server" value={l.host || "—"} />
      <Row label="Channel" value={l.channel || "(root)"} />
      {l.connected && <Row label="Peers" value={l.peers ?? "—"} />}
      {l.tx_to_rf === false && (
        <p className="muted">
          Receive-only: Mumble voice is not transmitted over the air (mumble.tx_to_rf is off).
        </p>
      )}
      <div className="btn-row">
        <button type="button" onClick={toggle} disabled={pending}>
          {pending ? "Working…" : running ? "Disconnect" : "Connect"}
        </button>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
