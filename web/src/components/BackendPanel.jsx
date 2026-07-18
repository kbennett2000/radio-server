// The backend selector card (ADR 0077): pick the active radio at runtime, driving the ADR 0076 live
// switch (GET /radio/backends + POST /radio/select). A successful select re-emits capabilities+status
// server-side, so ControlPanel re-greys the tuning/scan controls without a reconnect (the payoff of
// the whole switch arc).
//
// One radio is active at a time. The <select> tracks the *live* active backend (`active`, folded from
// the WS status by useEvents), not an optimistic pick — so a failed switch (503; the server has
// already rolled back to the previous radio) snaps the control back to the real radio and the error
// banner says which one you're still on. It never implies a switch that didn't happen.
//
// Self-hides when fewer than two backends are configured — with nothing to switch to, a selector is
// just noise (the LinkPanel hide-when-unconfigured pattern). Both backends must be present in
// radio.toml to appear here (ADR 0074 multi-backend config).

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

export default function BackendPanel({ client, active, transmitting, onAuthError }) {
  const [backends, setBackends] = useState(null); // null = not loaded yet; [] / [one] = self-hide
  const [fetchedActive, setFetchedActive] = useState(null);
  const { run, pending, error } = useAction({ onAuthError });

  // The live active backend: the WS-folded status wins (`active` = state.backend), else the value the
  // mount fetch reported. This is reality — the switch only "took" once this changes.
  const current = active ?? fetchedActive;

  // What the dropdown shows: it optimistically follows the operator's pick while a switch is in
  // flight, then re-syncs to `current` once it settles — so a success stays put and a failure (where
  // `current` never changed) snaps back to the radio you're still on.
  const [choice, setChoice] = useState(null);
  useEffect(() => {
    if (!pending && current != null) setChoice(current);
  }, [current, pending]);

  // Load the configured list once on mount — WS status frames are RadioStatus-only and carry no
  // backend list. A failed fetch leaves the card hidden rather than showing a broken selector.
  useEffect(() => {
    let live = true;
    client
      .backends()
      .then((body) => {
        if (!live) return;
        setBackends(body?.backends ?? []);
        if (body?.active) setFetchedActive(body.active);
      })
      .catch(() => {
        /* non-fatal: keep the card hidden */
      });
    return () => {
      live = false;
    };
  }, [client]);

  if (!backends || backends.length <= 1) return null;

  const onChange = (e) => {
    const name = e.target.value;
    if (name === current) return; // re-picking the active radio is a no-op, not a switch
    setChoice(name);
    run(() => client.selectBackend(name));
  };

  const value = choice ?? current ?? "";

  return (
    <div className="card">
      <h2>Radio</h2>
      <div className="tune-row">
        <label htmlFor="backend-select">Backend</label>
        <select id="backend-select" value={value} disabled={pending} onChange={onChange}>
          {backends.map((b) => (
            <option key={b.name} value={b.name}>
              {b.name}
              {b.name === current ? " (active)" : ""}
            </option>
          ))}
        </select>
        {pending && <span className="muted">Switching…</span>}
      </div>
      {/* The kv4p reboots on open, so a switch takes a beat; and the server drops PTT on teardown —
          surface that honestly rather than hiding it behind an instant-looking control. */}
      {transmitting && (
        <p className="muted">On air — switching radios drops the current transmission.</p>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
