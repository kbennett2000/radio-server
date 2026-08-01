// The SECOND RECEIVER card (ADR 0164, reshaped by ADR 0168).
//
// The UV-K5 carries a BK1080 commercial-FM chip alongside the BK4819 everything else drives. While
// it runs it holds the speaker line the AIOC listens on, so between overs the station hears a
// broadcast station and not its own channel.
//
// ADR 0164 built this card around the MOMENT OF COMMITMENT: three consequences in an amber notice, a
// two-step arm/confirm button, and a red keypad warning while it ran. ADR 0168 takes all three out at
// the operator's instruction (issue #225), and the card becomes a thing an operator uses rather than
// a thing they consent to. Two points about that, so nobody re-derives them later:
//
//  - The consequences did not stop being true. They live in `docs/troubleshooting.md` (written for
//    the operator who is looking at the symptom) and in ADR 0164 (written for whoever changes this).
//  - None of them was ever the Part 97 control. That is the relay mute (ADR 0162) and the
//    pre-key-up clear (ADR 0161), both untouched by this file and unreachable from it. The confirm
//    step was courtesy, and removing courtesy does not remove a control.
//
// THE DESIGN RULE NOW: the frequency and band controls stay on screen WHILE THE RECEIVER RUNS.
// ADR 0164 gated them on `!on` while gating Retune on `on` — so the only two controls that could
// change what Retune sends were unmounted exactly when Retune existed. Retune could do nothing but
// re-send the frequency the radio was already on (issue #226), and moving the receiver meant
// stopping it first (issue #227). One defect, seen from two sides, and it shipped green because
// `"tune"` had no test in this file at all.
//
// And a control that follows the radio has to be SEEDED from the radio: the box takes its value from
// the read-back, so a reload, a front-panel `F+0` or a second browser cannot leave a stale 104.3 in
// a field whose button will happily send it.

import { useEffect, useRef, useState } from "react";
import { useAction } from "../actions.js";

// 0 is the broadcast band nearly every host wants; the others exist because the wire carries them
// and a two-bit field would clamp a bad value in silence (ADR 0156). The band's own frequency
// LIMITS are deliberately not repeated here — they live in the BK1080 driver and the radio is what
// refuses, so these are what the protocol calls the bands, not a validation table this can drift.
const BANDS = [
  { value: 0, label: "87.5 – 108 MHz" },
  { value: 1, label: "76 – 108 MHz" },
  { value: 2, label: "76 – 90 MHz" },
  { value: 3, label: "64 – 76 MHz" },
];

// How long the radio's own answer stays on screen. It is the answer to ONE action rather than a
// status line — the line above it is the live truth — so it must not outlive its action by much.
const APPLIED_TIMEOUT_MS = 5000;

const fmtMHz = (hz) => (hz ? `${(hz / 1e6).toFixed(1)} MHz` : "an unreported frequency");

export default function BroadcastFmPanel({
  client,
  broadcastFm,
  hasCap,
  onAuthError,
  onUnsupported,
}) {
  const canSet = hasCap("set_broadcast_fm");
  const canClear = hasCap("clear_broadcast_fm");
  const [mhz, setMhz] = useState("104.3");
  const [band, setBand] = useState(0);
  const [applied, setApplied] = useState(null);
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  // `=== true`, never truthy: `null` is "this server has never learned whether the second receiver
  // is running", which is every pre-F8 radio and every backend with no dock tuner. Rendering that
  // as OFF is how a deaf station gets trusted (ADR 0157).
  const on = broadcastFm?.on === true;
  const known = broadcastFm != null;
  const reportedHz = broadcastFm?.hz ?? null;
  const reportedBand = broadcastFm?.band ?? null;

  // Follow the radio, not the last thing that was typed.
  //
  // Keyed on the READING rather than run on every render, and that is the whole subtlety: `status`
  // frames arrive continuously and most of them do not move the receiver (`rescues` ticking, an
  // on/off transition with the same frequency), so syncing unconditionally would erase a
  // half-typed frequency under the operator's cursor. Syncing only when the radio's own reading
  // changes leaves typing alone and still picks up a front-panel `F+0` or another browser's tune.
  //
  // `hz == null` is the OFF block's shape, not a reading — the box keeps whatever it holds.
  // `toFixed(1)` is exact here: the raster is 100 kHz (`frames.py` BROADCAST_FM_RASTER_HZ).
  const synced = useRef(null);
  useEffect(() => {
    if (reportedHz == null) return;
    const reading = `${reportedHz}:${reportedBand}`;
    if (synced.current === reading) return;
    synced.current = reading;
    setMhz((reportedHz / 1e6).toFixed(1));
    if (reportedBand != null) setBand(reportedBand);
  }, [reportedHz, reportedBand]);

  useEffect(() => {
    if (!applied) return undefined;
    const t = setTimeout(() => setApplied(null), APPLIED_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [applied]);

  // An unusable control is just noise (ControlPanel's rule). A radio that has earned neither
  // capability has no second receiver this server can see at all.
  if (!canSet && !canClear) return null;

  // The host validates only that a number was entered. The 100 kHz raster and the band's own Hz
  // limits stay the server's and the radio's verdicts (guardrail 1) — a second copy of
  // `BK1080_GetFreqLoLimit` here is the drift hazard `dock.h` refuses for the same reason. What
  // this catches is the empty box, which serialises `hz: null` and earns a 422 about the wrong
  // thing.
  const asked = Number.parseFloat(mhz);
  const askable = Number.isFinite(asked) && asked > 0;

  const send = async (body) => {
    setApplied(null);
    const result = await run(() => client.broadcastFm(body));
    if (result) setApplied(result.broadcast_fm ?? null);
    return result;
  };

  // ON and TUNE are the same request with a different verb, and the radio decides which is legal:
  // TUNE on a receiver that is off is refused 409 (`ERR_OFF`, "TUNE is not a cheaper ON"), so this
  // reads `on` to pick the verb rather than to decide what to render.
  const apply = () => {
    if (!askable) return;
    send({ action: on ? "tune" : "on", hz: Math.round(asked * 1e6), band });
  };

  return (
    <div className="card">
      <h2>Second receiver</h2>

      <p className="tune-row">
        <span className="muted">Broadcast FM</span>
        {!known ? (
          <span className="status-value warn">has not been asked</span>
        ) : on ? (
          <span className="status-value warn">ON — {fmtMHz(broadcastFm.hz)}</span>
        ) : (
          <span className="status-value">off</span>
        )}
      </p>

      {canSet && (
        <>
          <div className="tune-row">
            <label htmlFor="bfm-hz">Frequency (MHz)</label>
            <input
              id="bfm-hz"
              type="number"
              step="0.1"
              value={mhz}
              onChange={(e) => setMhz(e.target.value)}
              // Enter is what a number field invites, and the alternative is an operator typing a
              // frequency, pressing Enter, and watching nothing happen — the shape of #226.
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                e.preventDefault();
                apply();
              }}
            />
          </div>
          <div className="tune-row">
            <label htmlFor="bfm-band">Band</label>
            <select
              id="bfm-band"
              value={band}
              onChange={(e) => setBand(Number(e.target.value))}
            >
              {BANDS.map((b) => (
                <option key={b.value} value={b.value}>
                  {b.label}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {/* `.btn-row` (flex), not `.tune-row` (a 96px/1fr/auto/auto grid meant for label+field rows):
          a lone button dropped into that grid lands in the 96px label column and wraps to three
          lines, which is what the ON button did for the whole of ADR 0164. */}
      {(on || canSet) && (
        <div className="btn-row">
          {on && (
            // Leaving broadcast FM is always safe — it gives the station its ears back and un-mutes
            // both links — and arming a button in front of the remedy is how ADR 0161 finding 2
            // stayed open for four cycles.
            <button
              type="button"
              disabled={pending || !canClear}
              onClick={() => send({ action: "off" })}
            >
              Turn it off
            </button>
          )}
          {canSet && (
            <button type="button" disabled={pending || !askable} onClick={apply}>
              {on ? "Retune" : "Turn on broadcast FM"}
            </button>
          )}
        </div>
      )}

      {/* The radio's own read-back, never an echo of the request (ADR 0156) — which is exactly why
          it is here. A retune to the frequency the receiver is ALREADY on changes nothing about the
          line above, and without this the operator gets a disabled flicker and no answer: #226's
          symptom, surviving the fix that removed its cause. */}
      {applied && (
        <p className="muted" role="status">
          The radio reports{" "}
          {applied.on ? `broadcast FM on at ${fmtMHz(applied.hz)}` : "broadcast FM off"}.
        </p>
      )}

      {/* The server's own sentence, not one composed here: a 422 names the frequency that was
          refused and whether the raster or the radio's own band limits refused it, and a 409 says
          which condition the radio is in. Either is more use than "request failed". */}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
