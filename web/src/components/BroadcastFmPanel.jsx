// The SECOND RECEIVER card (ADR 0164) — the first control in this arc that can make the station
// worse on purpose, and the first host-side way back out of broadcast FM (ADR 0158 R4 / 0160
// finding 3 / 0161 finding 2, open for four cycles).
//
// The UV-K5 carries a BK1080 commercial-FM chip alongside the BK4819 everything else drives. While
// it runs it holds the speaker line the AIOC listens on, so between overs the station hears a
// broadcast station and not its own channel. Turning that on deliberately is a legitimate thing to
// want; doing it without being told what it costs is not.
//
// THE DESIGN RULE HERE: nothing is hidden. The consequences sit in the card at all times rather
// than behind a disclosure or a modal — an operator deciding whether to press the button should not
// have to press it to find out what it does. The button itself is the two-step arm/confirm
// `RestartButton` already uses (SettingsView.jsx), because one stray click should not silence both
// links.
//
// And the consequences are split by WHO NEEDS THEM. Numbers 1-3 are for the person about to commit,
// so they sit next to the button. The repurposed-keypad warning is for whoever walks up to the radio
// later and never saw the confirm, so it appears only while broadcast FM is ACTIVE.

import { useEffect, useState } from "react";
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

const ARM_TIMEOUT_MS = 8000;

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
  const [armed, setArmed] = useState(false);
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  // Self-disarming, like RestartButton: an armed button left on screen is a trap for the next
  // person to walk past the machine.
  useEffect(() => {
    if (!armed) return undefined;
    const t = setTimeout(() => setArmed(false), ARM_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [armed]);

  // An unusable control is just noise (ControlPanel's rule), and this one would be noise with a
  // frightening warning attached. A radio that has earned neither capability has no second receiver
  // this server can see at all.
  if (!canSet && !canClear) return null;

  // `=== true`, never truthy: `null` is "this server has never learned whether the second receiver
  // is running", which is every pre-F8 radio and every backend with no dock tuner. Rendering that
  // as OFF is how a deaf station gets trusted (ADR 0157).
  const on = broadcastFm?.on === true;
  const known = broadcastFm != null;

  const send = (body) => run(() => client.broadcastFm(body));
  const asked = () => ({ hz: Math.round(parseFloat(mhz) * 1e6), band });

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

      {on && (
        // Consequence 4 (ADR 0160 item 6, photographed on the bench). Here rather than in the
        // pre-commit notice because the person it protects is whoever walks up to the radio later.
        //
        // It says what was MEASURED and no more. Four screens were photographed: digits going into
        // direct frequency entry on the broadcast band (`147.-` typed under an `87.5-108M` band
        // line), and `M` opening a `CH-01` / `SAVE?` prompt. Whether confirming that prompt
        // overwrites a stored channel was never measured, so this does not claim it (guardrail 1).
        <div className="notice notice-rx-paused" role="alert">
          <strong>The radio&apos;s keypad is not locked — it is repurposed.</strong> Digits now type
          a frequency into the broadcast receiver rather than moving the station, and{" "}
          <strong>M</strong> opens a save-to-channel prompt. Someone at the radio who does not know
          it is in broadcast FM will believe they are tuning the station.
        </div>
      )}

      {canSet && !on && (
        <>
          <div className="tune-row">
            <label htmlFor="bfm-hz">Frequency (MHz)</label>
            <input
              id="bfm-hz"
              type="number"
              step="0.1"
              value={mhz}
              onChange={(e) => setMhz(e.target.value)}
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

          {/* Consequences 1-3, always visible. Not a disclosure and not a modal: a control that
              hides what it does is worse than no control.

              The middle one is worded to the measurement. ADR 0163's M3 keyed a witness on the
              station's own channel with broadcast FM selected and recovered its 1000 Hz tone at
              power 0.995 — the radio HEARS real overs; the links are muted anyway because the probe
              reads "FM is selected" and cannot tell the difference. "The station is deaf" is the
              thing M3 disproved, and it must not appear here.

              The third is not "you cannot transmit". `_clear_if_deafened` runs first in `_key_on`,
              so by the time PTT asserts the F9 interlock has nothing left to interlock — the over
              goes out and takes the receiver back with it. */}
          <div className="notice" role="note" aria-label="Turning this on">
            <strong>Turning this on:</strong>
            <ul>
              <li>
                Both links go silent — nothing this radio hears reaches Mumble or the reflector.
              </li>
              <li>
                Real overs on this station&apos;s own channel are withheld from the links too,{" "}
                <strong>even though the radio hears them</strong>.
              </li>
              <li>
                Any transmission — Talk, a service, the automatic station ID — turns it back off.
              </li>
            </ul>
          </div>

          <div className="tune-row">
            <button
              type="button"
              className={`restart-btn${armed ? " confirm" : ""}`}
              disabled={pending}
              onClick={() => {
                if (!armed) {
                  setArmed(true);
                  return;
                }
                setArmed(false);
                send({ action: "on", ...asked() });
              }}
            >
              {armed ? "Confirm — turn it on" : "Turn on broadcast FM"}
            </button>
            {armed && (
              <button type="button" onClick={() => setArmed(false)}>
                Cancel
              </button>
            )}
          </div>
        </>
      )}

      {on && (
        <div className="tune-row">
          {/* No confirm step. Leaving broadcast FM is always safe — it gives the station its ears
              back and un-mutes both links — and arming a button in front of the remedy is how ADR
              0161 finding 2 stayed open for four cycles. */}
          <button
            type="button"
            disabled={pending || !canClear}
            onClick={() => send({ action: "off" })}
          >
            Turn it off
          </button>
          {canSet && (
            <button
              type="button"
              disabled={pending}
              onClick={() => send({ action: "tune", ...asked() })}
            >
              Retune
            </button>
          )}
        </div>
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
