// Talk-slot occupancy for the Status card (ADR 0170).
//
// **Why a poll and not an event.** A stranded slot is a STANDING condition, not an edge. The `busy`
// event fires only when somebody is refused — which is precisely the operator action this row exists
// to make unnecessary: before ADR 0170 a leak was invisible until a human pressed Talk and got a
// sentence that was false. And WS `status` frames are RadioStatus-only by design, so they carry no
// `slots` block. That leaves seed-on-mount plus a light poll, the shape `DvapPanel` already uses for
// the same reason.
//
// Failures are swallowed on purpose. This is a diagnostic row; a `/status` hiccup must never put an
// error in front of an operator who is trying to key.

import { useEffect, useState } from "react";

const POLL_MS = 5000;

export default function useSlots(client) {
  const [slots, setSlots] = useState(null);
  const [rxDemand, setRxDemand] = useState(null);

  useEffect(() => {
    // A client without `status` is a partial/stub one (several component tests build exactly that).
    // Report nothing rather than throwing out of an effect: this row is diagnostic, and a card that
    // crashes the panel it is reporting on has made the station less observable, not more.
    if (typeof client?.status !== "function") return undefined;
    let live = true;
    const read = () =>
      client
        .status()
        .then((body) => {
          if (!live) return;
          // `undefined` (an older server with no such block) and `null` (a subsystem that is not
          // configured) are different facts, and the card renders them differently — so this passes
          // through whatever it got rather than coercing one into the other.
          setSlots(body?.slots ?? null);
          setRxDemand(body?.rx_demand ?? null);
        })
        .catch(() => {});
    read();
    const id = setInterval(read, POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [client]);

  return { slots, rxDemand };
}
