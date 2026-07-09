// Shared async-action hook for the control buttons (ADR 0022).
//
// Every control does the same dance: disable while a request is in flight, show the result or the
// error, and route the two special API errors — an expired token drops back to the gate, a 501
// greys the offending CAT control (defensive backup to the capability list). Centralising it keeps
// each control component to just its inputs and its one endpoint call.

import { useCallback, useState } from "react";
import { Unauthorized, Unsupported } from "./api.js";

export function useAction({ onAuthError, onUnsupported } = {}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [ok, setOk] = useState(false);

  const run = useCallback(
    async (fn) => {
      setPending(true);
      setError(null);
      setOk(false);
      try {
        const result = await fn();
        setOk(true);
        return result;
      } catch (e) {
        if (e instanceof Unauthorized) {
          onAuthError?.();
        } else if (e instanceof Unsupported) {
          onUnsupported?.(e.capability);
          setError(e.message);
        } else {
          setError(e.message);
        }
        return undefined;
      } finally {
        setPending(false);
      }
    },
    [onAuthError, onUnsupported],
  );

  return { run, pending, error, ok };
}
