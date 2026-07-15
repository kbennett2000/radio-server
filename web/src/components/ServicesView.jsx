// Services panel: list the DTMF voice services / built-in commands wired in this deployment
// (GET /services) and let the control operator fire one over the air by digit (POST /services/{digit}).
//
// Transmitting is authorized by the LAN token alone — no over-the-air DTMF login — exactly like the
// Talk/PTT buttons. Each Transmit runs through the shared useAction hook (disable-while-pending +
// Unauthorized -> gate). The resulting command/session/id events surface in the live event log.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";
import { Unauthorized } from "../api.js";

export default function ServicesView({ client, onAuthError }) {
  const [services, setServices] = useState(null); // null = loading, [] = none
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    let live = true;
    client
      .services()
      .then((list) => live && setServices(list))
      .catch((e) => {
        if (e instanceof Unauthorized) return onAuthError?.();
        if (live) setLoadError(e.message);
      });
    return () => {
      live = false;
    };
  }, [client, onAuthError]);

  return (
    <div className="card">
      <h2>Services</h2>
      <p className="muted services-hint">Transmits over the air immediately.</p>
      {loadError && <div className="error" role="alert">{loadError}</div>}
      {services === null && !loadError && <div className="muted">Loading…</div>}
      {services !== null && services.length === 0 && (
        <div className="muted">No services (controller not configured).</div>
      )}
      {services && services.length > 0 && (
        <ul className="services">
          {services.map((s) => (
            <ServiceRow key={s.digit} service={s} client={client} onAuthError={onAuthError} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ServiceRow({ service, client, onAuthError }) {
  const { run, pending, error } = useAction({ onAuthError });
  const transmit = () => run(() => client.triggerService(service.digit));

  return (
    <li className="service-row">
      <div className="service-info">
        <span className="service-digit">{service.digit}#</span>
        <span className="service-name">{service.name}</span>
        {service.description && <span className="service-desc">{service.description}</span>}
      </div>
      <button type="button" onClick={transmit} disabled={pending}>
        {pending ? "Transmitting…" : "Transmit"}
      </button>
      {error && <div className="error" role="alert">{error}</div>}
    </li>
  );
}
