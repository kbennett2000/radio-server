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
import BackendPanel from "./BackendPanel.jsx";
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

  const { state, events, conn, clearEvents } = useEvents(client.token, onAuthError);

  // Capabilities discovered unsupported at runtime via a 501 (belt-and-suspenders over `caps`).
  const [disabledCaps, setDisabledCaps] = useState(() => new Set());
  // The `caps` prop is the one-shot set captured at login; a live backend switch (ADR 0077) re-emits
  // a `capabilities` event that `useEvents` folds into `state.caps`, so prefer the reactive value and
  // fall back to the login prop only until the first such event arrives. This is what re-greys the
  // tuning/scan controls (via `hasCap` → the render gates below) without a reconnect.
  const advertised = useMemo(() => new Set(state.caps ?? caps), [state.caps, caps]);

  // `disabledCaps` only ever grows (a 501 greys a control; nothing un-greys it), so a backend switch
  // must clear it — otherwise a cap the *new* radio supports stays greyed by the *previous* radio's
  // 501. A fresh `state.caps` (only a switch changes it) is exactly that signal.
  useEffect(() => {
    setDisabledCaps(new Set());
  }, [state.caps]);

  const onUnsupported = useCallback((capability) => {
    if (!capability) return;
    setDisabledCaps((prev) => new Set(prev).add(capability));
  }, []);

  const hasCap = useCallback(
    (name) => advertised.has(name) && !disabledCaps.has(name),
    [advertised, disabledCaps],
  );

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
  // the channel instead of RF. `state.link.active` is pushed by the `link` WS event on every
  // transition — but an `autoconnect` entry links at server startup, before this page's /events
  // socket exists, so that transition is never replayed. Seed the mode from a mount GET (like
  // LinkPanel does for its card) so a link already up at load is honored. A pushed event always
  // wins (`??` only falls back when state.link is absent), so a later connect/disconnect is live.
  const [seedLink, setSeedLink] = useState(null);
  useEffect(() => {
    let live = true;
    client
      .linkStatus()
      .then((body) => {
        if (live && body?.link) setSeedLink(body.link);
      })
      .catch(() => {
        /* non-fatal: a WS transition still flips the mode when one happens */
      });
    return () => {
      live = false;
    };
  }, [client]);
  const mumbleMode = !!(state.link?.active ?? seedLink?.active);

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
              {/* The live backend switch (ADR 0077) — first, since it reconfigures the whole radio.
                  Self-hides unless two+ backends are configured. Selecting one re-emits capabilities,
                  which re-greys the CAT tuning/scan cards below via the reactive `state.caps`. */}
              <BackendPanel
                client={client}
                active={state.backend}
                transmitting={state.transmitting}
                onAuthError={onAuthError}
              />
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

function ConnBadge({ conn }) {
  const label = { open: "live", connecting: "connecting…", reconnecting: "reconnecting…" }[conn];
  return (
    <span className={`conn conn-${conn}`}>
      <span className="conn-dot" aria-hidden="true" />
      {label ?? conn}
    </span>
  );
}
