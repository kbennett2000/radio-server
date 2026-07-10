// The authenticated control panel (ADR 0022): live status + controls + the operating log.
//
// Owns two cross-cutting concerns for its child controls:
//   - capability greying: `hasCap(name)` is true only when the backend advertised the capability
//     AND no 501 has since been seen for it (defensive backup to the advertised list).
//   - the single /events subscription, whose folded state drives the status panel and whose frames
//     fill the event log.

import { useCallback, useMemo, useState } from "react";
import { useEvents } from "../useEvents.js";
import StatusPanel from "./StatusPanel.jsx";
import ListenControl from "./ListenControl.jsx";
import TalkControl from "./TalkControl.jsx";
import TuneControls from "./TuneControls.jsx";
import PttControl from "./PttControl.jsx";
import ScanControl from "./ScanControl.jsx";
import ControllerControl from "./ControllerControl.jsx";
import EventLog from "./EventLog.jsx";
import SettingsView from "./SettingsView.jsx";

export default function ControlPanel({ client, caps, onAuthError, onReauth }) {
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
      </header>

      {view === "settings" ? (
        <SettingsView client={client} onAuthError={onAuthError} onReauth={onReauth} />
      ) : (
      <div className="grid">
        <section className="col">
          <StatusPanel state={state} />
          <ListenControl
            token={client.token}
            transmitting={state.transmitting}
            arbiter={state.arbiter}
            suspendedLocally={talking}
            onAuthError={onAuthError}
          />
          <TalkControl
            token={client.token}
            onAuthError={onAuthError}
            onTalkingChange={setTalking}
          />
          <PttControl client={client} transmitting={state.transmitting} {...actionHooks} />
          <TuneControls client={client} hasCap={hasCap} catAvailable={anyCat} {...actionHooks} />
          <ScanControl client={client} enabled={hasCap("scan")} scan={state.scan} {...actionHooks} />
          <ControllerControl client={client} session={state} {...actionHooks} />
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
