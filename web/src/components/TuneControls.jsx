// Tuning controls (ADR 0022) — the UI face of guardrail 3. Frequency / channel / tone / mode each
// POST their endpoint and are greyed when the backend doesn't advertise the capability (or a 501
// revealed it at runtime), so there is never a dead button that silently no-ops.
//
// The notice used to read "Not supported on this radio (audio-only backend)" — true, and useless.
// It described what the UI could not do and said nothing about what the radio WOULD do. Since
// ADR 0139 this is the repeater path, not a fallback: the operator picks the channel on the radio
// and Talk transmits there. Nothing in this repo can read a front panel back, so the one fact an
// operator needs before pressing Talk — you are about to transmit on a frequency this screen cannot
// show you — has to be said out loud here.

import { useState } from "react";
import { useAction } from "../actions.js";

const MODES = ["FM", "NFM", "AM", "USB", "LSB", "CW"];

export default function TuneControls({ client, hasCap, catAvailable, onAuthError, onUnsupported }) {
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

function ModeControl({ client, disabled, onAuthError, onUnsupported }) {
  const [mode, setMode] = useState("FM");
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const submit = (e) => {
    e.preventDefault();
    run(() => client.mode(mode));
  };
  return (
    <form className="tune-row" onSubmit={submit}>
      <label>Mode</label>
      <select value={mode} disabled={disabled} onChange={(e) => setMode(e.target.value)}>
        {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
      </select>
      <button type="submit" disabled={disabled || pending}>Set</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}
