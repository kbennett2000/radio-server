// The authenticated control panel (ADR 0022; simplified in ADR 0037): live status + controls + the
// operating log.
//
// Owns two cross-cutting concerns for its child controls:
//   - capability greying: `hasCap(name)` is true only when the backend advertised the capability
//     AND no 501 has since been seen for it (defensive backup to the advertised list).
//   - the single /events subscription, whose folded state drives the status panel and whose frames
//     fill the event log.
//
// Card order puts the everyday actions first — Listen, then Talk — with Status and the capability-
// gated Tune/Scan/Services below. The bare PTT toggle and the Controller Start/Stop card were removed
// (ADR 0037): `/ptt` still backs Talk, and the controller now autostarts via `controller.autostart`.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useEvents } from "../useEvents.js";
import StatusPanel from "./StatusPanel.jsx";
import ListenControl from "./ListenControl.jsx";
import TalkControl from "./TalkControl.jsx";
import TuneControls from "./TuneControls.jsx";
import ScanControl from "./ScanControl.jsx";
import LinkPanel from "./LinkPanel.jsx";
import ServicesView from "./ServicesView.jsx";
import EventLog from "./EventLog.jsx";
import SettingsView from "./SettingsView.jsx";
import SecureContextNotice from "./SecureContextNotice.jsx";

export default function ControlPanel({ client, caps, onAuthError, onReauth, onLogout }) {
  // Which screen is showing — the live control grid, or the settings editor (ADR 0027). A minimal
  // in-place view switch (no router); the /events subscription below stays live across both.
  const [view, setView] = useState("control");

  // Capabilities discovered unsupported at runtime via a 501 (belt-and-suspenders over `caps`).
  const [disabledCaps, setDisabledCaps] = useState(() => new Set());
  const advertised = useMemo(() => new Set(caps), [caps]);

  const onUnsupported = useCallback((capability) => {
    if (!capability) return;
    setDisabledCaps((prev) => new Set(prev).add(capability));
  }, []);

  const hasCap = useCallback(
    (name) => advertised.has(name) && !disabledCaps.has(name),
    [advertised, disabledCaps],
  );

  const { state, events, conn, clearEvents } = useEvents(client.token, onAuthError);

  // Auto-listen preference (ADR 0037): the server setting `web.auto_listen`, read once at mount.
  // Undefined until resolved; ListenControl only self-starts once it flips true (browsers allow the
  // audio start because logging in was a user gesture). A failed fetch just leaves it off.
  const [autoListen, setAutoListen] = useState(false);
  useEffect(() => {
    let live = true;
    client
      .settings()
      .then((body) => {
        if (!live) return;
        const spec = body?.settings?.find((s) => s.key === "web.auto_listen");
        if (spec) setAutoListen((spec.value ?? spec.default) === true);
      })
      .catch(() => {
        /* non-fatal: fall back to manual Listen */
      });
    return () => {
      live = false;
    };
  }, [client]);

  // True while THIS operator is talking (streaming to /audio/tx). Drives the local RX self-mute:
  // the server suspends RX during our TX, but the RX jitter buffer would still play its buffered
  // tail — so we force-mute the monitor the instant we key (ADR 0024). Gated on our own talk, not
  // the global `transmitting`, so a remote operator's TX doesn't mute our monitor.
  const [talking, setTalking] = useState(false);

  const actionHooks = { onAuthError, onUnsupported };
  const anyCat =
    hasCap("set_frequency") || hasCap("set_channel") || hasCap("set_tone") || hasCap("set_mode");

  return (
    <div className="panel">
      <header className="topbar">
        <h1>radio-server</h1>
        <nav className="viewnav">
          <button
            type="button"
            className={`viewtab${view === "control" ? " active" : ""}`}
            onClick={() => setView("control")}
          >
            Control
          </button>
          <button
            type="button"
            className={`viewtab${view === "settings" ? " active" : ""}`}
            onClick={() => setView("settings")}
          >
            Settings
          </button>
        </nav>
        <ConnBadge conn={conn} />
        {onLogout && (
          <button type="button" className="link logout" onClick={onLogout}>
            Log out
          </button>
        )}
      </header>

      <SecureContextNotice />

      {view === "settings" ? (
        <SettingsView client={client} onAuthError={onAuthError} onReauth={onReauth} />
      ) : (
      <div className="grid">
        <section className="col">
          <ListenControl
            token={client.token}
            transmitting={state.transmitting}
            arbiter={state.arbiter}
            suspendedLocally={talking}
            autoStart={autoListen}
            onAuthError={onAuthError}
          />
          <TalkControl
            token={client.token}
            onAuthError={onAuthError}
            onTalkingChange={setTalking}
          />
          <StatusPanel state={state} hasCap={hasCap} />
          {/* The Mumble link card renders only when the deployment configured the link — the
              LinkPanel hides itself while state.link is null/undefined (ADR 0041 Cycle D). */}
          <LinkPanel client={client} link={state.link} onAuthError={onAuthError} />
          {/* CAT tuning/scan are hidden entirely on a radio that lacks them (e.g. the audio-only
              Baofeng advertises no CAT caps) rather than shown greyed — an unusable control is just
              noise. On the V71 (FULL_CAPS) both still render. */}
          {anyCat && (
            <TuneControls client={client} hasCap={hasCap} catAvailable={anyCat} {...actionHooks} />
          )}
          {hasCap("scan") && (
            <ScanControl client={client} enabled scan={state.scan} {...actionHooks} />
          )}
          <ServicesView client={client} onAuthError={onAuthError} />
        </section>
        <section className="col">
          <EventLog events={events} onClear={clearEvents} />
        </section>
      </div>
      )}
    </div>
  );
}

function ConnBadge({ conn }) {
  const label = { open: "live", connecting: "connecting…", reconnecting: "reconnecting…" }[conn];
  return <span className={`conn conn-${conn}`}>● {label ?? conn}</span>;
}
