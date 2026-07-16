// Live status panel (ADR 0022, simplified in ADR 0037): the folded /events state, rendered as
// compact label/value rows for an everyday operator. The prominent On air / Receiving / Idle lamp
// lives in the radio-face header (ADR 0044 — see ControlPanel's StateLamp), and the frequency/mode
// readout is the face's LCD, so this card carries the remaining facts. The CAT fields
// (channel/tone/scan) only render when the backend advertised the matching capability, so an
// audio-only radio shows just Backend instead of a column of "—". `hasCap` defaults permissive so
// the panel still renders everything if a caller omits it.

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

export default function StatusPanel({ state, hasCap = () => true }) {
  const s = state || {};
  const scan = s.scan ? `${s.scan.phase}${s.scan.frequency ? ` @ ${fmtHz(s.scan.frequency)}` : ""}` : "—";
  const sessionOpen = s.session ? s.session.phase === "session_open" : s.sessionOpen;
  return (
    <div className="card">
      <h2>Status</h2>
      <Row label="Backend" value={s.backend ?? "—"} />
      {hasCap("set_channel") && <Row label="Channel" value={s.channel ?? "—"} />}
      {hasCap("set_tone") && <Row label="Tone" value={s.tone != null ? `${s.tone} Hz` : "—"} />}
      {hasCap("scan") && <Row label="Scan" value={scan} />}
      <Row label="OTA session" value={sessionOpen ? "open" : "—"} on={!!sessionOpen} />
    </div>
  );
}
