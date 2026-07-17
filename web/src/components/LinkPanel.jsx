// The Mumble link card (ADR 0041/0042): every configured [[mumble.servers]] entry with its live
// state, and per-entry Connect / Disconnect. One link is active at a time — connecting another
// entry switches (the server drops the current one first), so Connect stays enabled on idle
// entries while the active one shows Disconnect.
//
// State plumbing: `link` ({active, entries}) arrives three ways, freshest wins —
//   - the WS `link` events (folded into state.link by useEvents) push every transition,
//     including DTMF- and autoconnect-driven ones;
//   - the POST /link response body reflects a click immediately;
//   - a light poll while a link is active keeps the Mumble-side numbers (connected, peers)
//     honest — pymumble connects asynchronously and peer counts drift with no event.
// One GET on mount seeds the card (WS status frames are RadioStatus-only and carry no link
// block), and `null` (deployment has no entries) hides the card entirely — the TuneControls
// hide-when-unconfigured pattern (ADR 0037).

import { useEffect, useState } from "react";
import { useAction } from "../actions.js";

// Poll cadence while a link is active. Kept brisk so the channel roster and the per-user talk
// indicator (below) refresh promptly as people come, go, and speak — it's one small JSON GET on the
// LAN. Talk indication is therefore poll-granular (~this interval), not instantaneous.
const POLL_MS = 1500;

// The per-entry state pill: the active entry is Linked (green) once Mumble confirms, or
// Connecting… (amber) while the handshake is in flight; everything else is Off.
function entryState(entry) {
  if (entry.running && entry.connected) return { label: "Linked", cls: "state-rx" };
  if (entry.running) return { label: "Connecting…", cls: "state-warn" };
  return { label: "Off", cls: "state-idle" };
}

function EntryRow({ entry, pending, onConnect, onDisconnect }) {
  const st = entryState(entry);
  const detail = [entry.host, entry.channel || null].filter(Boolean).join(" · ");
  return (
    <div className="link-entry">
      <div className="link-entry-head">
        <span className="link-entry-name">{entry.name.replace(/_/g, " ")}</span>
        <span className={`state-pill ${st.cls}`} role="status">
          <span className="state-dot" aria-hidden="true" />
          {st.label}
        </span>
      </div>
      <div className="link-entry-detail">
        <span className="muted">{detail}</span>
        {entry.dtmf && <span className="muted"> · DTMF {entry.dtmf}#</span>}
        {entry.running && entry.connected && entry.peers != null && (
          <span className="muted"> · {entry.peers} peer(s)</span>
        )}
      </div>
      {entry.tx_to_rf === false && (
        <p className="muted">Receive-only: this entry never keys the transmitter.</p>
      )}
      {entry.running && entry.connected && Array.isArray(entry.users) && (
        <ChannelRoster users={entry.users} />
      )}
      <div className="btn-row">
        {entry.running ? (
          <button type="button" onClick={onDisconnect} disabled={pending}>
            {pending ? "Working…" : "Disconnect"}
          </button>
        ) : (
          <button type="button" onClick={onConnect} disabled={pending}>
            {pending ? "Working…" : "Connect"}
          </button>
        )}
      </div>
    </div>
  );
}

// Who else is in the joined Mumble channel (server-side roster, ADR 0041 follow-up). Names are
// sorted server-side; a lit dot marks anyone currently transmitting (best-effort, poll-granular).
function ChannelRoster({ users }) {
  return (
    <div className="link-roster">
      <span className="roster-head">In channel</span>
      {users.length === 0 ? (
        <span className="muted">No one else here.</span>
      ) : (
        <ul className="roster-list">
          {users.map((u) => (
            <li key={u.name} className={`roster-user${u.talking ? " talking" : ""}`}>
              <span className="state-dot" aria-hidden="true" />
              {u.name}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function LinkPanel({ client, link, onAuthError }) {
  // The freshest snapshot we've seen: the mount fetch, a POST /link response, or a poll result.
  // Cleared whenever the WS prop changes shape so a pushed `link` event is never shadowed.
  const [fresh, setFresh] = useState(null);
  const [absent, setAbsent] = useState(false); // confirmed unconfigured -> hide for good
  const { run, pending, error } = useAction({ onAuthError });

  const l = fresh ?? link;
  const active = l?.active ?? null;

  // Seed on mount: WS status frames carry no link block, so without this the card would wait
  // for the first transition event. A `null` body means no entries are configured.
  useEffect(() => {
    let live = true;
    client
      .linkStatus()
      .then((body) => {
        if (!live) return;
        if (body?.link) setFresh(body.link);
        else setAbsent(true);
      })
      .catch(() => {
        /* non-fatal: a WS event or the poll can still populate the card */
      });
    return () => {
      live = false;
    };
  }, [client]);

  // Poll while a link is active (cheap: one small JSON GET) so connected/peers stay honest
  // between events — pymumble's connect completes after POST /link returns.
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    const tick = () => {
      client
        .linkStatus()
        .then((body) => {
          if (live && body?.link) setFresh(body.link);
        })
        .catch(() => {
          /* non-fatal: the next event or poll refreshes */
        });
    };
    const id = setInterval(tick, POLL_MS);
    tick(); // immediate first refresh so "Connecting…" resolves quickly
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client, active]);

  // A pushed link event is authoritative going forward — drop the local override.
  useEffect(() => {
    setFresh(null);
  }, [link]);

  if (absent || l == null) return null;

  const setLink = (entry, on) =>
    run(async () => {
      const body = await client.setLink(entry, on);
      // POST /link returns the fresh link block — reflect it immediately.
      if (body?.link) setFresh(body.link);
    });

  return (
    <div className="card">
      <h2>Mumble link</h2>
      {(l.entries ?? []).map((entry) => (
        <EntryRow
          key={entry.slug ?? entry.name}
          entry={entry}
          pending={pending}
          onConnect={() => setLink(entry.slug ?? entry.name, true)}
          onDisconnect={() => setLink(entry.slug ?? entry.name, false)}
        />
      ))}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
