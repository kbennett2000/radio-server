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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import TalkControl from "./TalkControl.jsx";

// A controllable stand-in for the transmit hook. Two reasons it has to be mocked rather than left
// real: TalkControl calls getUserMedia the moment it keys and jsdom has none, and the keyboard tests
// below have to observe whether a key-up was even ATTEMPTED — which is invisible in the rendered
// output, because the whole point of the bug is that the key-up succeeds silently.
const tx = vi.hoisted(() => ({
  status: "idle",
  talking: false,
  error: null,
  startTalk: vi.fn(),
  stopTalk: vi.fn(),
}));
vi.mock("../useTxAudio.js", () => ({ useTxAudio: () => tx }));

const allCaps = () => true;

function renderTalk(state, { hasCap = allCaps, ...props } = {}) {
  return render(
    <TalkControl token="t" onAuthError={() => {}} state={state} hasCap={hasCap} {...props} />,
  );
}

beforeEach(() => {
  tx.status = "idle";
  tx.talking = false;
  tx.error = null;
  vi.clearAllMocks();
});

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

// The AM lockout (ADR 0154). A second reason the radio will refuse a key-up, and a different KIND of
// reason: `tx_ready_in` is transient and counts itself down, while `tx_ok: false` is a standing
// station condition that never clears until an operator changes the demodulator.
//
// Without this, the refusal surfaces as "Transmit connection dropped." — the server sends its ready
// ack, `AiocBaofeng._key_on` refuses on the FIRST audio frame, and useTxAudio's `s.ready` branch
// reports a network fault. This lockout is the only place AM is ever named.
describe("the AM transmit lockout", () => {
  const tuned = { frequency: 145_145_000, tx_frequency: null };

  it("locks out and names the demodulator when the radio reports it will not key", () => {
    renderTalk({ ...tuned, tx_ok: false, modulation: "AM" });
    const button = screen.getByRole("button", { name: /Transmit disabled — radio is on AM/ });
    expect(button.disabled).toBe(true);
  });

  it("says what it costs and where the remedy is, not just that talk is off", () => {
    // The remedy lives in a different card from the symptom, and AM costs more than the over: the
    // station ID Part 97 requires and every voice service go with it.
    renderTalk({ ...tuned, tx_ok: false, modulation: "AM" });
    expect(screen.getByText(/Set Demodulation to FM in the Tune card/)).toBeTruthy();
    expect(screen.getByText(/station ID/)).toBeTruthy();
  });

  it("does not claim AM when the radio only said 'not FM'", () => {
    // `tx_ok: false` means non-FM. USB is a reserved future on this wire, so naming AM from the
    // flag alone would be a guess printed as a fact.
    renderTalk({ ...tuned, tx_ok: false, modulation: null });
    expect(screen.getByRole("button", { name: /radio is not on FM/ }).disabled).toBe(true);
  });

  it("does NOT lock out on an unmeasured tx_ok", () => {
    // The rule the backend uses (`_refuse_if_tx_disabled`): only a MEASURED false refuses. `null` is
    // "nobody has asked this radio", and a transmitter disabled by an unknown is a worse failure
    // than the one being prevented.
    renderTalk({ ...tuned, tx_ok: null });
    expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
  });

  it("does not lock out when the radio says it WILL key", () => {
    renderTalk({ ...tuned, tx_ok: true });
    expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
  });

  it("does not block Mumble or D-STAR, which are not the radio", () => {
    // `tx_ok` is reported by the backend whatever the browser is aiming at, so a lockout written
    // outside the `source === "rf"` guard would dead-button Talk over a radio nobody is using.
    const state = { ...tuned, tx_ok: false, modulation: "AM" };
    const { unmount } = renderTalk(state, { mumbleMode: true });
    expect(screen.getByRole("button", { name: /Hold to talk on Mumble/ }).disabled).toBe(false);
    unmount();
    renderTalk(state, { dstarMode: true });
    expect(screen.getByRole("button", { name: /Hold to talk on the reflector/ }).disabled).toBe(false);
  });

  it("does not strip the handlers out from under a live over", () => {
    // The half-state the pointer-capture comment exists to prevent: swapping to `{disabled: true}`
    // mid-hold removes onPointerUp/onLostPointerCapture, so the release fires nothing and the
    // transmitter stays keyed. `!talking` is what stops it, and it must cover BOTH reasons.
    tx.talking = true;
    renderTalk({ ...tuned, tx_ok: false, modulation: "AM" });
    const button = screen.getByRole("button", { name: /On air — release to stop/ });
    expect(button.disabled).toBe(false);
  });

  it("names the standing reason first when both reasons apply", () => {
    // The countdown clears itself; AM does not. A label that expires into a still-dead button is
    // the silent failure this cycle exists to close.
    renderTalk({ ...tuned, tx_ok: false, modulation: "AM", tx_ready_in: 4.2 });
    expect(screen.getByRole("button", { name: /Transmit disabled — radio is on AM/ })).toBeTruthy();
    expect(screen.queryByText(/Radio ready in/)).toBeNull();
  });
});

// ADR 0158. A third cause, and the only one where the radio does NOT refuse: with its second
// receiver running it transmits perfectly happily, into a channel it cannot hear. So this lockout
// is not predicting hardware the way the AM one is — it is showing the operator the server's own
// refusal before they lean on a button that would 503.
describe("the broadcast-FM transmit lockout", () => {
  const tuned = { frequency: 145_145_000, tx_frequency: null };

  it("locks out and names the second receiver, not the demodulator", () => {
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    const button = screen.getByRole("button", { name: /radio is on broadcast FM/ });
    expect(button.disabled).toBe(true);
  });

  it("does NOT lock out when the server never learned the state", () => {
    // The load-bearing direction, and the same rule as `tx_ok`: a null block is "nobody asked this
    // radio", which is every backend without a dock tuner. An unknown must never dead-button a
    // transmitter.
    renderTalk({ ...tuned, broadcast_fm: null });
    expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
  });

  it("does NOT lock out when the server verified the receiver is off", () => {
    // `hz` survives an off state — it is where the receiver WOULD resume — so a lockout keyed on
    // the frequency rather than on `on` would dead-button a perfectly healthy station.
    renderTalk({ ...tuned, broadcast_fm: { on: false, hz: 103_200_000 } });
    expect(screen.getByRole("button", { name: /Hold to talk/ }).disabled).toBe(false);
  });

  it("names deafness before the demodulator when both are wrong", () => {
    // The order the backend refuses in, for the reason the backend refuses in it: an operator told
    // only about AM would set FM and get a station that now DOES transmit and still cannot hear.
    renderTalk({
      ...tuned, tx_ok: false, modulation: "AM", broadcast_fm: { on: true, hz: 103_200_000 },
    });
    expect(screen.getByRole("button", { name: /radio is on broadcast FM/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /radio is on AM/ })).toBeNull();
  });

  it("says EXIT is the whole remedy and never asks for a restart (ADR 0161)", () => {
    // Reversed on purpose. The old sub-line told the operator to restart the server, because the
    // server read the second receiver once at startup and never again. It re-reads before every
    // key-up now, so pressing EXIT IS the whole remedy — and a message still demanding a restart
    // would send someone to reboot a station that would have worked on the next press.
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    expect(screen.getByText(/press EXIT/)).toBeTruthy();
    expect(screen.getByText(/station ID/)).toBeTruthy();
    expect(screen.queryByText(/restart the server/)).toBeNull();
  });

  it("states its real limit: it reflects the last key-up, not this instant", () => {
    // THE LIMIT, restated where the operator is, because the old one is no longer true. This button
    // still cannot see a front-panel F+0 the moment it happens — the server does not poll, for the
    // reasons ADR 0161 gives — but it is no longer blind for ever: the state is re-read and cleared
    // immediately before every key-up. The honest sentence is about staleness, not about ignorance.
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    expect(screen.getByText(/before every key-up/)).toBeTruthy();
    expect(screen.queryByText(/not proof the radio is hearing/)).toBeNull();
  });

  it("stops claiming the host is the thing refusing", () => {
    // After F9 the RADIO refuses too, so a sub-line saying the server is the obstacle is simply
    // wrong — and wrong in the direction that makes an operator distrust an interlock that is
    // working. This is the wording half of ADR 0159's own correction to ADR 0158.
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    expect(screen.queryByText(/it only checks at startup/)).toBeNull();
    expect(screen.getByText(/the radio refuses to transmit/)).toBeTruthy();
  });

  it("does not block Mumble or D-STAR, which are not the radio", () => {
    const state = { ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } };
    const { unmount } = renderTalk(state, { mumbleMode: true });
    expect(screen.getByRole("button", { name: /Hold to talk on Mumble/ }).disabled).toBe(false);
    unmount();
    renderTalk(state, { dstarMode: true });
    expect(screen.getByRole("button", { name: /Hold to talk on the reflector/ }).disabled).toBe(false);
  });

  it("does not strip the handlers out from under a live over", () => {
    // Same half-state guard as the AM lockout: `!talking` must cover every reason, or a block
    // arriving mid-hold removes the release handlers and strands the key.
    tx.talking = true;
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    expect(screen.getByRole("button", { name: /On air — release to stop/ }).disabled).toBe(false);
  });
});

// The Spacebar is a second way into the transmitter and it did not consult the lockout at all: the
// keydown listener is registered by an effect that runs above where `lockedOut` was even defined,
// and `holdProps` only ever stripped the POINTER handlers off the button. Cosmetic while the only
// lockout lasted six seconds; permanent once AM can cause one.
describe("the Spacebar respects the lockout", () => {
  const tuned = { frequency: 145_145_000, tx_frequency: null };

  it("does not key an AM-disabled radio", () => {
    renderTalk({ ...tuned, tx_ok: false, modulation: "AM" });
    fireEvent.keyDown(window, { code: "Space" });
    expect(tx.startTalk).not.toHaveBeenCalled();
  });

  it("does not key a radio that cannot hear its own channel", () => {
    // The keyboard is the bypass the button's `disabled` cannot cover, and it must cover every
    // cause. This one never expires, so a bypass here is a permanent way to transmit blind.
    renderTalk({ ...tuned, broadcast_fm: { on: true, hz: 103_200_000 } });
    fireEvent.keyDown(window, { code: "Space" });
    expect(tx.startTalk).not.toHaveBeenCalled();
  });

  it("does not key a radio still inside its post-write mute", () => {
    renderTalk({ ...tuned, tx_ready_in: 4.2 });
    fireEvent.keyDown(window, { code: "Space" });
    expect(tx.startTalk).not.toHaveBeenCalled();
  });

  it("still keys when the radio is ready", () => {
    // The guard must not cost the feature: this is the assertion that stops it being "fixed" by
    // disabling the keyboard path outright.
    renderTalk({ ...tuned, tx_ok: true });
    fireEvent.keyDown(window, { code: "Space" });
    expect(tx.startTalk).toHaveBeenCalled();
  });

  it("still releases on key-up, unconditionally", () => {
    // Deliberately NOT guarded. Refusing to stop is the dangerous direction in a transmitter, so a
    // redundant stop is correct and a missed one is a stuck key.
    tx.talking = true;
    renderTalk({ ...tuned, tx_ok: true });
    fireEvent.keyUp(window, { code: "Space" });
    expect(tx.stopTalk).toHaveBeenCalled();
  });
});
