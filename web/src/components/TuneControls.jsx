// Tuning controls (ADR 0022) — the UI face of guardrail 3. Frequency / channel / tone / bandwidth /
// demodulation each POST their endpoint and are greyed when the backend doesn't advertise the
// capability (or a 501 revealed it at runtime), so there is never a dead button that silently no-ops.
//
// The two selects are named apart on purpose (ADR 0154). `POST /mode` is wide/narrow BANDWIDTH and
// `POST /modulation` is the DEMODULATOR, they are different radio settings reached by different
// frames, and both spell one of their values "FM" — the confusion Capability.SET_MODE and
// SET_MODULATION were split for, arriving where an operator can act on it. So this card says
// "Bandwidth" and "Demodulation"; neither says "Mode", because a label that survived with its
// meaning changed is what puts muscle memory on the wrong control. The "(FM)"/"(NFM)" parentheticals
// are the only thing joining these words back to the raw values, which still appear in radio.toml,
// in preset records, and on the face LCD.
//
// The notice used to read "Not supported on this radio (audio-only backend)" — true, and useless.
// It described what the UI could not do and said nothing about what the radio WOULD do. Since
// ADR 0139 this is the repeater path, not a fallback: the operator picks the channel on the radio
// and Talk transmits there. Nothing in this repo can read a front panel back, so the one fact an
// operator needs before pressing Talk — you are about to transmit on a frequency this screen cannot
// show you — has to be said out loud here.

import { useState } from "react";
import { useAction } from "../actions.js";

// Two, because two is all `POST /mode` has ever accepted. This list used to carry AM/USB/LSB/CW as
// well, which every real backend rejects with a ValueError — `presets.VALID_MODES` is exactly
// {"FM", "NFM"} — so four of the six options could only ever fail. AM was the dangerous one: it read
// as a demodulator, in the control that is not the demodulator.
const BANDWIDTHS = [
  { value: "FM", label: "Wide (FM)" },
  { value: "NFM", label: "Narrow (NFM)" },
];

// The demodulator (ADR 0150). USB is deliberately absent: the wire reserves its number but no radio
// here has been proven on it, and the API 422s it.
const MODULATIONS = ["FM", "AM"];

// Three, because three is what the radio's dock command accepts (ADR 0146). Deliberately NOT
// labelled with watts: the radio computes the level per band from calibration in its own flash that
// the server cannot read, so a number here would be one this project made up.
const POWER_LEVELS = ["low", "mid", "high"];

export default function TuneControls({ client, state, hasCap, catAvailable, onAuthError, onUnsupported }) {
  const hooks = { onAuthError, onUnsupported };

  return (
    <div className="card">
      <h2>Tune</h2>
      {!catAvailable && (
        <div className="notice">
          This radio holds its own channel. Talk transmits on whatever its front panel is set
          to — the server cannot read it back or change it.
        </div>
      )}
      <FreqControl client={client} disabled={!hasCap("set_frequency")} {...hooks} />
      <ChannelControl client={client} disabled={!hasCap("set_channel")} {...hooks} />
      <ToneControl client={client} disabled={!hasCap("set_tone")} {...hooks} />
      <ModeControl client={client} disabled={!hasCap("set_mode")} {...hooks} />
      <ModulationControl
        client={client}
        modulation={state?.modulation ?? null}
        disabled={!hasCap("set_modulation")}
        {...hooks}
      />
      {hasCap("set_power") && (
        <PowerControl client={client} level={state?.power ?? null} {...hooks} />
      )}
    </div>
  );
}

function FreqControl({ client, disabled, onAuthError, onUnsupported }) {
  const [mhz, setMhz] = useState("146.520");
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const submit = (e) => {
    e.preventDefault();
    const hz = Math.round(parseFloat(mhz) * 1e6);
    if (Number.isFinite(hz)) run(() => client.frequency(hz));
  };
  return (
    <form className="tune-row" onSubmit={submit}>
      <label>Frequency (MHz)</label>
      <input type="number" step="0.0001" value={mhz} disabled={disabled}
        onChange={(e) => setMhz(e.target.value)} />
      <button type="submit" disabled={disabled || pending}>Set</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

function ChannelControl({ client, disabled, onAuthError, onUnsupported }) {
  const [n, setN] = useState("0");
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const submit = (e) => {
    e.preventDefault();
    const v = parseInt(n, 10);
    if (Number.isInteger(v)) run(() => client.channel(v));
  };
  return (
    <form className="tune-row" onSubmit={submit}>
      <label>Channel</label>
      <input type="number" step="1" value={n} disabled={disabled}
        onChange={(e) => setN(e.target.value)} />
      <button type="submit" disabled={disabled || pending}>Set</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

function ToneControl({ client, disabled, onAuthError, onUnsupported }) {
  const [tone, setTone] = useState("100.0");
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const set = (e) => {
    e.preventDefault();
    const v = parseFloat(tone);
    if (Number.isFinite(v)) run(() => client.tone(v));
  };
  const clear = () => run(() => client.tone(null)); // null clears the tone
  return (
    <form className="tune-row" onSubmit={set}>
      <label>Tone (Hz)</label>
      <input type="number" step="0.1" value={tone} disabled={disabled}
        onChange={(e) => setTone(e.target.value)} />
      <button type="submit" disabled={disabled || pending}>Set</button>
      <button type="button" onClick={clear} disabled={disabled || pending}>Clear</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

// Channel BANDWIDTH — how much spectrum the radio listens across. The option text is prose, the
// posted value is still "FM"/"NFM": presets and `radio.toml` speak the raw vocabulary and
// PresetControl's active-channel highlight compares against it, so a relabel that reached the wire
// would silently break the highlight.
//
// `aria-label` because the <label> beside it is a bare element with no htmlFor — that is the
// pre-existing idiom in this card, and with two selects in one card it leaves neither of them
// reachable by name.
function ModeControl({ client, disabled, onAuthError, onUnsupported }) {
  const [mode, setMode] = useState("FM");
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const submit = (e) => {
    e.preventDefault();
    run(() => client.mode(mode));
  };
  return (
    <form className="tune-row" onSubmit={submit}>
      <label>Bandwidth</label>
      <select
        aria-label="Bandwidth"
        value={mode}
        disabled={disabled}
        onChange={(e) => setMode(e.target.value)}
      >
        {BANDWIDTHS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
      </select>
      <button type="submit" disabled={disabled || pending}>Set</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

// The DEMODULATOR (ADR 0150/0154) — what kind of signal the radio expects to find. AM is what makes
// airband receivable at all, and on this firmware it also stops the station transmitting.
//
// No "Set" button and no draft state, unlike every other row here, and the reason is not tidiness.
// `apply_preset` writes the modulation on EVERY preset apply — unconditionally, because a channel
// list with one airband entry has to return to FM when the operator taps a repeater. A local draft
// would sit here showing the operator's last pick while the radio had been moved out from under it.
// So the select renders what the radio CONFIRMED and the change is the intent, which is exactly the
// rule PowerControl states below for the same reason. Nothing is claimed before the server has
// asserted a modulation: `null` is "not known", and the firmware seeding FM is not this server's
// knowledge.
//
// Greyed rather than hidden where the backend cannot do it — the opposite of PowerControl's choice,
// deliberately. An unusable power control is just noise, but this one is the remedy named by the
// Talk button's AM lockout, and hiding a remedy while showing the symptom is the silent-failure
// shape this project keeps closing.
function ModulationControl({ client, modulation, disabled, onAuthError, onUnsupported }) {
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  return (
    <div className="tune-row">
      <label>Demodulation</label>
      <select
        aria-label="Demodulation"
        value={modulation ?? ""}
        disabled={disabled || pending}
        onChange={(e) => run(() => client.modulation(e.target.value))}
      >
        {modulation == null && <option value="">—</option>}
        {MODULATIONS.map((m) => <option key={m} value={m}>{m}</option>)}
      </select>
      {error && <span className="error">{error}</span>}
    </div>
  );
}

// Transmit power (ADR 0146). Hidden rather than greyed when the backend cannot set it, matching how
// ControlPanel hides the whole card on an audio-only radio — an unusable control is just noise.
//
// Unlike its siblings this has no "Set" button and no local draft state: a power level is one of
// three, so a click IS the intent, and the state shown is `status.power` — which is what the RADIO
// read back, not what was asked for. Nothing is highlighted before the first tune, because until
// then the radio is on whatever its front panel says and the server genuinely cannot see it.
function PowerControl({ client, level, onAuthError, onUnsupported }) {
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  return (
    <div className="tune-row">
      <label>Power</label>
      <div className="btn-row power-row" role="group" aria-label="Transmit power">
        {POWER_LEVELS.map((l) => (
          <button
            key={l}
            type="button"
            className={`power-btn${l === level ? " active" : ""}`}
            aria-pressed={l === level}
            disabled={pending}
            onClick={() => run(() => client.power(l))}
          >
            {l}
          </button>
        ))}
      </div>
      {error && <span className="error">{error}</span>}
    </div>
  );
}
