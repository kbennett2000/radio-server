// Controller start/stop (ADR 0022): POST /controller {on}. When no controller is wired the API
// returns 503 — we surface "controller not configured in this deployment" and disable the toggle
// (not a dead button). To exercise start/stop against the mock, launch it with RADIO_TOTP_SECRET.

import { useState } from "react";
import { useAction } from "../actions.js";
import { ControllerUnavailable } from "../api.js";

export default function ControllerControl({ client, session, onAuthError, onUnsupported }) {
  const [running, setRunning] = useState(null); // null = unknown until first call/response
  const [unavailable, setUnavailable] = useState(false);
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  const set = async (on) => {
    try {
      const res = await client.controller(on);
      setRunning(res?.controller?.running ?? on);
      setUnavailable(false);
    } catch (e) {
      if (e instanceof ControllerUnavailable) {
        setUnavailable(true);
        return;
      }
      // Route auth/other via the shared hook for consistent handling.
      run(() => Promise.reject(e));
    }
  };

  const sessionOpen = session?.sessionOpen ?? session?.session?.phase === "session_open";

  return (
    <div className="card">
      <h2>Controller</h2>
      {unavailable ? (
        <div className="notice">Controller not configured in this deployment.</div>
      ) : (
        <>
          <div className="btn-row">
            <button type="button" onClick={() => set(true)} disabled={pending || running === true}>
              Start
            </button>
            <button type="button" onClick={() => set(false)} disabled={pending || running === false}>
              Stop
            </button>
          </div>
          <div className="muted">
            Running: {running == null ? "—" : running ? "yes" : "no"}
            {sessionOpen ? " · session open" : ""}
          </div>
        </>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </div>
  );
}
