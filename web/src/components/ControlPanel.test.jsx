// ControlPanel capability re-greying (ADR 0077). The payoff of the backend-switch arc: when the
// server re-emits a `capabilities` event over /events, the tuning/scan cards must appear or vanish
// live — no reconnect. We mock `useEvents` (the reactive `state` seam) and the heavy child panels
// (audio/fetch), then drive `state.caps` through a switch and back and assert ControlPanel's
// render gates follow. That the reactive `state.caps` is preferred over the one-shot `caps` prop is
// exactly what this proves: the prop stays the audio-only set the whole test.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// A visible marker per CAT card so we can assert mount/unmount; everything else the panel renders is
// stubbed to nothing (they'd otherwise open audio sockets or fetch on mount).
vi.mock("../useEvents.js", () => ({ useEvents: () => mockEvents }));
vi.mock("./TuneControls.jsx", () => ({ default: () => <div data-testid="tune-controls" /> }));
vi.mock("./ScanControl.jsx", () => ({ default: () => <div data-testid="scan-control" /> }));
vi.mock("./BackendPanel.jsx", () => ({ default: () => null }));
vi.mock("./ListenControl.jsx", () => ({ default: () => null }));
vi.mock("./TalkControl.jsx", () => ({ default: () => null }));
vi.mock("./ServicesView.jsx", () => ({ default: () => null }));
vi.mock("./LinkPanel.jsx", () => ({ default: () => null }));
vi.mock("./StatusPanel.jsx", () => ({ default: () => null }));
vi.mock("./EventLog.jsx", () => ({ default: () => null }));
vi.mock("./TotpCard.jsx", () => ({ default: () => null }));
vi.mock("./ThemeToggle.jsx", () => ({ default: () => null }));
vi.mock("./SettingsView.jsx", () => ({ default: () => null }));
vi.mock("./SecureContextNotice.jsx", () => ({ default: () => null }));

import ControlPanel from "./ControlPanel.jsx";

// The mutable value the mocked useEvents returns; the test reassigns `.state` and rerenders.
let mockEvents = { state: {}, events: [], conn: "open", clearEvents: () => {} };

const AUDIO_ONLY = ["ptt", "receive", "status", "transmit"]; // AIOC UV-5R
const CAT = [...AUDIO_ONLY, "scan", "set_frequency", "set_mode", "set_tone"]; // kv4p

const client = {
  token: "t",
  settings: vi.fn().mockResolvedValue({ settings: [] }),
  linkStatus: vi.fn().mockResolvedValue({}),
  dstarStatus: vi.fn().mockResolvedValue({}),
};

function renderWith(state) {
  mockEvents = { state, events: [], conn: "open", clearEvents: () => {} };
  // The one-shot login prop is the audio-only set for the whole test — so any CAT card that appears
  // came from the reactive event, not the prop.
  return render(<ControlPanel client={client} caps={AUDIO_ONLY} />);
}

describe("ControlPanel capability re-greying", () => {
  it("re-greys the CAT tuning/scan cards live as capabilities are re-emitted", () => {
    // Initial connect: no capabilities event yet, so the reactive set is empty and the prop (audio
    // only) governs — no CAT cards.
    const { rerender } = renderWith({});
    expect(screen.queryByTestId("tune-controls")).toBeNull();
    expect(screen.queryByTestId("scan-control")).toBeNull();

    // Switch to the kv4p (CAT): the re-emitted capabilities event lands in state.caps and both cards
    // mount — without a reconnect.
    mockEvents = { state: { caps: CAT }, events: [], conn: "open", clearEvents: () => {} };
    rerender(<ControlPanel client={client} caps={AUDIO_ONLY} />);
    expect(screen.getByTestId("tune-controls")).toBeInTheDocument();
    expect(screen.getByTestId("scan-control")).toBeInTheDocument();

    // Switch back to the audio-only radio: the cards vanish again.
    mockEvents = { state: { caps: AUDIO_ONLY }, events: [], conn: "open", clearEvents: () => {} };
    rerender(<ControlPanel client={client} caps={AUDIO_ONLY} />);
    expect(screen.queryByTestId("tune-controls")).toBeNull();
    expect(screen.queryByTestId("scan-control")).toBeNull();
  });
});
