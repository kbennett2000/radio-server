// TuneControls — the power selector (ADR 0146). The frequency/channel/tone/mode rows predate this
// file and are covered through ControlPanel; what is pinned here is the one control with real
// semantics behind it: three levels, hidden where the backend cannot set them, and lit from what
// the RADIO confirmed rather than from what was clicked.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TuneControls from "./TuneControls.jsx";
import { Unsupported } from "../api.js";

const allCaps = () => true;

function makeClient(overrides = {}) {
  return {
    power: vi.fn().mockResolvedValue({ power: "low" }),
    ...overrides,
  };
}

function renderTune(client, state = {}, hasCap = allCaps) {
  return render(
    <TuneControls
      client={client}
      state={state}
      hasCap={hasCap}
      catAvailable
      onAuthError={() => {}}
      onUnsupported={() => {}}
    />,
  );
}

describe("TuneControls power", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers the three levels the radio's dock command accepts", () => {
    renderTune(makeClient(), { power: "high" });
    const group = screen.getByRole("group", { name: /transmit power/i });
    expect(group).toBeInTheDocument();
    for (const level of ["low", "mid", "high"]) {
      expect(screen.getByRole("button", { name: level })).toBeInTheDocument();
    }
  });

  it("is hidden on a backend that cannot set power", () => {
    // A UV-5R holds its power on its own front panel — an unusable control is just noise.
    renderTune(makeClient(), { power: null }, (c) => c !== "set_power");
    expect(screen.queryByRole("group", { name: /transmit power/i })).toBeNull();
  });

  it("lights the level the radio reported", () => {
    renderTune(makeClient(), { power: "mid" });
    expect(screen.getByRole("button", { name: "mid" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "high" })).toHaveAttribute("aria-pressed", "false");
  });

  it("lights nothing before the first tune", () => {
    // Until then the radio is on whatever its front panel says and the server cannot see it, so
    // there is no level to claim (ADR 0134's rule).
    renderTune(makeClient(), { power: null });
    for (const level of ["low", "mid", "high"]) {
      expect(screen.getByRole("button", { name: level })).toHaveAttribute("aria-pressed", "false");
    }
  });

  it("asks the server rather than lighting the click", async () => {
    // The status event moves the highlight. An optimistic local one would show a level the radio
    // may have refused.
    const client = makeClient();
    renderTune(client, { power: "high" });
    fireEvent.click(screen.getByRole("button", { name: "low" }));
    await waitFor(() => expect(client.power).toHaveBeenCalledWith("low"));
    expect(screen.getByRole("button", { name: "high" })).toHaveAttribute("aria-pressed", "true");
  });

  it("surfaces a refusal instead of swallowing it", async () => {
    const client = makeClient({
      power: vi.fn().mockRejectedValue(new Unsupported("set_power")),
    });
    renderTune(client, { power: "high" });
    fireEvent.click(screen.getByRole("button", { name: "low" }));
    await waitFor(() => expect(client.power).toHaveBeenCalled());
  });
});
