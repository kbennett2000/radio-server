// `classifyTxMessage` (ADR 0161). The TX socket's one text channel, folded from a server message
// into what the operator sees — exercised directly, without a WebSocket, the way `reduceStatus` is
// in `useEvents.test.js`.
//
// Why there is a text channel at all: a browser cannot read a WebSocket close code. It sees 1006 for
// everything, so the server sends an explicit message first and closes second. That was built for
// the single-talker `busy` case; ADR 0161 gives the same treatment to a refused key-up, because
// until this cycle the reason never reached the browser at all — every AM refusal and every
// broadcast-FM refusal arrived as "Transmit connection dropped."

import { describe, it, expect } from "vitest";
import { classifyTxMessage, formatHeld } from "./useTxAudio.js";

describe("classifyTxMessage", () => {
  it("carries a refusal reason through verbatim", () => {
    // Verbatim, not summarised or mapped to a friendlier sentence. The backend wrote that string to
    // send an operator to a specific place — the Demodulation control, or the radio's own EXIT key
    // — and a client that rewords it throws away the diagnosis the whole arc exists to produce.
    const reason =
      "the radio's second receiver is running (tuned to 103.2 MHz) — it holds the speaker line";
    expect(classifyTxMessage({ status: "refused", reason })).toEqual({
      status: "error",
      rejected: "refused",
      error: reason,
    });
  });

  it("still says something useful when the server names no reason", () => {
    // A refusal with no reason is not expected, but silence in an alert box is worse than a generic
    // sentence: the operator needs to know the radio declined rather than that the page froze.
    const out = classifyTxMessage({ status: "refused" });
    expect(out.status).toBe("error");
    expect(out.error).toMatch(/refused/i);
  });

  it("falls back to the old wording when the server names no holder", () => {
    // An older server (or a claim made without a label) sends a bare `busy`. Fall back rather than
    // invent — "held by unknown since unknown" would replace one untrue sentence with another. The
    // `busy` classification itself must never collapse into the refusal wording: different answer,
    // different remedy.
    expect(classifyTxMessage({ status: "busy" })).toEqual({
      status: "busy",
      rejected: "busy",
      error: "Radio busy — another operator is transmitting.",
    });
  });

  it("names the holder and how long it has been held", () => {
    // The lie this cycle fixes. "Another operator is transmitting" is FALSE during a leak, when a
    // dead socket owns the flag and nobody is on the air (ADR 0167's carried finding, ADR 0170).
    const out = classifyTxMessage({
      status: "busy",
      slot: "tx",
      holder: "browser",
      held_s: 12.4,
      stale_after_s: 180,
    });
    expect(out.status).toBe("busy");
    expect(out.error).toBe("Radio busy — browser has held the transmit slot for 12s.");
  });

  it("reads differently once the hold is longer than any legal over", () => {
    // Past the transmitter time-out no legitimate holder exists, so the phrasing CHANGES rather than
    // just carrying a bigger number: "held for 4h 20m" only reads as wrong to somebody who already
    // knows what normal looks like, and the operator staring at a stuck Talk button does not.
    const out = classifyTxMessage({
      status: "busy",
      slot: "tx",
      holder: "mumble-relay",
      held_s: 15600,
      stale_after_s: 180,
    });
    expect(out.error).toContain("4h 20m");
    expect(out.error).toContain("mumble-relay");
    expect(out.error).toMatch(/nobody may actually be transmitting/i);
  });

  it("formats a held-for duration an operator can read at a glance", () => {
    expect(formatHeld(0)).toBe("0s");
    expect(formatHeld(12.9)).toBe("12s");
    expect(formatHeld(260)).toBe("4m 20s");
    expect(formatHeld(15600)).toBe("4h 20m");
    // Absent is absent — never "0s", which would render "no reading" as "just claimed".
    expect(formatHeld(null)).toBeNull();
    expect(formatHeld(undefined)).toBeNull();
  });

  it("marks a ready ack as talking and nothing else", () => {
    expect(classifyTxMessage({ status: "ready" })).toEqual({ status: "talking", ready: true });
  });

  it("ignores anything it does not recognise", () => {
    // A later server may send frames this build has never heard of, and guessing at them is how a
    // live over gets torn down by a message that meant nothing.
    expect(classifyTxMessage({ status: "something-new" })).toBeNull();
    expect(classifyTxMessage({})).toBeNull();
    expect(classifyTxMessage(null)).toBeNull();
  });
});
