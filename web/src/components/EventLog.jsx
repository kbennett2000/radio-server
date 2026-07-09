// Event log (ADR 0022): the operating log made visible. Every taxonomy frame that streams over
// /events (status/ptt/scan/session/auth/command/arbiter) is listed newest-last as it arrives. This
// is the LIVE stream captured client-side, not the persisted JSONL ledger (which has no GET API
// yet — deferred). The list is bounded upstream (useEvents) so it can't grow without limit.

function fmtTime(d) {
  return d.toLocaleTimeString([], { hour12: false });
}

function summarize(type, data) {
  switch (type) {
    case "status":
      return data.transmitting ? "transmitting" : data.busy ? "busy" : "idle";
    case "ptt":
      return data.on ? "key up" : "key down";
    case "scan":
      return `${data.phase}${data.frequency ? ` @ ${(data.frequency / 1e6).toFixed(4)}MHz` : ""}`;
    case "session":
      return data.phase + (data.callsign ? ` (${data.callsign})` : "");
    case "auth":
      return data.result;
    case "command":
      return data.service ?? "—";
    case "arbiter":
      return data.mode;
    default:
      return JSON.stringify(data);
  }
}

export default function EventLog({ events, onClear }) {
  return (
    <div className="card log-card">
      <div className="log-head">
        <h2>Event log</h2>
        <button type="button" className="link" onClick={onClear} disabled={!events.length}>
          clear
        </button>
      </div>
      <div className="log">
        {events.length === 0 && <div className="muted">Waiting for events…</div>}
        {events.map((e) => (
          <div key={e.id} className={`log-row log-${e.type}`}>
            <span className="log-time">{fmtTime(e.at)}</span>
            <span className="log-type">{e.type}</span>
            <span className="log-summary">{summarize(e.type, e.data)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
