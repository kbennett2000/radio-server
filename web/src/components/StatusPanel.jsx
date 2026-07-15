// Live status panel (ADR 0022): the folded /events state, rendered. CAT fields (frequency/channel/
// tone/mode) are null on an audio-only backend and show as "—". transmitting/busy/arbiter/session/
// scan update the instant their frames arrive.

function fmtHz(hz) {
  if (hz == null) return "—";
  return `${(hz / 1e6).toFixed(4)} MHz`;
}

function Row({ label, value, on }) {
  return (
    <div className="status-row">
      <span className="status-label">{label}</span>
      <span className={`status-value${on ? " on" : ""}`}>{value}</span>
    </div>
  );
}

export default function StatusPanel({ state }) {
  const s = state || {};
  const scan = s.scan ? `${s.scan.phase}${s.scan.frequency ? ` @ ${fmtHz(s.scan.frequency)}` : ""}` : "—";
  const session = s.session ? s.session.phase : s.sessionOpen ? "open" : "—";
  return (
    <div className="card">
      <h2>Status</h2>
      <Row label="Backend" value={s.backend ?? "—"} />
      <Row label="Transmitting" value={s.transmitting ? "ON AIR" : "no"} on={s.transmitting} />
      <Row label="Channel busy" value={s.busy ? "yes" : "no"} on={s.busy} />
      <Row label="Frequency" value={fmtHz(s.frequency)} />
      <Row label="Channel" value={s.channel ?? "—"} />
      <Row label="Tone" value={s.tone != null ? `${s.tone} Hz` : "—"} />
      <Row label="Mode" value={s.mode ?? "—"} />
      <Row label="Arbiter" value={s.arbiter ?? "—"} />
      <Row label="Session" value={session} />
      <Row label="Scan" value={scan} />
    </div>
  );
}
