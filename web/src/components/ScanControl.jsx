// Scan control (ADR 0022). The API's POST /scan runs ONE synchronous sweep and returns the
// frequency it stopped-and-held on (`held`, or null) — there is no server-side "stop" endpoint, so
// this offers a "Scan" button (start a sweep) and reflects live scan-phase events, with no dead
// stop button. A server-side scan-stop is a future cycle. Greyed on a backend without the `scan`
// capability. Addressing is one-of: an explicit frequency list, or a start/stop/step range.

import { useState } from "react";
import { useAction } from "../actions.js";

export default function ScanControl({ client, enabled, scan, onAuthError, onUnsupported }) {
  const [mode, setMode] = useState("list");
  const [list, setList] = useState("146.520, 146.640, 147.000");
  const [start, setStart] = useState("146.000");
  const [stop, setStop] = useState("148.000");
  const [step, setStep] = useState("0.025");
  const [held, setHeld] = useState(undefined);
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  const mhzToHz = (v) => Math.round(parseFloat(v) * 1e6);

  const sweep = async () => {
    const plan =
      mode === "list"
        ? { frequencies: list.split(",").map((s) => mhzToHz(s.trim())).filter(Number.isFinite) }
        : { start_hz: mhzToHz(start), stop_hz: mhzToHz(stop), step_hz: mhzToHz(step) };
    const res = await run(() => client.scan(plan));
    if (res) setHeld(res.held);
  };

  return (
    <div className="card">
      <h2>Scan</h2>
      <div className="tune-row">
        <label>Addressing</label>
        <select value={mode} disabled={!enabled} onChange={(e) => setMode(e.target.value)}>
          <option value="list">Frequency list</option>
          <option value="range">Range (start/stop/step)</option>
        </select>
      </div>
      {mode === "list" ? (
        <div className="tune-row">
          <label>Freqs (MHz)</label>
          <input value={list} disabled={!enabled} onChange={(e) => setList(e.target.value)} />
        </div>
      ) : (
        <div className="tune-row range">
          <input value={start} disabled={!enabled} onChange={(e) => setStart(e.target.value)} aria-label="start MHz" />
          <input value={stop} disabled={!enabled} onChange={(e) => setStop(e.target.value)} aria-label="stop MHz" />
          <input value={step} disabled={!enabled} onChange={(e) => setStep(e.target.value)} aria-label="step MHz" />
        </div>
      )}
      <button type="button" onClick={sweep} disabled={!enabled || pending}>
        {pending ? "Sweeping…" : "Scan"}
      </button>
      {!enabled && <div className="notice">Not supported on this radio.</div>}
      {scan && <div className="muted">Live: {scan.phase}{scan.frequency ? ` @ ${(scan.frequency / 1e6).toFixed(4)} MHz` : ""}</div>}
      {held !== undefined && (
        <div className="muted">Held: {held ? `${(held / 1e6).toFixed(4)} MHz` : "none (swept clear)"}</div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
    </div>
  );
}
