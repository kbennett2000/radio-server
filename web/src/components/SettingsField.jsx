// One settings field, rendered from its schema entry (ADR 0027).
//
// The control is chosen purely by `type` (string/integer/number/boolean/enum) — no per-setting
// code — and every field shows the schema `description` as always-visible inline help, so the
// operator understands what each does without the docs. Required-unset (value null) is flagged.
//
// Editing is lifted: the parent (`SettingsView`) owns dirty-tracking, so this component is
// controlled — it reports the field's effective value and calls `onChange(key, value)` on edits.

// A readable label from a dotted key: last segment, snake→spaced, title-cased. "station.cw_wpm" →
// "Cw Wpm". The full dotted key is still shown (muted) so there is never any ambiguity.
function labelFor(key) {
  const leaf = key.includes(".") ? key.slice(key.lastIndexOf(".") + 1) : key;
  return leaf
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export default function SettingsField({ spec, value, onChange, error, dirty = false }) {
  const { key, type, choices, required } = spec;
  const needsSetting = required && value === null;

  const set = (v) => onChange(key, v);

  let control;
  if (type === "boolean") {
    control = (
      <label className="toggle">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => set(e.target.checked)}
        />
        <span className="toggle-track" aria-hidden="true" />
        <span className="muted">{value === true ? "On" : "Off"}</span>
      </label>
    );
  } else if (type === "enum") {
    control = (
      <select value={value ?? ""} onChange={(e) => set(e.target.value)}>
        {value === null && <option value="">— unset —</option>}
        {(choices ?? []).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    );
  } else if (type === "integer" || type === "number") {
    control = (
      <input
        type="number"
        step={type === "integer" ? "1" : "any"}
        value={value ?? ""}
        placeholder={needsSetting ? "unset" : ""}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return set(null);
          const n = Number(raw);
          set(Number.isFinite(n) ? n : raw); // keep raw junk so validation can reject it server-side
        }}
      />
    );
  } else {
    control = (
      <input
        type="text"
        value={value ?? ""}
        placeholder={needsSetting ? "unset — needs setting" : ""}
        onChange={(e) => set(e.target.value === "" ? null : e.target.value)}
      />
    );
  }

  return (
    <div className={`setting-field${error ? " has-error" : ""}`}>
      <div className="setting-head">
        <span className="setting-label">{labelFor(key)}</span>
        {required && <span className="tag tag-req">required</span>}
        {needsSetting && <span className="tag tag-unset">needs setting</span>}
        <code className="setting-key muted">{key}</code>
        {dirty && <span className="setting-dirty" title="unsaved change" />}
      </div>
      <div className="setting-control">{control}</div>
      <p className="setting-desc muted">{spec.description}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
