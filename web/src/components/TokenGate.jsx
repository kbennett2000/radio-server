// The token gate (ADR 0022): prompt for the LAN API token, validate it by fetching /capabilities,
// and lift {token, caps} to App on success. A bad token yields a clear inline error — never a
// silent hang. The token is held only in this form's local state until it is handed up.

import { useState } from "react";
import { makeClient, Unauthorized } from "../api.js";

export default function TokenGate({ onAuthenticated, notice }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!token || checking) return;
    setChecking(true);
    setError(null);
    try {
      const caps = await makeClient(token).capabilities();
      onAuthenticated(token, caps);
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
      <form className="gate-card" onSubmit={submit}>
        <h1>radio-server</h1>
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
        <button type="submit" disabled={!token || checking}>
          {checking ? "Connecting…" : "Connect"}
        </button>
        {error && <div className="error" role="alert">{error}</div>}
      </form>
    </div>
  );
}
