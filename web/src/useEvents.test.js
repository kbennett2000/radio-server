// reduceStatus (ADR 0022; the `capabilities` case added in ADR 0077). The pure fold from a `{type,
// data}` frame into the running status snapshot — exercised directly so the live re-grey path (a
// server-pushed capability set becoming reactive `state.caps`) is pinned without a WebSocket.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { reduceStatus, useEvents } from "./useEvents.js";

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

// The claim ADR 0180's whole design rests on, pinned where it actually lives.
//
// The server drops a subscriber that falls behind, tells it why in a frame, and closes 1013. That is
// only the right trade because THIS client comes back: `/events` sends a full `status` snapshot on
// connect, so a reconnect is a resync and the cost of being dropped is about a second. If the hook
// ever stopped retrying a 1013 the server-side design would silently become "the UI goes dark".
describe("useEvents reconnects after the server drops a slow subscriber (ADR 0180)", () => {
  let sockets;

  class FakeSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      sockets.push(this);
    }
    close() {
      this.readyState = 3;
    }
  }

  beforeEach(() => {
    sockets = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("retries a 1013 close and does not treat it as an auth failure", () => {
    const onAuthError = vi.fn();
    const { unmount } = renderHook(() => useEvents("tok", onAuthError));
    expect(sockets).toHaveLength(1);

    act(() => sockets[0].onopen());
    // What the server does to a subscriber it gave up on: the readable notice, then 1013.
    act(() => sockets[0].onmessage({ data: JSON.stringify({ type: "overflow", data: { missed: 161 } }) }));
    act(() => sockets[0].onclose({ code: 1013 }));

    expect(onAuthError).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1000)); // BACKOFF_START_MS — recovery is ~1s, not a session
    expect(sockets).toHaveLength(2);

    unmount();
  });

  it("still refuses to retry a 1008, which no reconnect can fix", () => {
    const onAuthError = vi.fn();
    const { unmount } = renderHook(() => useEvents("tok", onAuthError));
    act(() => sockets[0].onopen());
    act(() => sockets[0].onclose({ code: 1008 }));

    expect(onAuthError).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(60000));
    expect(sockets).toHaveLength(1); // no retry, ever

    unmount();
  });

  it("the overflow notice lands in the operating log rather than mutating status", () => {
    // `reduceStatus` has no `overflow` case on purpose: it is a fact about this socket, not about
    // the radio, and folding it into `state` would put a transport event in the status panel.
    const before = { transmitting: true, frequency: 145145000 };
    expect(reduceStatus(before, { type: "overflow", data: { missed: 161 } })).toBe(before);
  });
});
