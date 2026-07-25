// TalkControl's transmit-target line (ADR 0134). The field failure that prompted it: every one of
// the operator's 37 repeater presets went out on the repeater's OUTPUT because the armed split had
// been cleared, and nothing on screen said so — the only signal was a StatusPanel row that
// disappears, which an operator holding the Talk button cannot notice.
//
// So the load-bearing assertion here is the SIMPLEX one, not the SPLIT one: a card that only
// announced a split would have been exactly as silent in the failure case as the old code.
//
// TalkControl calls getUserMedia the moment you key, so these tests never key — they render and
// read the target line, which is derived purely from `state`.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import TalkControl from "./TalkControl.jsx";

const allCaps = () => true;

function renderTalk(state, { hasCap = allCaps, ...props } = {}) {
  return render(
    <TalkControl token="t" onAuthError={() => {}} state={state} hasCap={hasCap} {...props} />,
  );
}

describe("the transmit-target line", () => {
  it("names the repeater input when a split is armed", () => {
    renderTalk({ frequency: 145_145_000, tx_frequency: 144_545_000 });
    expect(screen.getByText(/SPLIT — transmits on 144\.5450 MHz/)).toBeTruthy();
  });

  it("says SIMPLEX out loud when no split is armed", () => {
    // The regression that matters: this is what a repeater preset looks like after a scan hop, a
    // manual retune or a service restart has silently cleared its TX leg.
    renderTalk({ frequency: 145_145_000, tx_frequency: null });
    expect(screen.getByText(/SIMPLEX — transmits on 145\.1450 MHz/)).toBeTruthy();
    expect(screen.queryByText(/SPLIT/)).toBeNull();
  });

  it("names no frequency while a scan is running, because it cannot know one", () => {
    // A scan retunes every hop and emits only `scan` frames, so `state.frequency` is frozen at the
    // last status snapshot. Printing that stale number is worse than printing none.
    renderTalk({ frequency: 145_145_000, tx_frequency: null, scan: { running: true } });
    expect(screen.getByText(/SIMPLEX — scanning/)).toBeTruthy();
    expect(screen.queryByText(/145\.1450/)).toBeNull();
  });

  it("stays quiet on a backend that cannot split", () => {
    renderTalk({ frequency: 145_145_000, tx_frequency: null }, { hasCap: (c) => c !== "set_split" });
    expect(screen.queryByText(/SIMPLEX|SPLIT/)).toBeNull();
  });

  it("stays quiet before the first status arrives", () => {
    renderTalk({});
    expect(screen.queryByText(/SIMPLEX|SPLIT/)).toBeNull();
  });

  it("stays quiet while Talk is aimed at Mumble or D-STAR, which do not key the radio", () => {
    const state = { frequency: 145_145_000, tx_frequency: 144_545_000 };
    const { unmount } = renderTalk(state, { mumbleMode: true });
    expect(screen.queryByText(/SPLIT|SIMPLEX/)).toBeNull();
    unmount();
    renderTalk(state, { dstarMode: true });
    expect(screen.queryByText(/SPLIT|SIMPLEX/)).toBeNull();
  });
});
