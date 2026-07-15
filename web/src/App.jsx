// Top-level app (ADR 0022; ADR 0037 adds opt-in token persistence): the token gate guards
// everything, then the control panel.
//
// By default the token lives only in memory, so a refresh re-prompts and nothing persists the LAN
// secret. When the operator ticks "Remember on this device" at the gate, the token is stored in
// localStorage and reused for silent re-auth on the next load — a deliberate convenience for a
// trusted home machine (the token is a LAN bearer credential and everything is already in the clear
// over RF). "Log out" forgets it. `capabilities()` doubles as the token check: a good token returns
// the list (which then drives control greying); a bad one throws Unauthorized.

import { useCallback, useEffect, useState } from "react";
import { makeClient, Unauthorized } from "./api.js";
import TokenGate from "./components/TokenGate.jsx";
import ControlPanel from "./components/ControlPanel.jsx";

const TOKEN_KEY = "radio.token"; // localStorage key for the opt-in remembered token

export default function App() {
  // session = { token, caps } once authenticated, else null.
  const [session, setSession] = useState(null);
  const [gateNotice, setGateNotice] = useState(null);
  // While we try a remembered token on load, hold the gate back so it doesn't flash before silent
  // auth resolves. Starts true only when there is something stored to try.
  const [restoring, setRestoring] = useState(() => !!readStoredToken());

  const onAuthenticated = useCallback((token, caps, remember) => {
    setGateNotice(null);
    if (remember) storeToken(token);
    else clearToken();
    setSession({ token, caps });
  }, []);

  // Any Unauthorized after entry (expired/rotated token, or a WS 1008) drops back to the gate. A
  // remembered token that just got rejected is stale — forget it so we don't loop on it.
  const onAuthError = useCallback(() => {
    clearToken();
    setGateNotice("Your session was rejected — re-enter the API token.");
    setSession(null);
  }, []);

  // Deliberate return to the gate after the operator rotates the API token (ADR 0027) — not an
  // error, so a friendlier notice. Same mechanism: clear the in-memory session and re-prompt.
  const onReauth = useCallback(() => {
    clearToken();
    setGateNotice("Re-enter the API token — use the newly rotated token after restarting the server.");
    setSession(null);
  }, []);

  const onLogout = useCallback(() => {
    clearToken();
    setGateNotice(null);
    setSession(null);
  }, []);

  // On load, try a remembered token exactly once. Success lands straight in the panel; failure clears
  // the stale token and falls to the gate.
  useEffect(() => {
    const stored = readStoredToken();
    if (!stored) return;
    let live = true;
    makeClient(stored)
      .capabilities()
      .then((caps) => {
        if (!live) return;
        setSession({ token: stored, caps });
      })
      .catch((err) => {
        if (!live) return;
        clearToken();
        if (err instanceof Unauthorized) {
          setGateNotice("Saved token was rejected — enter the current API token.");
        }
      })
      .finally(() => {
        if (live) setRestoring(false);
      });
    return () => {
      live = false;
    };
  }, []);

  if (!session) {
    if (restoring) return <div className="gate" />; // brief; avoids a gate flash during silent auth
    return <TokenGate onAuthenticated={onAuthenticated} notice={gateNotice} />;
  }

  const client = makeClient(session.token);
  return (
    <ControlPanel
      client={client}
      caps={session.caps}
      onAuthError={onAuthError}
      onReauth={onReauth}
      onLogout={onLogout}
    />
  );
}

// --- remembered-token storage (guarded so a disabled/private-mode localStorage never crashes) ---

function readStoredToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage unavailable — the session still works in-memory for this tab */
  }
}

function clearToken() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}
