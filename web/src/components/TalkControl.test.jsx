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
import { act, render, screen } from "@testing-library/react";
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

// The serial TX lockout (ADR 0144). A UV-K5 mutes its transmitter for six seconds after the channel
// is written to its memory — it refuses the key-up outright and cuts an over already in progress.
// The server waits it out regardless; this stops the operator being INVITED into a press that will
// do nothing, which is the same fault as a 500 with no reason: the control looks live, nothing
// happens, and nothing says why.
describe("the transmit lockout", () => {
  const tuned = { frequency: 145_145_000, tx_frequency: null };

  it("disables the button and says how long, rather than greying it silently", () => {
    renderTalk({ ...tuned, tx_ready_in: 4.2 });
    const button = screen.getByRole("button", { name: /Radio ready in 5s/ });
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/mutes its transmitter briefly/)).toBeTruthy();
  });

  it("leaves the button live when the radio is ready", () => {
    renderTalk({ ...tuned, tx_ready_in: null });
    const button = screen.getByRole("button", { name: /Hold to talk/ });
    expect(button.disabled).toBe(false);
    expect(screen.queryByText(/Radio ready in/)).toBeNull();
  });

  it("leaves the button live on a backend that reports no lockout at all", () => {
    // Every backend but the dock-tuned UV-K5 omits the field. Absent must never read as blocked.
    renderTalk(tuned);
    expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
  });

  it("does not block Mumble, which is not the radio", () => {
    // A muted UV-K5 says nothing about a link that never touches RF.
    renderTalk({ ...tuned, tx_ready_in: 4.2 }, { mumbleMode: true });
    expect(screen.getByRole("button", { name: /Hold to talk on Mumble/ }).disabled).toBe(false);
  });

  it("re-enables itself without waiting to be told", () => {
    // Nothing pushes another status event while the radio sits there muted, so a button waiting
    // for permission would simply stay dead.
    vi.useFakeTimers();
    try {
      renderTalk({ ...tuned, tx_ready_in: 1 });
      expect(screen.getByRole("button", { name: /Radio ready in 1s/ }).disabled).toBe(true);
      act(() => vi.advanceTimersByTime(1500));
      expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
