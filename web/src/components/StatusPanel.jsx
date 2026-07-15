// Live status panel (ADR 0022, simplified in ADR 0037): the folded /events state, rendered for an
// everyday operator. The three low-level flags (transmitting / channel-busy / arbiter) collapse into
// one clear state pill — On air / Receiving / Idle. The CAT fields (frequency/channel/tone/mode/scan)
// only render when the backend advertised the matching capability, so an audio-only radio shows just
// the pill + backend instead of a column of "—". `hasCap` defaults permissive so the panel still
// renders everything if a caller omits it.

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

// One prominent state derived from the live flags. Transmitting wins over busy (half-duplex: while we
// key, RX is suspended anyway), and idle is the resting state.
function radioState(s) {
  if (s.transmitting) return { label: "On air", cls: "state-tx" };
  if (s.busy) return { label: "Receiving", cls: "state-rx" };
  return { label: "Idle", cls: "state-idle" };
}

export default function StatusPanel({ state, hasCap = () => true }) {
  const s = state || {};
  const scan = s.scan ? `${s.scan.phase}${s.scan.frequency ? ` @ ${fmtHz(s.scan.frequency)}` : ""}` : "—";
  const sessionOpen = s.session ? s.session.phase === "session_open" : s.sessionOpen;
  const st = radioState(s);
  return (
    <div className="card">
      <h2>Status</h2>
      <div className={`state-pill ${st.cls}`} role="status">
        <span className="state-dot" aria-hidden="true" />
        {st.label}
      </div>
      <Row label="Backend" value={s.backend ?? "—"} />
      {hasCap("set_frequency") && <Row label="Frequency" value={fmtHz(s.frequency)} />}
      {hasCap("set_channel") && <Row label="Channel" value={s.channel ?? "—"} />}
      {hasCap("set_tone") && <Row label="Tone" value={s.tone != null ? `${s.tone} Hz` : "—"} />}
      {hasCap("set_mode") && <Row label="Mode" value={s.mode ?? "—"} />}
      {hasCap("scan") && <Row label="Scan" value={scan} />}
      {sessionOpen && <Row label="Session" value="open" on />}
    </div>
  );
}
