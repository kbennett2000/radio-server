// The settings screen (ADR 0027): read and edit radio.toml through the browser.
//
// It renders ENTIRELY from `GET /settings` — the schema drives the form, so a setting added to the
// registry later needs zero change here. Fields are grouped by their `group`; edits are
// dirty-tracked and Save PATCHes only the changed keys. The cycle-26 PATCH is atomic: on a 400 the
// whole patch is rejected, so we surface the named key inline and KEEP the operator's edits.
// Changes are restart-to-apply (v1) — a banner says so after every save.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, Unauthorized } from "../api.js";
import SettingsField from "./SettingsField.jsx";
import SecretsPanel from "./SecretsPanel.jsx";
import MumbleServersPanel from "./MumbleServersPanel.jsx";

// Group the flat schema list into [{group, items}] preserving first-appearance order.
function groupSettings(settings) {
  const order = [];
  const byGroup = new Map();
  for (const spec of settings) {
    if (!byGroup.has(spec.group)) {
      byGroup.set(spec.group, []);
      order.push(spec.group);
    }
    byGroup.get(spec.group).push(spec);
  }
  return order.map((group) => ({ group, items: byGroup.get(group) }));
}

// One collapsible group of fields (ADR 0037): native <details> so it's accessible and JS-free.
function GroupPanel({ group, items, open, valueOf, onFieldChange, fieldErrors }) {
  return (
    <details className="settings-group" open={open}>
      <summary>{group}</summary>
      <div className="settings-group-body">
        {items.map((spec) => (
          <SettingsField
            key={spec.key}
            spec={spec}
            value={valueOf(spec)}
            onChange={onFieldChange}
            error={fieldErrors[spec.key]}
          />
        ))}
      </div>
    </details>
  );
}

export default function SettingsView({ client, onAuthError, onReauth }) {
  const [data, setData] = useState(null); // { settings, secrets, apply }
  const [loadError, setLoadError] = useState(null);
  const [edited, setEdited] = useState({}); // key -> new value (dirty set)
  const [fieldErrors, setFieldErrors] = useState({}); // key -> message (from an atomic 400)
  const [saveError, setSaveError] = useState(null); // a non-field-specific save error
  const [saved, setSaved] = useState(null); // { restart_required: [...] } after a good save
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const body = await client.settings();
      setData(body);
    } catch (e) {
      if (e instanceof Unauthorized) return onAuthError?.();
      setLoadError(e.message);
    }
  }, [client, onAuthError]);

  useEffect(() => {
    load();
  }, [load]);

  // Effective value for a field: the pending edit if dirty, else the served value.
  const valueOf = useCallback(
    (spec) => (spec.key in edited ? edited[spec.key] : spec.value),
    [edited],
  );

  const onFieldChange = useCallback(
    (key, value) => {
      setSaved(null); // any edit dismisses the last restart banner
      setSaveError(null);
      setFieldErrors((prev) => {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      });
      const served = data?.settings.find((s) => s.key === key)?.value;
      setEdited((prev) => {
        const next = { ...prev };
        // Reverting a field to its originally-served value clears it from the dirty set.
        if (value === served) delete next[key];
        else next[key] = value;
        return next;
      });
    },
    [data],
  );

  const dirtyKeys = useMemo(() => Object.keys(edited), [edited]);

  const save = useCallback(async () => {
    if (dirtyKeys.length === 0 || saving) return;
    setSaving(true);
    setSaveError(null);
    setFieldErrors({});
    try {
      const res = await client.updateSettings(edited);
      setSaved({ restart_required: res.restart_required ?? [] });
      setEdited({});
      await load(); // reflect the persisted file; clears dirty state
    } catch (e) {
      if (e instanceof Unauthorized) {
        onAuthError?.();
      } else if (e instanceof ApiError && e.status === 400) {
        // Atomic rejection: the detail names the offending key. Attach it inline to that field and
        // KEEP every edit — nothing was written server-side.
        const hitKey = dirtyKeys.find((k) => e.message.includes(k));
        if (hitKey) setFieldErrors({ [hitKey]: e.message });
        else setSaveError(e.message);
      } else {
        setSaveError(e.message);
      }
    } finally {
      setSaving(false);
    }
  }, [client, edited, dirtyKeys, saving, load, onAuthError]);

  if (loadError) {
    return (
      <div className="card">
        <h2>Settings</h2>
        <div className="error" role="alert">
          Could not load settings: {loadError}
        </div>
        <button type="button" onClick={load}>
          Retry
        </button>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card">
        <h2>Settings</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  // Split the schema into everyday (basic) and advanced tiers (ADR 0037). Basic groups render open;
  // the whole advanced tier hides behind one collapsed panel so the page opens short and calm.
  const basicGroups = groupSettings(data.settings.filter((s) => !s.advanced));
  const advancedGroups = groupSettings(data.settings.filter((s) => s.advanced));
  const panelProps = { valueOf, onFieldChange, fieldErrors };

  return (
    <div className="settings">
      <div className="settings-intro card">
        <h2>Settings</h2>
        <p className="muted">
          Edit <code>radio.toml</code> in the browser. Changes are saved to file but{" "}
          <strong>take effect only after the server restarts</strong> (v1).
        </p>
      </div>

      <section className="card settings-tier">
        {basicGroups.map(({ group, items }) => (
          <GroupPanel key={group} group={group} items={items} open {...panelProps} />
        ))}
      </section>

      {advancedGroups.length > 0 && (
        <details className="card settings-tier settings-advanced">
          <summary>
            <span className="settings-advanced-title">Advanced settings</span>
            <span className="muted">tuning &amp; hardware — usually leave as-is</span>
          </summary>
          <div className="settings-advanced-body">
            {advancedGroups.map(({ group, items }) => (
              <GroupPanel key={group} group={group} items={items} open={false} {...panelProps} />
            ))}
          </div>
        </details>
      )}

      <div className="settings-savebar card">
        {saved && (
          <div className="notice" role="status">
            Saved.{" "}
            {saved.restart_required.length > 0 ? (
              <>
                Restart the server to apply:{" "}
                <strong>{saved.restart_required.join(", ")}</strong>.
              </>
            ) : (
              "Restart the server to apply."
            )}
          </div>
        )}
        {saveError && (
          <div className="error" role="alert">
            {saveError}
          </div>
        )}
        <div className="btn-row">
          <button type="button" onClick={save} disabled={dirtyKeys.length === 0 || saving}>
            {saving ? "Saving…" : `Save${dirtyKeys.length ? ` (${dirtyKeys.length})` : ""}`}
          </button>
          {dirtyKeys.length > 0 && (
            <button type="button" className="link" onClick={() => setEdited({})} disabled={saving}>
              Discard changes
            </button>
          )}
        </div>
      </div>

      {/* The [[mumble.servers]] list channel (ADR 0042) — a bespoke editor, since the
          schema-driven form above renders only scalar settings. */}
      <MumbleServersPanel client={client} onAuthError={onAuthError} />

      <SecretsPanel client={client} secrets={data.secrets} onAuthError={onAuthError} onReauth={onReauth} />
    </div>
  );
}
