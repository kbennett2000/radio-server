// Shared level-meter scaling for the Monitor (RX) and Transmit (MIC) bars.
//
// The meters are driven by a linear peak amplitude in 0..1 (the max |sample| since the last frame,
// already attack/decay-smoothed by the audio hooks). Mapping that linear peak straight to bar width
// badly under-fills the meter: speech and radio audio sound "loud" while their peak sits around
// 0.05–0.3, so even a maxed signal only lit a sliver of the track. Map a dB window onto the bar
// instead — the way a real VU/PPM meter reads — so ordinary loud audio fills most of it.
//
// -60 dBFS -> 0%, 0 dBFS (full scale) -> 100%. The floor is the one knob: raise it (toward 0) for a
// hotter-reading meter, lower it for more headroom.

const FLOOR_DB = -60;

export function levelToPct(level) {
  if (!(level > 0)) return 0; // also guards NaN (NaN > 0 is false)
  const db = 20 * Math.log10(level);
  const pct = ((db - FLOOR_DB) / -FLOOR_DB) * 100;
  return Math.max(0, Math.min(100, Math.round(pct)));
}
