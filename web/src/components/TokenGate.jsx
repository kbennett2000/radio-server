// The token gate (ADR 0022): prompt for the LAN API token, validate it by fetching /capabilities,
// and lift {token, caps} to App on success. A bad token yields a clear inline error — never a
// silent hang. The token is held only in this form's local state until it is handed up.
//
// The look (ADR 0044): the banner's handheld-radio illustration over the wordmark, with faint
// concentric rings and a dial-tick strip as fixed page decor. Both decor layers are aria-hidden —
// the form is the only content here.

import { useState } from "react";
import { makeClient, Unauthorized } from "../api.js";

// The friendly handheld radio from docs/banner.html, cropped to the body and recolored to theme
// tokens so it reads in both Day and Night.
function RadioMark() {
  return (
    <svg
      className="gate-radio"
      width="108"
      height="104"
      viewBox="88 20 170 264"
      aria-hidden="true"
    >
      <g fill="none" stroke="var(--ink)" strokeWidth="7" strokeLinecap="round" opacity="0.9">
        <path d="M196 44 a40 40 0 0 1 40 -18" />
        <path d="M204 30 a72 72 0 0 1 72 -20" />
      </g>
      <rect x="170" y="40" width="12" height="86" rx="6" fill="var(--ink)" />
      <circle cx="176" cy="40" r="9" fill="var(--red)" />
      <rect x="96" y="112" width="150" height="168" rx="22" fill="var(--ink)" />
      <rect x="108" y="124" width="126" height="144" rx="15" fill="#f7e7c6" />
      <g fill="#b98a4a">
        <circle cx="132" cy="150" r="5" />
        <circle cx="152" cy="150" r="5" />
        <circle cx="172" cy="150" r="5" />
        <circle cx="192" cy="150" r="5" />
        <circle cx="212" cy="150" r="5" />
        <circle cx="132" cy="170" r="5" />
        <circle cx="152" cy="170" r="5" />
        <circle cx="172" cy="170" r="5" />
        <circle cx="192" cy="170" r="5" />
        <circle cx="212" cy="170" r="5" />
      </g>
      <rect x="126" y="196" width="90" height="34" rx="6" fill="#e8a63e" />
      <circle cx="140" cy="252" r="12" fill="#c8792a" />
      <circle cx="176" cy="252" r="12" fill="#c8792a" />
      <circle cx="212" cy="252" r="12" fill="var(--red)" />
    </svg>
  );
}

export default function TokenGate({ onAuthenticated, notice }) {
  const [token, setToken] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!token || checking) return;
    setChecking(true);
    setError(null);
    try {
      const caps = await makeClient(token).capabilities();
      onAuthenticated(token, caps, remember);
    } catch (err) {
      setError(
        err instanceof Unauthorized
          ? "Invalid API token."
          : `Could not reach the server: ${err.message}`,
      );
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="gate">
      <div className="decor-rings" aria-hidden="true">
        <div />
        <div />
        <div />
        <div />
      </div>
      <div className="decor-dial" aria-hidden="true" />
      <form className="gate-card" onSubmit={submit}>
        <RadioMark />
        <div>
          <div className="wordmark-kicker">Amateur Radio · Part 97</div>
          <h1 className="wordmark">
            radio-server<span className="wordmark-dot">.</span>
          </h1>
        </div>
        <p className="muted">Enter the LAN API token to connect.</p>
        {notice && <div className="notice">{notice}</div>}
        <input
          type="password"
          autoFocus
          placeholder="API token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-label="API token"
        />
        <label className="gate-remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          Remember on this device
        </label>
        <button type="submit" disabled={!token || checking}>
          {checking ? "Connecting…" : "Connect"}
        </button>
        {error && <div className="error" role="alert">{error}</div>}
        <p className="muted gate-foot">
          The token is the <code>api_token</code> secret on the server
          (<code>radio-secrets.toml</code> or <code>RADIO_API_TOKEN</code>).
        </p>
      </form>
    </div>
  );
}
