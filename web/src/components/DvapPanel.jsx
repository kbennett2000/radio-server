// The DVAP control card (ADR 0095/0096): one row per DVAP gateway module (e.g. B = 441.600,
// C = 441.000). Pick a reflector and Connect / Disconnect that module over the gateway's remote-control
// interface. Unlike the D-STAR reflector card, the link state here is CONFIRMED — read back from the
// gateway — so a module shows the reflector it is actually linked to, or "Unreachable" if the gateway
// isn't answering for it.
//
// State plumbing mirrors DStarPanel: `dvap` ({configured, remote, modules:[...]}) arrives via the WS
// `dvap` events (folded into state.dvap), the POST responses reflect a click immediately, and a light
// poll while configured refreshes confirmed state. One GET on mount seeds the card; `null` (no DVAP
// module configured) hides it entirely.

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

const POLL_MS = 2000;

function moduleState(m) {
  if (!m.reachable) return { label: "Unreachable", cls: "state-idle" };
  if (m.linked) return { label: `Linked · ${m.reflector}`, cls: "state-rx" };
  return { label: "Not linked", cls: "state-idle" };
}

function mhz(hz) {
  return `${(hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")} MHz`;
}

export default function DvapPanel({ client, dvap, onAuthError }) {
  const [fresh, setFresh] = useState(null);
  const [absent, setAbsent] = useState(false);
  // Per-module reflector input text, keyed by module letter.
  const [reflectors, setReflectors] = useState({});
  const { run, pending, error } = useAction({ onAuthError });

  const d = fresh ?? dvap;

  // Seed on mount (WS status frames carry no dvap block). `null` body → DVAP not configured → hide.
  useEffect(() => {
    let live = true;
    client
      .dvapStatus()
      .then((body) => {
        if (!live) return;
        if (body?.dvap) setFresh(body.dvap);
        else setAbsent(true);
      })
      .catch(() => {
        /* non-fatal: a WS event or the poll can still populate the card */
      });
    return () => {
      live = false;
    };
  }, [client]);

  // Light poll while configured so confirmed link state stays current between events.
  useEffect(() => {
    if (absent) return undefined;
    let live = true;
    const id = setInterval(() => {
      client
        .dvapStatus()
        .then((body) => {
          if (live && body?.dvap) setFresh(body.dvap);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client, absent]);

  // A pushed dvap event is authoritative going forward — drop the local override.
  useEffect(() => {
    setFresh(null);
  }, [dvap]);

  if (absent || d == null) return null;

  const modules = d.modules ?? [];

  const setText = (mod, value) => setReflectors((r) => ({ ...r, [mod]: value }));

  const link = (mod, name) =>
    run(async () => {
      const body = await client.dvapLink(mod, name);
      if (body?.dvap) setFresh(body.dvap);
    });
  const unlink = (mod) =>
    run(async () => {
      const body = await client.dvapUnlink(mod);
      if (body?.dvap) setFresh(body.dvap);
    });

  return (
    <div className="card">
      <div className="log-head">
        <h2>DVAP</h2>
        <span className="muted">D-STAR hotspots</span>
      </div>

      {modules.map((m) => {
        const st = moduleState(m);
        const text = reflectors[m.module] ?? "";
        const submit = (e) => {
          e.preventDefault();
          const name = text.trim();
          if (name) link(m.module, name);
        };
        return (
          <div className="dvap-module" key={m.module}>
            <div className="log-head">
              <h3>
                {m.label} <span className="muted">· {mhz(m.frequency_hz)}</span>
              </h3>
              <span className={`state-pill ${st.cls}`} role="status">
                <span className="state-dot" aria-hidden="true" />
                {st.label}
              </span>
            </div>
            <form onSubmit={submit} className="dstar-picker">
              <input
                type="text"
                value={text}
                onChange={(e) => setText(m.module, e.target.value)}
                placeholder="XLX999 A"
                aria-label={`Reflector for module ${m.module} (name and module letter)`}
                spellCheck={false}
                autoCapitalize="characters"
              />
              <div className="btn-row">
                <button type="submit" disabled={pending || !text.trim()}>
                  {pending ? "Working…" : "Connect"}
                </button>
                <button
                  type="button"
                  onClick={() => unlink(m.module)}
                  disabled={pending || !m.linked}
                >
                  Disconnect
                </button>
              </div>
            </form>
          </div>
        );
      })}

      <p className="muted">
        Each row is a DVAP module the gateway hosts. Type a reflector as name + module (e.g.{" "}
        <code>XLX999 A</code>, <code>REF001 C</code>). Link state is confirmed by the gateway.
      </p>

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
