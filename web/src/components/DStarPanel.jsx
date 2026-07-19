// The D-STAR reflector card (ADR 0088): pick a reflector (REF/XRF/DCS/XLX) and Connect / Disconnect
// this endpoint's module. One reflector is linked at a time — connecting another switches.
//
// Free-text + presets (the operator's choice): type any reflector as "NAME MODULE" (e.g. "REF030 C",
// "XRF012 A") or tap a preset. Linking sends the standard D-STAR URCALL command through the bridge;
// there is no gateway readback (its remote-control interface is off), so the "Linked" state is what
// we last *sent*, not a confirmation — surfaced honestly below.
//
// State plumbing mirrors LinkPanel: `dstar` ({active, mode, ...}) arrives via the WS `dstar` events
// (folded into state.dstar), the POST responses reflect a click immediately, and a light poll while
// configured keeps `mode` honest. One GET on mount seeds the card; `null` (D-STAR unconfigured)
// hides it entirely.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

const POLL_MS = 2000;
const PRESETS = ["REF001 C", "REF030 C"];

function linkState(dstar) {
  const active = dstar?.active ?? null;
  if (active) return { label: `Linked · ${active.reflector}`, cls: "state-rx" };
  return { label: "Not linked", cls: "state-idle" };
}

export default function DStarPanel({ client, dstar, onAuthError }) {
  const [fresh, setFresh] = useState(null);
  const [absent, setAbsent] = useState(false);
  const [reflector, setReflector] = useState("");
  const { run, pending, error } = useAction({ onAuthError });

  const d = fresh ?? dstar;
  const active = d?.active ?? null;
  const st = linkState(d);

  // Seed on mount (WS status frames carry no dstar block). `null` body → D-STAR not configured → hide.
  useEffect(() => {
    let live = true;
    client
      .dstarStatus()
      .then((body) => {
        if (!live) return;
        if (body?.dstar) setFresh(body.dstar);
        else setAbsent(true);
      })
      .catch(() => {
        /* non-fatal: a WS event or the poll can still populate the card */
      });
    return () => {
      live = false;
    };
  }, [client]);

  // Light poll while configured so `mode` (idle/rx/tx) stays honest between events.
  useEffect(() => {
    if (absent) return undefined;
    let live = true;
    const id = setInterval(() => {
      client
        .dstarStatus()
        .then((body) => {
          if (live && body?.dstar) setFresh(body.dstar);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client, absent]);

  // A pushed dstar event is authoritative going forward — drop the local override.
  useEffect(() => {
    setFresh(null);
  }, [dstar]);

  if (absent || d == null) return null;

  const link = (name) =>
    run(async () => {
      const body = await client.dstarLink(name);
      if (body?.dstar) setFresh(body.dstar);
    });
  const unlink = () =>
    run(async () => {
      const body = await client.dstarUnlink();
      if (body?.dstar) setFresh(body.dstar);
    });

  const submit = (e) => {
    e.preventDefault();
    const name = reflector.trim();
    if (name) link(name);
  };

  return (
    <div className="card">
      <div className="log-head">
        <h2>D-STAR reflector</h2>
        <span className={`state-pill ${st.cls}`} role="status">
          <span className="state-dot" aria-hidden="true" />
          {st.label}
        </span>
      </div>

      <div className="btn-row" style={{ flexWrap: "wrap" }}>
        {PRESETS.map((p) => (
          <button
            type="button"
            key={p}
            onClick={() => {
              setReflector(p);
              link(p);
            }}
            disabled={pending}
            title={`Link ${p}`}
          >
            {p}
          </button>
        ))}
      </div>

      <form onSubmit={submit} className="dstar-picker">
        <input
          type="text"
          value={reflector}
          onChange={(e) => setReflector(e.target.value)}
          placeholder="REF030 C"
          aria-label="Reflector (name and module letter)"
          spellCheck={false}
          autoCapitalize="characters"
        />
        <div className="btn-row">
          <button type="submit" disabled={pending || !reflector.trim()}>
            {pending ? "Working…" : "Connect"}
          </button>
          <button type="button" onClick={unlink} disabled={pending || !active}>
            Disconnect
          </button>
        </div>
      </form>

      <p className="muted">
        Type a reflector as name + module (e.g. <code>REF030 C</code>, <code>XRF012 A</code>) or tap a
        preset. Link state shown is what was last sent — the gateway doesn&rsquo;t report it back.
      </p>

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
