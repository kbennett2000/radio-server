// Top-level app (ADR 0022): the token gate guards everything, then the control panel.
//
// The token lives only in this component's state — never localStorage — so a refresh re-prompts
// and nothing persists the LAN secret. `capabilities()` doubles as the token check: a good token
// returns the list (which then drives control greying); a bad one throws Unauthorized and we stay
// on the gate with a clear message.

import { useCallback, useState } from "react";
import { makeClient } from "./api.js";
import TokenGate from "./components/TokenGate.jsx";
import ControlPanel from "./components/ControlPanel.jsx";

export default function App() {
  // session = { token, caps } once authenticated, else null.
  const [session, setSession] = useState(null);
  const [gateNotice, setGateNotice] = useState(null);

  const onAuthenticated = useCallback((token, caps) => {
    setGateNotice(null);
    setSession({ token, caps });
  }, []);

  // Any Unauthorized after entry (expired/rotated token, or a WS 1008) drops back to the gate.
  const onAuthError = useCallback(() => {
    setGateNotice("Your session was rejected — re-enter the API token.");
    setSession(null);
  }, []);

  // Deliberate return to the gate after the operator rotates the API token (ADR 0027) — not an
  // error, so a friendlier notice. Same mechanism: clear the in-memory session and re-prompt.
  const onReauth = useCallback(() => {
    setGateNotice("Re-enter the API token — use the newly rotated token after restarting the server.");
    setSession(null);
  }, []);

  if (!session) {
    return <TokenGate onAuthenticated={onAuthenticated} notice={gateNotice} />;
  }

  const client = makeClient(session.token);
  return (
    <ControlPanel
      client={client}
      caps={session.caps}
      onAuthError={onAuthError}
      onReauth={onReauth}
    />
  );
}
