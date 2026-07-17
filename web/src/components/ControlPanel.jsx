// The authenticated control panel (ADR 0022; simplified in ADR 0037; retro-ham face in ADR 0044):
// live status + controls + the operating log.
//
// Owns two cross-cutting concerns for its child controls:
//   - capability greying: `hasCap(name)` is true only when the backend advertised the capability
//     AND no 501 has since been seen for it (defensive backup to the advertised list).
//   - the single /events subscription, whose folded state drives the status panel and whose frames
//     fill the event log.
//
// Layout (ADR 0044): a masthead, then the "radio face" hero groups the everyday surface — state
// lamp, frequency LCD + dial scale (CAT radios only), and the Monitor/Talk pair. The remaining
// cards sit below in two columns. The bare PTT toggle and the Controller Start/Stop card were
// removed (ADR 0037): `/ptt` still backs Talk, and the controller autostarts.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useEvents } from "../useEvents.js";
import StatusPanel from "./StatusPanel.jsx";
import ListenControl from "./ListenControl.jsx";
import TalkControl from "./TalkControl.jsx";
import TuneControls from "./TuneControls.jsx";
import ScanControl from "./ScanControl.jsx";
import LinkPanel from "./LinkPanel.jsx";
import ServicesView from "./ServicesView.jsx";
import ThemeToggle from "./ThemeToggle.jsx";
import TotpCard from "./TotpCard.jsx";
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
  const showDial = hasCap("set_frequency");

  // ADR 0050: with a Mumble link active, the browser is a Mumble client — Monitor/Transmit target
  // the channel instead of RF. `state.link.active` is pushed reliably by the `link` WS event.
  const mumbleMode = !!state.link?.active;

  return (
    <div className="panel">
      <header className="topbar">
        <div className="wordmark-block">
          <div className="wordmark-kicker">Amateur Radio · Part 97</div>
          <h1 className="wordmark">
            radio-server<span className="wordmark-dot">.</span>
          </h1>
        </div>
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
        {/* The current over-the-air login code, always at hand in the header (hidden when no
            TOTP secret is enrolled). Clicking it opens the OTA session (ADR 0046); it lights up
            while one is open (same derivation as the StatusPanel row). */}
        <TotpCard
          client={client}
          sessionOpen={
            !!(state.session ? state.session.phase === "session_open" : state.sessionOpen)
          }
        />
        <ThemeToggle />
        {onLogout && (
          <button type="button" className="logout" onClick={onLogout}>
            Log out
          </button>
        )}
      </header>

      <SecureContextNotice />

      {view === "settings" ? (
        <SettingsView client={client} onAuthError={onAuthError} onReauth={onReauth} />
      ) : (
        <>
          <section className="face">
            <div className="face-head">
              <StateLamp state={state} />
              <div className="face-right">
                {state.backend && <span className="backend-label">{state.backend}</span>}
                {showDial && <FreqLcd hz={state.frequency} mode={state.mode} />}
              </div>
            </div>
            {showDial && <DialScale state={state} />}
            <div className="face-grid">
              <ListenControl
                token={client.token}
                transmitting={state.transmitting}
                arbiter={state.arbiter}
                suspendedLocally={talking}
                autoStart={autoListen}
                mumbleMode={mumbleMode}
                onAuthError={onAuthError}
              />
              <TalkControl
                token={client.token}
                onAuthError={onAuthError}
                onTalkingChange={setTalking}
                mumbleMode={mumbleMode}
              />
            </div>
          </section>

          <div className="grid">
            <section className="col">
              <ServicesView client={client} onAuthError={onAuthError} />
              {/* CAT tuning/scan are hidden entirely on a radio that lacks them (e.g. the
                  audio-only Baofeng advertises no CAT caps) rather than shown greyed — an unusable
                  control is just noise. On the V71 (FULL_CAPS) both still render. */}
              {anyCat && (
                <TuneControls client={client} hasCap={hasCap} catAvailable={anyCat} {...actionHooks} />
              )}
              {hasCap("scan") && (
                <ScanControl client={client} enabled scan={state.scan} {...actionHooks} />
              )}
              {/* The Mumble link card renders only when the deployment configured the link — the
                  LinkPanel hides itself while state.link is null/undefined (ADR 0041 Cycle D). */}
              <LinkPanel client={client} link={state.link} onAuthError={onAuthError} />
            </section>
            <section className="col">
              <StatusPanel state={state} hasCap={hasCap} />
              <EventLog events={events} onClear={clearEvents} />
            </section>
          </div>
        </>
      )}
    </div>
  );
}

// One prominent state lamp derived from the live flags. Transmitting wins over busy (half-duplex:
// while we key, RX is suspended anyway), and idle is the resting state.
function StateLamp({ state }) {
  const st = state.transmitting
    ? { label: "On air", cls: "state-tx" }
    : state.busy
      ? { label: "Receiving", cls: "state-rx" }
      : { label: "Idle", cls: "state-idle" };
  return (
    <span className={`state-pill state-lamp ${st.cls}`} role="status">
      <span className="state-dot" aria-hidden="true" />
      {st.label}
    </span>
  );
}

function FreqLcd({ hz, mode }) {
  return (
    <span className="freq-block">
      <span className="freq-labels">
        <span>Frequency</span>
        {mode && <span>{mode}</span>}
      </span>
      <span className="freq-lcd" role="status" aria-label="tuned frequency">
        <span className="freq-value">{hz != null ? (hz / 1e6).toFixed(4) : "———.————"}</span>
        <span className="freq-unit">MHz</span>
      </span>
    </span>
  );
}

// Decorative-but-live dial: 144–148 MHz mapped across the strip, needle clamped to 2–98%. It
// follows the scanning frequency while a scan runs, else the tuned frequency. Purely visual —
// the LCD above and the status rows stay the accessible readouts.
function DialScale({ state }) {
  const scanning = state.scan && state.scan.phase !== "stopped" && state.scan.frequency;
  const hz = scanning ? state.scan.frequency : state.frequency;
  const pct = Number.isFinite(hz) ? Math.min(98, Math.max(2, ((hz - 144e6) / 4e6) * 100)) : null;
  return (
    <div className="dial" aria-hidden="true">
      <div className="dial-bands">
        <span>144</span>
        <span>145</span>
        <span>146</span>
        <span>147</span>
        <span>148</span>
      </div>
      <div className="dial-minor" />
      <div className="dial-major" />
      {pct != null && <div className="dial-needle" style={{ left: `${pct}%` }} />}
    </div>
  );
}

function ConnBadge({ conn }) {
  const label = { open: "live", connecting: "connecting…", reconnecting: "reconnecting…" }[conn];
  return (
    <span className={`conn conn-${conn}`}>
      <span className="conn-dot" aria-hidden="true" />
      {label ?? conn}
    </span>
  );
}
