// Live event subscription for the radio-server `/events` WebSocket (ADR 0022).
//
// One socket feeds the whole UI: every `{type, data}` frame is folded into a live `state` object
// (the status panel) and appended to a bounded `events` list (the scrolling operating log). The
// socket authenticates via the `?token=` query param (browsers can't set headers on a WS
// handshake) and reconnects with exponential backoff on any drop — except a 1008 policy close,
// which means the token was rejected: we stop and bubble an auth error back to the token gate.

import { useEffect, useRef, useState } from "react";

const MAX_EVENTS = 500; // bound the log so a long session can't grow memory without limit
const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 10000;
const WS_POLICY_VIOLATION = 1008; // bad/missing token — do not retry

// Fold one frame into the running status snapshot. The `status` frame carries the full RadioStatus
// (fields null on an audio-only backend); the narrower frames update just their slice, so the panel
// reflects ptt/scan/session/arbiter transitions the moment they happen, between status snapshots.
// Exported for unit tests (a pure function); the hook is the only runtime caller.
export function reduceStatus(prev, { type, data }) {
  switch (type) {
    case "status":
      return { ...prev, ...data };
    case "capabilities":
      // A live backend switch (ADR 0076/0077) re-emits the new capability set so the panel re-greys
      // without a reconnect. `data` is {capabilities: [...]} — fold it into reactive `state.caps`,
      // which ControlPanel prefers over the one-shot login `caps` prop.
      return { ...prev, caps: data.capabilities };
    case "ptt":
      return { ...prev, transmitting: data.on };
    case "scan":
      return { ...prev, scan: data };
    case "session":
      return {
        ...prev,
        session: data,
        sessionOpen:
          data.phase === "session_open"
            ? true
            : data.phase === "session_close"
              ? false
              : prev.sessionOpen,
      };
    case "arbiter":
      return { ...prev, arbiter: data.mode };
    case "link":
      // A Mumble link transition (ADR 0042) carries the full link block ({active, entries}) —
      // the only push channel for the link card, since status frames are RadioStatus-only. The
      // DTMF command record (via: "dtmf") has no entries and is log-only.
      return data.entries !== undefined
        ? { ...prev, link: { active: data.active, entries: data.entries } }
        : prev;
    case "dstar":
      // A D-STAR reflector link transition (ADR 0088) carries the full believed-link block
      // ({active, mode, gateway, tx, configured}) plus the {reflector, state} of the transition — the
      // only push channel for the reflector card. `activity` is folded by its own event (below), so
      // preserve it here rather than dropping it on a link transition.
      return {
        ...prev,
        dstar: {
          ...prev.dstar,
          active: data.active,
          mode: data.mode,
          gateway: data.gateway,
          tx: data.tx,
          configured: data.configured,
        },
      };
    case "activity":
      // A reflector over (ADR 0089): a callsign heard inbound (dir "rx") or our own outbound over
      // (dir "tx"). Ring-buffer the last 30 on `state.activity` (newest last) for the activity card;
      // it also lands in the raw event log for free.
      return { ...prev, activity: [...(prev.activity ?? []), data].slice(-30) };
    case "auth":
      return { ...prev, lastAuth: data.result };
    case "command":
      return { ...prev, lastCommand: data.service };
    default:
      return prev;
  }
}

function eventsUrl(token) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/events?token=${encodeURIComponent(token)}`;
}

// `conn` is one of: "connecting" | "open" | "reconnecting". An auth rejection calls `onAuthError`
// instead of reconnecting, so the caller can drop back to the token gate.
export function useEvents(token, onAuthError) {
  const [state, setState] = useState({});
  const [events, setEvents] = useState([]);
  const [conn, setConn] = useState("connecting");
  const seq = useRef(0);

  useEffect(() => {
    if (!token) return undefined;

    let disposed = false;
    let ws = null;
    let retryTimer = null;
    let backoff = BACKOFF_START_MS;

    const connect = () => {
      if (disposed) return;
      setConn((c) => (c === "open" ? "reconnecting" : c));
      ws = new WebSocket(eventsUrl(token));

      ws.onopen = () => {
        if (disposed) return;
        backoff = BACKOFF_START_MS; // reset on a healthy connection
        setConn("open");
      };

      ws.onmessage = (ev) => {
        if (disposed) return;
        let frame;
        try {
          frame = JSON.parse(ev.data);
        } catch {
          return; // ignore anything that isn't a JSON event frame
        }
        setState((prev) => reduceStatus(prev, frame));
        setEvents((prev) => {
          const next = [
            ...prev,
            { id: seq.current++, at: new Date(), type: frame.type, data: frame.data },
          ];
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
      };

      ws.onclose = (ev) => {
        if (disposed) return;
        if (ev.code === WS_POLICY_VIOLATION) {
          // Token rejected at the handshake — no amount of retrying fixes that.
          onAuthError?.();
          return;
        }
        setConn("reconnecting");
        retryTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      };

      ws.onerror = () => {
        // Let onclose drive reconnection; closing here avoids a dangling half-open socket.
        try {
          ws.close();
        } catch {
          /* already closing */
        }
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) {
        ws.onclose = null; // prevent the teardown close from scheduling a reconnect
        try {
          ws.close();
        } catch {
          /* already closed */
        }
      }
    };
  }, [token, onAuthError]);

  const clearEvents = () => setEvents([]);
  return { state, events, conn, clearEvents };
}
