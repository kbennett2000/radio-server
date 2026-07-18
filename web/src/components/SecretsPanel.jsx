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
  const fixedSet = !!secrets?.fixed_code?.set; // whether a fixed login code is already stored
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

      <div className="secret-row">
        <span className="secret-name">Fixed login code</span>
        <PresenceBadge set={secrets?.fixed_code?.set} />
        <FixedCodeControl client={client} set={fixedSet} onAuthError={onAuthError} />
      </div>
      <p className="muted secret-warning" role="note">
        <strong>Less secure.</strong> A fixed code never changes, so anyone who overhears it over the
        air can reuse it — unlike the rotating login code above, it gets no single-use protection. Use
        it only if you accept that. It takes effect after a restart, and only when{" "}
        <code>auth.fixed_code</code> is turned on above and a login is required.
      </p>

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

// Write-only 6-digit fixed login code (ADR 0083), mirroring the Mumble per-entry password control:
// the code lands on the 0600 secrets channel and is never read back, so this only ever SETS a value.
function FixedCodeControl({ client, set, onAuthError }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState(set ? "set" : "unset"); // "set" | "unset" | "saved"
  const [error, setError] = useState(null);

  const valid = /^\d{6}$/.test(value);

  const save = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.setFixedCode(value);
      setValue("");
      setState("saved");
    } catch (e) {
      if (e instanceof Unauthorized) onAuthError?.();
      else setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed-code">
      <div className="fixed-code-row">
        <input
          type="password"
          inputMode="numeric"
          maxLength={6}
          value={value}
          placeholder={state === "saved" ? "saved ✓" : "6 digits, write-only"}
          onChange={(e) => setValue(e.target.value.replace(/\D/g, ""))}
          autoComplete="off"
        />
        <button type="button" onClick={save} disabled={!valid || busy}>
          {busy ? "Saving…" : "Set"}
        </button>
      </div>
      {value && !valid && <span className="muted">Enter exactly 6 digits.</span>}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

function PresenceBadge({ set }) {
  return (
    <span className={`presence ${set ? "present" : "absent"}`}>{set ? "set" : "not set"}</span>
  );
}
