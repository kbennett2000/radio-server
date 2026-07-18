// reduceStatus (ADR 0022; the `capabilities` case added in ADR 0077). The pure fold from a `{type,
// data}` frame into the running status snapshot — exercised directly so the live re-grey path (a
// server-pushed capability set becoming reactive `state.caps`) is pinned without a WebSocket.

import { describe, it, expect } from "vitest";
import { reduceStatus } from "./useEvents.js";

describe("reduceStatus", () => {
  it("folds a capabilities frame into reactive state.caps (ADR 0077)", () => {
    // The event helper wires `{type:'capabilities', data:{capabilities:[...]}}`; the reducer lifts
    // the array to state.caps, which ControlPanel prefers over the one-shot login prop.
    const caps = ["ptt", "receive", "scan", "set_frequency", "set_mode", "set_tone", "status", "transmit"];
    const next = reduceStatus({ backend: "baofeng" }, { type: "capabilities", data: { capabilities: caps } });
    expect(next.caps).toEqual(caps);
    expect(next.backend).toBe("baofeng"); // unrelated slices are preserved
  });

  it("a later capabilities frame replaces the previous set (a switch back)", () => {
    const first = reduceStatus({}, { type: "capabilities", data: { capabilities: ["set_frequency", "scan"] } });
    const second = reduceStatus(first, { type: "capabilities", data: { capabilities: ["ptt", "transmit"] } });
    expect(second.caps).toEqual(["ptt", "transmit"]);
  });

  it("still folds the existing frame types (regression)", () => {
    expect(reduceStatus({ a: 1 }, { type: "status", data: { transmitting: true } })).toEqual({
      a: 1,
      transmitting: true,
    });
    expect(reduceStatus({ transmitting: false }, { type: "ptt", data: { on: true } })).toEqual({
      transmitting: true,
    });
    expect(reduceStatus({ x: 1 }, { type: "unknown", data: {} })).toEqual({ x: 1 });
  });
});
