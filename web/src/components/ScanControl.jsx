// Scan control (ADR 0022, async start/stop in ADR 0028). POST /scan starts a background scan and
// POST /scan/stop ends it, so this offers a real Start/Stop pair (modeled on ControllerControl) that
// reflects live scan-phase events. Greyed on a backend without the `scan` capability. Addressing is
// one-of: an explicit frequency list, or a start/stop/step range.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

export default function ScanControl({ client, enabled, scan, onAuthError, onUnsupported }) {
  const [mode, setMode] = useState("list");
  const [list, setList] = useState("146.520, 146.640, 147.000");
  const [start, setStart] = useState("146.000");
  const [stop, setStop] = useState("148.000");
  const [step, setStep] = useState("0.025");
  const [running, setRunning] = useState(null); // null = unknown until first response/event
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  const mhzToHz = (v) => Math.round(parseFloat(v) * 1e6);

  // Keep running state in sync with live scan events, so a scan started/stopped by another client
  // (or torn down at server shutdown) is reflected here too. A `stopped` phase means idle.
  useEffect(() => {
    if (scan) setRunning(scan.phase !== "stopped");
  }, [scan]);

  const startScan = async () => {
    const plan =
      mode === "list"
        ? { frequencies: list.split(",").map((s) => mhzToHz(s.trim())).filter(Number.isFinite) }
        : { start_hz: mhzToHz(start), stop_hz: mhzToHz(stop), step_hz: mhzToHz(step) };
    const res = await run(() => client.scan(plan));
    if (res) setRunning(true);
  };

  const stopScan = async () => {
    const res = await run(() => client.scanStop());
    if (res) setRunning(false);
  };

  return (
    <div className="card">
      <div className="log-head">
        <h2>Scan</h2>
        {scan && scan.phase !== "stopped" && (
          <span className="scan-live">
            {scan.phase}
            {scan.frequency ? ` @ ${(scan.frequency / 1e6).toFixed(4)} MHz` : ""}
          </span>
        )}
      </div>
      <div className="tune-row">
        <label>Addressing</label>
        <select value={mode} disabled={!enabled || running === true} onChange={(e) => setMode(e.target.value)}>
          <option value="list">Frequency list</option>
          <option value="range">Range (start/stop/step)</option>
        </select>
      </div>
      {mode === "list" ? (
        <div className="tune-row">
          <label>Freqs (MHz)</label>
          <input value={list} disabled={!enabled || running === true} onChange={(e) => setList(e.target.value)} />
        </div>
      ) : (
        <div className="tune-row range">
          <input value={start} disabled={!enabled || running === true} onChange={(e) => setStart(e.target.value)} aria-label="start MHz" />
          <input value={stop} disabled={!enabled || running === true} onChange={(e) => setStop(e.target.value)} aria-label="stop MHz" />
          <input value={step} disabled={!enabled || running === true} onChange={(e) => setStep(e.target.value)} aria-label="step MHz" />
        </div>
      )}
      <div className="btn-row">
        <button type="button" className="primary" onClick={startScan} disabled={!enabled || pending || running === true}>
          {pending && running !== true ? "Starting…" : "Scan"}
        </button>
        <button type="button" onClick={stopScan} disabled={!enabled || pending || running === false || running === null}>
          Stop
        </button>
      </div>
      {!enabled && <div className="notice">Not supported on this radio.</div>}
      {error && <div className="error" role="alert">{error}</div>}
    </div>
  );
}
