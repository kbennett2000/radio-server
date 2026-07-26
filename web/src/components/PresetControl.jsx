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
//
// ADR 0145 adds the "Save to radio" switch, on the same seam: `tune_persist` rides the status
// event, so it is server state every browser agrees on rather than a per-tab preference.

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
    if (hasCap("set_tone") && !nullableEqual(state.tone, p.tx_tone)) return false;
    // The TX leg is part of the channel's identity: two entries can share an output frequency and
    // differ only in where they transmit, and a stale split must not keep a channel highlighted.
    if (hasCap("set_split") && !nullableEqual(state.tx_frequency, p.tx_frequency)) return false;
    return true;
  });
  return matches.length === 1 ? matches[0].name : null;
}

// Equality treating null/undefined (unset) alike; both sides come from the same JSON numbers.
function nullableEqual(a, b) {
  if (a == null && b == null) return true;
  return a === b;
}

// The skipped fields worth telling the operator about: the ones the ACTIVE backend couldn't honour.
//
// `skipped` mixes two unlike things behind one shape. A capability gap ("this radio has no split")
// is per-backend, changes when you switch radios, and is genuinely news. A field nothing implements
// on any backend — `rx_tone` (ADR 0133) — is not: it fires on every apply of every channel that
// carries one, it cannot be acted on, and the sentence it produces says "this radio" when it means
// "this software, on every radio". Repeating an unactionable warning in the same amber box as the
// actionable ones is how the actionable ones stop being read.
//
// The API already distinguishes them and always did: a capability gap carries the `Capability`
// string the UI greys controls by, and an unhonoured field carries `capability: ""` with a `reason`.
// So filter on that rather than on a list of field names here, which would drift.
// Nothing is hidden — GET /presets still reports it per channel, and so do the config docs.
export function blockingSkips(skipped) {
  return (skipped ?? []).filter((s) => s.capability);
}

// Human label for a skipped field: name the capability the active backend couldn't honour.
function skipLabel(skipped) {
  const fields = skipped.map((s) => s.field).join(", ");
  return `Applied — ${fields} not supported on this radio.`;
}

// "145.4600" for a simplex channel; "145.4600 −0.600" for a repeater. The offset is what a ham
// reads a repeater by, and with 40 imported channels the callsign-shaped names are not enough to
// navigate by on their own (ADR 0133).
function presetSubLabel(p) {
  const mhz = (p.frequency / 1e6).toFixed(4);
  if (p.offset == null) return mhz;
  const sign = p.offset < 0 ? "−" : "+";
  return `${mhz} ${sign}${(Math.abs(p.offset) / 1e6).toFixed(3)}`;
}

// Free-text filter over name and frequency, so "145.4" and "w0cra" both narrow the list.
function matchesFilter(p, needle) {
  if (!needle) return true;
  const hay = `${p.name} ${(p.frequency / 1e6).toFixed(4)}`.toLowerCase();
  return hay.includes(needle.toLowerCase());
}

// Above this many channels the row stops being scannable by eye and needs a filter box. An imported
// repeater list runs to dozens; the hand-written bench list is three.
const FILTER_THRESHOLD = 12;

// What the storage switch costs either way, said in the operator's terms rather than the firmware's
// (ADR 0145). Both states are a real trade, so neither reads as the "safe" one — the six seconds
// are what persistence costs, and forgetting is what instant costs.
const PERSIST_HELP = {
  false: "Tuning is instant; the radio forgets the channel when you switch it off.",
  true: "The radio keeps the channel when switched off, and needs about 6 s before it will transmit.",
};

export default function PresetControl({ client, state, hasCap, onAuthError, onUnsupported }) {
  const [presets, setPresets] = useState([]);
  const [skipped, setSkipped] = useState([]);
  const [filter, setFilter] = useState("");
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
      const blocking = blockingSkips(res?.skipped);
      if (blocking.length) setSkipped(blocking);
    });

  // Storing is a live choice on the UV-K5's hybrid tuner and does not exist anywhere else, so the
  // switch renders off `tune_persist != null` — "no such switch" and "the switch is off" are
  // different answers and the status carries both. Optimistic local echo would be wrong here: the
  // server refuses mid-TX and the write can fail, so the state shown is the one it reports back.
  const persist = state?.tune_persist ?? null;
  const setPersist = (on) => run(() => client.tunePersist(on));

  const shown = presets.filter((p) => matchesFilter(p, filter));

  return (
    <div className="card">
      <div className="log-head">
        <h2>Channels</h2>
        {persist !== null && (
          <label className="persist-toggle" title={PERSIST_HELP[String(persist)]}>
            <input
              type="checkbox"
              checked={persist}
              disabled={pending}
              onChange={(e) => setPersist(e.target.checked)}
            />
            <span>Save to radio</span>
          </label>
        )}
        {presets.length > FILTER_THRESHOLD && (
          <input
            type="search"
            className="preset-filter"
            placeholder={`Filter ${presets.length} channels…`}
            aria-label="Filter channels"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        )}
      </div>

      <div className="btn-row preset-row" style={{ flexWrap: "wrap" }}>
        {shown.map((p) => {
          const isActive = p.name === active;
          const txLeg =
            p.tx_frequency != null ? ` · TX ${(p.tx_frequency / 1e6).toFixed(4)} MHz` : "";
          return (
            <button
              type="button"
              key={p.name}
              className={`preset-btn${isActive ? " active" : ""}`}
              aria-pressed={isActive}
              onClick={() => apply(p.name)}
              disabled={pending}
              title={`Tune to ${(p.frequency / 1e6).toFixed(4)} MHz${txLeg}${
                p.mode ? ` ${p.mode}` : ""
              }${p.tx_tone != null ? ` · ${p.tx_tone} Hz` : ""}`}
            >
              <span className="preset-name">{p.name}</span>
              <span className="preset-freq">{presetSubLabel(p)}</span>
            </button>
          );
        })}
        {shown.length === 0 && <p className="muted">No channel matches “{filter}”.</p>}
      </div>

      <p className="muted">
        Tap a channel to tune. Edit channels in the settings file ([[presets]]).
        {persist !== null && ` ${PERSIST_HELP[String(persist)]}`}
      </p>

      {skipped.length > 0 && <div className="notice">{skipLabel(skipped)}</div>}

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
