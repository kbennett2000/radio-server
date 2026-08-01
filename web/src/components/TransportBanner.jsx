// The dead-serial-reader banner (ADR 0166).
//
// The reader thread that carries every word between this server and the radio dies permanently on
// any read error — a USB re-enumeration (hourly on the bench box), a cable event, a second process
// opening the tty. Nothing restarted it and nothing watched it, and because every other field the
// server reports is served from cached state, the whole UI went on looking normal: a frequency, a
// mode, a ready transmitter. ADR 0163 found it by accident.
//
// THE DESIGN RULE HERE: this is the one condition where everything else on the page is a lie. So it
// is not a chip beside the frequency and not a line in the log — it sits across the top, above the
// controls whose readings it invalidates, and it stays until the fault is actually gone.
//
// It is deliberately NOT rendered for `transport === null`. That means "this backend has no serial
// link to report on" — the mock, an audio-only radio — and a station that cannot have this fault
// must not be decorated with a reassurance about it either. Absence of a fault is not a status
// worth a banner.

import { useAction } from "../actions.js";

export default function TransportBanner({ client, transport, onAuthError, onUnsupported }) {
  const { run, status, error } = useAction({ client, onAuthError, onUnsupported });

  // `undefined` on a server too old to send the field, `null` where there is no serial link. Both
  // are "nothing to say", and neither is a fault — the tri-state rule this project keeps to.
  if (transport == null || transport.alive !== false) return null;

  return (
    <div className="notice notice-rx-paused transport-banner" role="alert">
      <strong>The radio has stopped answering.</strong> The serial link{" "}
      {transport.port ? <code>{transport.port}</code> : null} is no longer being read, so everything
      else on this page — frequency, mode, whether the transmitter is ready — is the last thing this
      server knew and not what the radio is doing now.
      {transport.error ? (
        <div className="transport-banner-detail">
          <code>{transport.error}</code>
        </div>
      ) : null}
      <div className="transport-banner-actions">
        <button
          type="button"
          className="btn"
          disabled={status === "busy"}
          onClick={() => run(() => client.reconnectTransport())}
        >
          {status === "busy" ? "Reopening…" : "Reopen the link"}
        </button>
        <span className="transport-banner-hint">
          One attempt, not a retry loop — if another program has the port, it says so rather than
          fighting for it.
        </span>
      </div>
      {error ? <div className="error" role="alert">{error}</div> : null}
    </div>
  );
}
