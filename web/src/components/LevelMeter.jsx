// The LED-segment level meter (ADR 0044) shared by Monitor (RX) and Talk (MIC). Three layers:
// the track, an absolutely-positioned gradient fill whose width is the level, and a
// repeating-gradient overlay that slices the fill into LED segments. Purely presentational —
// the level itself comes from the caller's audio hook.

export default function LevelMeter({ label, pct, kind, dimmed = false, ariaLabel }) {
  return (
    <div className="meter-row">
      <span className="meter-label" aria-hidden="true">
        {label}
      </span>
      <div className="meter" aria-label={ariaLabel} title={ariaLabel}>
        <div
          className={`meter-fill meter-${kind}${dimmed ? " meter-dim" : ""}`}
          style={{ width: `${pct}%` }}
        />
        <div className="meter-segments" aria-hidden="true" />
      </div>
    </div>
  );
}
