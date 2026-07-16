// The over-the-air login code, shown as an LCD chip in the masthead: the current TOTP code, so
// the operator can key a DTMF login (code then '#') at the radio without pulling out their phone.
//
// Posture: the LAN token already transmits directly (/ptt, the Services Transmit buttons), so
// showing the short-lived code grants the token holder no capability they don't have — and the
// SECRET is never sent (the endpoint returns only {code, seconds_remaining, interval}). Keying
// the code over RF still goes through the single-use burn like any entry.
//
// Timing: one fetch seeds {code, seconds_remaining}; a 1 s interval (the LinkPanel tick pattern)
// counts down locally and refetches when the window rolls, so the card is one tiny GET per 30 s.
// The countdown renders as a thin bar under the code (width = fraction of the window left).
// Hidden entirely when TOTP isn't configured (a 503 on the first fetch) — the
// hide-when-unconfigured pattern (ADR 0037).

import { useEffect, useRef, useState } from "react";

export default function TotpCard({ client }) {
  const [totp, setTotp] = useState(null); // {code, seconds_remaining, interval}
  const [absent, setAbsent] = useState(false); // confirmed unconfigured -> hide for good
  const fetching = useRef(false);

  useEffect(() => {
    if (absent) return undefined;
    let live = true;

    const fetchCode = () => {
      if (fetching.current) return;
      fetching.current = true;
      client
        .totpCode()
        .then((body) => {
          if (live && body?.code) setTotp(body);
        })
        .catch((e) => {
          // 503 = no TOTP secret enrolled: hide the card. Anything else (a network blip) keeps
          // the last code visible; the next roll retries.
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
        if (!prev) return prev;
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

  if (absent || totp == null) return null;

  const pct = Math.round((totp.seconds_remaining / (totp.interval || 30)) * 100);

  return (
    <span
      className="totp-chip"
      title="Over-the-air login code — key it then # on the radio to open a session"
    >
      <span className="totp-chip-row">
        <span className="totp-label">OTA code</span>
        <span className="totp-code" aria-label="current login code">
          {totp.code}
        </span>
      </span>
      <span className="totp-countdown" aria-hidden="true">
        <span className="totp-countdown-fill" style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}
