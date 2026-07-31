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
import { classifyTxMessage } from "./useTxAudio.js";

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

  it("keeps the busy case exactly as it was", () => {
    // Regression, not evidence. `busy` is a different answer with a different remedy (wait for the
    // other operator), and it must never collapse into the refusal wording.
    expect(classifyTxMessage({ status: "busy" })).toEqual({
      status: "busy",
      rejected: "busy",
      error: "Radio busy — another operator is transmitting.",
    });
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
