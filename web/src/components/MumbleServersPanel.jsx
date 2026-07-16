// The Mumble servers editor (ADR 0042) — the settings screen's list channel.
//
// [[mumble.servers]] is a list of entries, which the schema-driven form can't render, so this is
// its own panel (the SecretsPanel pattern): GET the full list, edit rows locally (add/remove/
// change), Save PUTs the WHOLE list back — the server validates atomically (slugs, hosts,
// duplicate/colliding DTMF combos) and a 400 keeps every local edit. Restart-to-apply, like every
// setting.
//
// Passwords are write-only (the secrets channel): each saved entry shows a set/not-set tag and a
// one-shot input that POSTs to the per-entry password endpoint — a value is never read back.

import { useCallback, useEffect, useState } from "react";
import { Unauthorized } from "../api.js";

// The server requires entry names to be slugs ([a-z0-9_]{1,32} — they become TOML keys, secret
// names, env suffixes, and URL segments), so the Name field turns whatever the operator types
// into one live ("Mumble Demo" -> "mumble_demo") instead of 400ing at save. The spoken link
// confirmation reads underscores as spaces, so nothing is lost.
const slugify = (value) =>
  value
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .slice(0, 32);

// Combos are matchable DTMF only: 0-9/A-D ('#' submits, '*' clears — they can't appear inside).
const dtmfOnly = (value) => value.toUpperCase().replace(/[^0-9A-D]/g, "");

const BLANK = {
  name: "",
  host: "",
  port: 64738,
  username: "radio-server",
  channel: "",
  dtmf: "",
  tx_to_rf: true,
  autoconnect: false,
  password_set: false,
};

function Field({ label, children }) {
  return (
    <label className="mumble-field">
      <span className="mumble-field-label">{label}</span>
      {children}
    </label>
  );
}

function PasswordControl({ client, name, passwordSet, onAuthError }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState(passwordSet ? "set" : "unset"); // "set" | "unset" | "saved"
  const [error, setError] = useState(null);

  const save = async () => {
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.setMumblePassword(name, value);
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
    <div className="mumble-password">
      <Field label={`Password ${state === "unset" ? "(not set)" : state === "saved" ? "(saved ✓)" : "(set)"}`}>
        <div className="mumble-password-row">
          <input
            type="password"
            value={value}
            placeholder="write-only"
            onChange={(e) => setValue(e.target.value)}
            autoComplete="new-password"
          />
          <button type="button" onClick={save} disabled={!value || busy}>
            {busy ? "Saving…" : "Set"}
          </button>
        </div>
      </Field>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

function EntryEditor({ entry, saved, client, onChange, onRemove, onAuthError }) {
  const set = (field, value) => onChange({ ...entry, [field]: value });
  return (
    <div className="mumble-entry">
      <div className="mumble-entry-grid">
        <Field label="Name (lowercase slug)">
          <input
            type="text"
            value={entry.name}
            placeholder="home"
            onChange={(e) => set("name", slugify(e.target.value))}
          />
        </Field>
        <Field label="Host">
          <input
            type="text"
            value={entry.host}
            placeholder="murmur.example.net"
            onChange={(e) => set("host", e.target.value)}
          />
        </Field>
        <Field label="Port">
          <input
            type="number"
            value={entry.port}
            onChange={(e) =>
              // A cleared field must not become port 0 (Number("") === 0) — fall to the default.
              set("port", e.target.value === "" ? 64738 : Number(e.target.value))
            }
          />
        </Field>
        <Field label="Username">
          <input
            type="text"
            value={entry.username}
            onChange={(e) => set("username", e.target.value)}
          />
        </Field>
        <Field label="Channel (empty = root)">
          <input
            type="text"
            value={entry.channel}
            onChange={(e) => set("channel", e.target.value)}
          />
        </Field>
        <Field label="DTMF combo (empty = none)">
          <input
            type="text"
            value={entry.dtmf}
            placeholder="13"
            onChange={(e) => set("dtmf", dtmfOnly(e.target.value))}
          />
        </Field>
      </div>
      <div className="mumble-entry-flags">
        <label>
          <input
            type="checkbox"
            checked={entry.tx_to_rf}
            onChange={(e) => set("tx_to_rf", e.target.checked)}
          />{" "}
          Transmit Mumble voice over RF
        </label>
        <label>
          <input
            type="checkbox"
            checked={entry.autoconnect}
            onChange={(e) => set("autoconnect", e.target.checked)}
          />{" "}
          Connect on boot
        </label>
      </div>
      {saved ? (
        <PasswordControl
          client={client}
          name={entry.name}
          passwordSet={entry.password_set}
          onAuthError={onAuthError}
        />
      ) : (
        <p className="muted">Save the list first, then set this entry's password (write-only).</p>
      )}
      <div className="btn-row">
        <button type="button" className="link" onClick={onRemove}>
          Remove entry
        </button>
      </div>
    </div>
  );
}

export default function MumbleServersPanel({ client, onAuthError }) {
  const [servers, setServers] = useState(null); // local editable list
  const [savedNames, setSavedNames] = useState(new Set()); // entries present in the persisted file
  const [dirty, setDirty] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const [savedBanner, setSavedBanner] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const body = await client.mumbleServers();
      setServers(body.servers);
      setSavedNames(new Set(body.servers.map((s) => s.name)));
      setDirty(false);
    } catch (e) {
      if (e instanceof Unauthorized) return onAuthError?.();
      setLoadError(e.message);
    }
  }, [client, onAuthError]);

  useEffect(() => {
    load();
  }, [load]);

  const edit = (index, next) => {
    setSavedBanner(false);
    setSaveError(null);
    setDirty(true);
    setServers((prev) => prev.map((s, i) => (i === index ? next : s)));
  };

  const add = () => {
    setSavedBanner(false);
    setDirty(true);
    setServers((prev) => [...(prev ?? []), { ...BLANK }]);
  };

  const remove = (index) => {
    setSavedBanner(false);
    setDirty(true);
    setServers((prev) => prev.filter((_, i) => i !== index));
  };

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      // Strip the read-only presence flag; the server rejects unknown fields as typos.
      const body = await client.saveMumbleServers(
        servers.map(({ password_set, ...entry }) => entry),
      );
      setServers(body.servers);
      setSavedNames(new Set(body.servers.map((s) => s.name)));
      setDirty(false);
      setSavedBanner(true);
    } catch (e) {
      // An atomic 400 names the offending entry/field; every local edit is kept.
      if (e instanceof Unauthorized) onAuthError?.();
      else setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card">
      <h2>Mumble servers</h2>
      <p className="muted">
        Destinations the station can link to — one active at a time; connecting another switches.
        An entry's DTMF combo (keyed as <code>combo#</code> in an authenticated session) connects
        it over the air; <code>73#</code> disconnects. Saved to <code>radio.toml</code>;{" "}
        <strong>restart to apply</strong>.
      </p>
      {loadError && (
        <div className="error" role="alert">
          Could not load: {loadError}{" "}
          <button type="button" className="link" onClick={load}>
            Retry
          </button>
        </div>
      )}
      {servers === null && !loadError && <p className="muted">Loading…</p>}
      {servers !== null && (
        <>
          {servers.length === 0 && <p className="muted">No servers configured.</p>}
          {servers.map((entry, index) => (
            <EntryEditor
              key={index}
              entry={entry}
              saved={savedNames.has(entry.name)}
              client={client}
              onChange={(next) => edit(index, next)}
              onRemove={() => remove(index)}
              onAuthError={onAuthError}
            />
          ))}
          {savedBanner && (
            <div className="notice" role="status">
              Saved. Restart the server to apply.
            </div>
          )}
          {saveError && (
            <div className="error" role="alert">
              {saveError}
            </div>
          )}
          <div className="btn-row">
            <button type="button" onClick={add}>
              Add server
            </button>
            <button type="button" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save servers"}
            </button>
            {dirty && (
              <button type="button" className="link" onClick={load} disabled={saving}>
                Discard changes
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
