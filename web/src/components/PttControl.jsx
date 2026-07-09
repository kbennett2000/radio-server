// PTT toggle (ADR 0022): POST /ptt {on}. The button reflects live `transmitting` (from /events),
// not local optimism — the key state is authoritative from the server.

import { useAction } from "../actions.js";

export default function PttControl({ client, transmitting, onAuthError, onUnsupported }) {
  const { run, pending, error } = useAction({ onAuthError, onUnsupported });
  const toggle = () => run(() => client.ptt(!transmitting));

  return (
    <div className="card">
      <h2>PTT</h2>
      <button
        type="button"
        className={`ptt ${transmitting ? "keyed" : ""}`}
        onClick={toggle}
        disabled={pending}
      >
        {transmitting ? "On air — key down" : "Key up (transmit)"}
      </button>
      {error && <div className="error" role="alert">{error}</div>}
    </div>
  );
}
