// The secrets section of the settings screen (ADR 0027) — separate and honest.
//
// Secrets are never read back (the cycle-25/26 security split): GET reports only present/absent, and
// the write-only rotation endpoints reveal a freshly-minted secret exactly ONCE. So this panel shows
// presence, and after a rotate/enroll it displays the new value one time with a copy affordance, then
// nothing persists it. Restart-to-apply: the new API token becomes active only after a restart, so the
// current session keeps working until then — we say so, and offer a return-to-gate for re-auth.

import { useState } from "react";
import { Unauthorized } from "../api.js";
import QrCode from "./QrCode.jsx";

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked (e.g. non-secure context) — the value is shown to copy by hand
    }
  };
  return (
    <button type="button" onClick={copy}>
      {copied ? "Copied ✓" : "Copy"}
    </button>
  );
}

export default function SecretsPanel({ client, secrets, onAuthError, onReauth }) {
  const totpSet = !!secrets?.totp_secret?.set; // first-time "Set up" vs "Re-enroll" wording
  const [busy, setBusy] = useState(null); // "token" | "totp" while a request is in flight
  const [error, setError] = useState(null);
  const [newToken, setNewToken] = useState(null); // shown once after a rotate
  const [totp, setTotp] = useState(null); // { provisioning_uri, secret } shown once after enroll

  const run = async (kind, fn, onOk) => {
    setBusy(kind);
    setError(null);
    try {
      onOk(await fn());
    } catch (e) {
      if (e instanceof Unauthorized) onAuthError?.();
      else setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const rotateToken = () =>
    run("token", () => client.rotateApiToken(), (res) => {
      setTotp(null);
      setNewToken(res.api_token);
    });

  const enroll = () =>
    run("totp", () => client.enrollTotp(), (res) => {
      setNewToken(null);
      setTotp({ provisioning_uri: res.provisioning_uri, secret: res.secret });
    });

  return (
    <section className="card secrets">
      <h2>Secrets</h2>
      <p className="muted">
        Secrets are never shown here — only whether each is set. Rotating reveals the new value{" "}
        <strong>once</strong>; capture it before leaving this screen.
      </p>

      <div className="secret-row">
        <span className="secret-name">API token</span>
        <PresenceBadge set={secrets?.api_token?.set} />
        <button type="button" onClick={rotateToken} disabled={busy !== null}>
          {busy === "token" ? "Rotating…" : "Rotate API token"}
        </button>
      </div>

      {newToken && (
        <div className="secret-reveal">
          <div className="reveal-head">New API token — shown once</div>
          <div className="reveal-value">
            <code>{newToken}</code>
            <CopyButton text={newToken} />
          </div>
          <p className="muted">
            This takes effect <strong>after you restart the server</strong>. Your current session keeps
            working with the old token until then. Store it in the secrets file, restart, then
            re-authenticate with the new token.
          </p>
          {onReauth && (
            <button type="button" className="link" onClick={onReauth}>
              Return to token gate to re-enter it
            </button>
          )}
        </div>
      )}

      <div className="secret-row">
        <span className="secret-name">Over-the-air login code (TOTP)</span>
        <PresenceBadge set={secrets?.totp_secret?.set} />
        <button type="button" onClick={enroll} disabled={busy !== null}>
          {busy === "totp"
            ? "Enrolling…"
            : totpSet
              ? "Re-enroll TOTP"
              : "Set up login code"}
        </button>
      </div>

      {totp && (
        <div className="secret-reveal">
          <div className="reveal-head">
            {totpSet ? "Scan to re-enroll — shown once" : "Scan to set up your login code — shown once"}
          </div>
          <div className="reveal-qr">
            <QrCode value={totp.provisioning_uri} />
          </div>
          <p className="muted">
            Scan with your authenticator app (Google Authenticator, Authy, or any TOTP app)
            {totpSet ? ". This replaces any previous login code and takes" : ". It takes"} effect
            after a server restart. If you can't scan, use the URI:
          </p>
          <div className="reveal-value">
            <code className="reveal-uri">{totp.provisioning_uri}</code>
            <CopyButton text={totp.provisioning_uri} />
          </div>
        </div>
      )}

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

function PresenceBadge({ set }) {
  return (
    <span className={`presence ${set ? "present" : "absent"}`}>{set ? "set" : "not set"}</span>
  );
}
