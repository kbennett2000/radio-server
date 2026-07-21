// The channel-presets card (ADR 0116): tap a named channel and the radio tunes to its
// {frequency, tone?, mode} through the CAT surface (server-side POST /presets/apply, ADR 0115).
//
// Patterned on DStarPanel: a standalone card with a tap-a-preset button row, self-hiding when there
// is nothing to show, errors surfaced as a role="alert". Two hide gates, matching existing precedent
// (no third state):
//   - the SET_FREQUENCY capability gate is the ControlPanel mount predicate ({hasCap("set_frequency")
//     && <PresetControl/>}) — the same hide-by-not-mounting model as showDial/ScanControl;
//   - config-absence (no [[presets]] configured) self-hides here (return null on an empty list), like
//     DStarPanel/LinkPanel/DvapPanel.
//
// The active-channel highlight is DERIVED from live status, never stored: a preset is "active" when
// its honoured fields exactly match state.frequency/tone/mode. Applying publishes a status event
// server-side (ADR 0076/0077), so the highlight updates in every connected browser with no polling
// and no client-side store; a manual tune-away changes the status and clears it naturally.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

// The name of the single preset whose honoured fields match the current radio state, or null.
// Frequency is always required (the card only mounts when set_frequency is honoured); tone/mode are
// compared only when the backend advertises them. Exactly one match highlights; zero or an ambiguous
// two-plus (e.g. tone-less duplicates of a frequency) → no highlight.
export function activePresetName(presets, state, hasCap) {
  if (state?.frequency == null) return null;
  const matches = presets.filter((p) => {
    if (state.frequency !== p.frequency) return false;
    if (hasCap("set_mode") && (state.mode ?? null) !== (p.mode ?? null)) return false;
    if (hasCap("set_tone") && !toneEqual(state.tone, p.tone)) return false;
    return true;
  });
  return matches.length === 1 ? matches[0].name : null;
}

// Tone equality treating null/undefined (no tone) alike; both sides come from the same JSON numbers.
function toneEqual(a, b) {
  if (a == null && b == null) return true;
  return a === b;
}

// Human label for a skipped field: name the capability the active backend couldn't honour.
function skipLabel(skipped) {
  const fields = skipped.map((s) => s.field).join(", ");
  return `Applied — ${fields} not supported on this radio.`;
}

export default function PresetControl({ client, state, hasCap, onAuthError, onUnsupported }) {
  const [presets, setPresets] = useState([]);
  const [skipped, setSkipped] = useState([]);
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });

  // Fetch the configured presets once on mount — the list is static config (WS status frames carry
  // no preset list). A non-fatal failure just leaves the card hidden.
  useEffect(() => {
    let live = true;
    client
      .presets()
      .then((body) => {
        if (live && Array.isArray(body?.presets)) setPresets(body.presets);
      })
      .catch(() => {
        /* non-fatal: no card rather than a broken one */
      });
    return () => {
      live = false;
    };
  }, [client]);

  // Config-absence self-hide (like DStarPanel). The SET_FREQUENCY gate is the ControlPanel mount.
  if (presets.length === 0) return null;

  const active = activePresetName(presets, state, hasCap);

  const apply = (name) =>
    run(async () => {
      setSkipped([]);
      const res = await client.applyPreset(name);
      if (res?.skipped?.length) setSkipped(res.skipped);
    });

  return (
    <div className="card">
      <div className="log-head">
        <h2>Channels</h2>
      </div>

      <div className="btn-row" style={{ flexWrap: "wrap" }}>
        {presets.map((p) => {
          const isActive = p.name === active;
          return (
            <button
              type="button"
              key={p.name}
              className={`preset-btn${isActive ? " active" : ""}`}
              aria-pressed={isActive}
              onClick={() => apply(p.name)}
              disabled={pending}
              title={`Tune to ${(p.frequency / 1e6).toFixed(4)} MHz${p.mode ? ` ${p.mode}` : ""}${
                p.tone != null ? ` · ${p.tone} Hz` : ""
              }`}
            >
              {p.name}
            </button>
          );
        })}
      </div>

      <p className="muted">Tap a channel to tune. Edit channels in the settings file ([[presets]]).</p>

      {skipped.length > 0 && <div className="notice">{skipLabel(skipped)}</div>}

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
