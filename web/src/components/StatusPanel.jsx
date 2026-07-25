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

// `on` is the green good/active emphasis (session open, split armed). `warn` is its opposite and
// must not reuse it — a "power is uncharacterised" alarm painted the same colour as "session open"
// reads as reassurance (ADR 0134).
function Row({ label, value, on, warn }) {
  return (
    <div className="status-row">
      <span className="status-label">{label}</span>
      <span className={`status-value${on ? " on" : ""}${warn ? " warn" : ""}`}>{value}</span>
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
      {/* Only when a split is actually armed. The face's LCD shows the RX frequency, so without
          this row nothing anywhere tells the operator that PTT keys somewhere else (ADR 0133). */}
      {hasCap("set_split") && s.tx_frequency != null && (
        <Row label="Transmits on" value={fmtHz(s.tx_frequency)} on />
      )}
      {hasCap("set_channel") && <Row label="Channel" value={s.channel ?? "—"} />}
      {hasCap("set_tone") && <Row label="Tone" value={s.tone != null ? `${s.tone} Hz` : "—"} />}
      {hasCap("scan") && <Row label="Scan" value={scan} />}
      <Row label="OTA session" value={sessionOpen ? "open" : "—"} on={!!sessionOpen} />
      {/* What the last over actually radiated. Absent until the first key-up of the process, and
          absent on backends that cannot see their PA. The wrong-band case is the one worth reading:
          the bias byte is the other band's calibration and the output level is uncharacterised, so
          it is called out rather than shown as a number nobody can interpret (ADR 0134). */}
      {s.pa && (
        <Row
          label="PA (last over)"
          value={
            s.pa.band_matched
              ? `bias ${s.pa.bias} · ${fmtHz(s.pa.tx_frequency)}`
              : `bias ${s.pa.bias} — WRONG BAND, power uncharacterised`
          }
          warn={!s.pa.band_matched}
        />
      )}
    </div>
  );
}
