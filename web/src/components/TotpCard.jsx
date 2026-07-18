// The over-the-air login code, shown as an LCD chip in the masthead: the current TOTP code, so
// the operator can key a DTMF login (code then '#') at the radio without pulling out their phone.
// The chip is also a button (ADR 0046): clicking it opens the OTA session directly — same on-air
// effect as keying the code (welcome announcement, station ID armed), but the LAN token is the
// credential so no code is burned. `sessionOpen` lights the chip while a session is live.
//
// Posture: the LAN token already transmits directly (/ptt, the Services Transmit buttons), so
// showing the short-lived code grants the token holder no capability they don't have — and the
// SECRET is never sent (the endpoint returns only {code, seconds_remaining, interval}). Keying
// the code over RF still goes through the single-use burn like any entry.
//
// When TOTP auth is turned OFF (ADR 0048), `/auth/totp` reports {enforced: false}: there is no
// login code, so instead of the code chip we show an "un-gated" indicator with an open padlock —
// the scary tell that anyone in range can key the radio. It reflects the RUNNING controller state.
//
// Timing: one fetch seeds {code, seconds_remaining}; a 1 s interval (the LinkPanel tick pattern)
// counts down locally and refetches when the window rolls, so the card is one tiny GET per 30 s.
// The countdown renders as a thin bar under the code (width = fraction of the window left).
// Hidden entirely when TOTP is enforced but no secret is enrolled (a 503 on the first fetch) — the
// hide-when-unconfigured pattern (ADR 0037).

import { useEffect, useRef, useState } from "react";

// An open padlock — the "un-gated" tell. Stroke follows currentColor so it themes with the chip.
function UnlockIcon() {
  return (
    <svg
      className="totp-unlock-icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 7.4-2.1" />
    </svg>
  );
}

// A closed padlock — the "fixed login code in use" tell (ADR 0083). The code itself is never shown
// (it's write-only), so the chip only signals that a static code is required.
function LockIcon() {
  return (
    <svg
      className="totp-lock-icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

export default function TotpCard({ client, sessionOpen = false }) {
  const [totp, setTotp] = useState(null); // {code, seconds_remaining, interval}
  const [ungated, setUngated] = useState(false); // TOTP auth is off — show the un-gated indicator
  const [fixed, setFixed] = useState(false); // a fixed login code is in use (ADR 0083) — no rotating code
  const [absent, setAbsent] = useState(false); // confirmed unconfigured -> hide for good
  const [pending, setPending] = useState(false); // an openSession POST in flight
  const fetching = useRef(false);

  const openSession = () => {
    if (pending) return;
    setPending(true);
    client
      .openSession()
      .catch(() => {
        // A 503 (no controller) or a blip: nothing to show here — the chip stays a code
        // display, and the StatusPanel/session events reflect whatever actually happened.
      })
      .finally(() => setPending(false));
  };

  useEffect(() => {
    if (absent) return undefined;
    let live = true;

    const fetchCode = () => {
      if (fetching.current) return;
      fetching.current = true;
      client
        .totpCode()
        .then((body) => {
          if (!live) return;
          if (body?.enforced === false) {
            // Auth is off — no code; show the un-gated indicator.
            setUngated(true);
            setFixed(false);
            setTotp(null);
          } else if (body?.fixed) {
            // A fixed login code is in use (ADR 0083): the code is write-only, so show a locked
            // indicator instead of a rotating code/countdown.
            setUngated(false);
            setFixed(true);
            setTotp(null);
          } else if (body?.code) {
            setUngated(false);
            setFixed(false);
            setTotp(body);
          }
        })
        .catch((e) => {
          // 503 = TOTP enforced but no secret enrolled: hide the card. Anything else (a network
          // blip) keeps the last state visible; the next roll retries.
          if (live && e?.status === 503) setAbsent(true);
        })
        .finally(() => {
          fetching.current = false;
        });
    };

    fetchCode();
    const id = setInterval(() => {
      if (!live) return;
      setTotp((prev) => {
        // No live code (un-gated, or not yet loaded): re-poll occasionally so a restart that flips
        // enforcement is picked up without a page reload.
        if (!prev) {
          fetchCode();
          return prev;
        }
        const remaining = prev.seconds_remaining - 1;
        if (remaining <= 0) {
          fetchCode(); // the window rolled — get the fresh code
          return { ...prev, seconds_remaining: 0 };
        }
        return { ...prev, seconds_remaining: remaining };
      });
    }, 1000);

    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client, absent]);

  if (absent) return null;

  if (ungated) {
    return (
      <div
        className="totp-chip totp-chip-unlocked"
        role="status"
        aria-label="Over-the-air auth is off — DTMF commands are un-gated"
        title="TOTP auth is off — anyone in range can key the radio with DTMF (no login required)"
      >
        <span className="totp-chip-row">
          <UnlockIcon />
          <span className="totp-label totp-label-ungated">no auth</span>
        </span>
      </div>
    );
  }

  if (fixed) {
    // A fixed login code is required (ADR 0083). Still a button so the operator can open a session
    // from the UI (the LAN token is the credential); the code itself is never shown.
    return (
      <button
        type="button"
        className={`totp-chip totp-chip-fixed${sessionOpen ? " totp-chip-open" : ""}`}
        onClick={openSession}
        disabled={pending}
        aria-label="Fixed over-the-air login code — click to open a session"
        title={
          sessionOpen
            ? "OTA session open — click to keep it alive"
            : "A fixed login code is set — key it then # on the radio, or click to open a session now"
        }
      >
        <span className="totp-chip-row">
          <LockIcon />
          <span className="totp-label">{sessionOpen ? "session" : "fixed code"}</span>
        </span>
      </button>
    );
  }

  if (totp == null) return null;

  const pct = Math.round((totp.seconds_remaining / (totp.interval || 30)) * 100);

  return (
    <button
      type="button"
      className={`totp-chip${sessionOpen ? " totp-chip-open" : ""}`}
      onClick={openSession}
      disabled={pending}
      aria-label="Open an over-the-air session"
      title={
        sessionOpen
          ? "OTA session open — click to keep it alive"
          : "Over-the-air login code — key it then # on the radio, or click to open a session now"
      }
    >
      <span className="totp-chip-row">
        <span className="totp-label">{sessionOpen ? "session" : "OTA code"}</span>
        <span className="totp-code" aria-label="current login code">
          {totp.code}
        </span>
      </span>
      <span className="totp-countdown" aria-hidden="true">
        <span className="totp-countdown-fill" style={{ width: `${pct}%` }} />
      </span>
    </button>
  );
}
