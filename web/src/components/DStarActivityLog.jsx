// The D-STAR reflector activity log (ADR 0089): who's heard on the linked reflector. Each inbound
// over carries the sender's callsign (MYCALL) in its D-STAR header; our own overs (mic or crossband)
// show as "you". Fed by the live `activity` WS events folded into `state.activity`, seeded on mount
// from GET /dstar/status so a reload while linked shows recent traffic immediately. Self-hides when
// D-STAR is unconfigured. This is the client-side live stream, not a persisted ledger.
//
// The list arrives oldest-first; we render newest-first (freshest on top), the EventLog convention.

export default function DStarActivityLog({ dstar, activity }) {
  // Unconfigured node → no card at all (mirrors DStarPanel's self-hide on a null `dstar`).
  if (dstar == null) return null;
  const rows = activity ?? [];
  const linked = !!dstar.active;

  return (
    <div className="card log-card">
      <div className="log-head">
        <h2>Reflector activity</h2>
        {dstar.active?.reflector && <span className="log-sub">{dstar.active.reflector}</span>}
      </div>
      <div className="log">
        {rows.length === 0 && (
          <div className="muted">
            {linked ? "Listening — no traffic yet…" : "Link a reflector to hear activity."}
          </div>
        )}
        {rows
          .slice()
          .reverse()
          .map((a, i) => (
            <div key={rows.length - i} className={`log-row log-activity-${a.dir || "rx"}`}>
              <span className="log-type">{a.dir === "tx" ? "TX" : "RX"}</span>
              <span className="log-call">{a.dir === "tx" ? "you" : a.mycall || "—"}</span>
              <span className="log-summary">{a.reflector || ""}</span>
            </div>
          ))}
      </div>
    </div>
  );
}
