// TuneControls — the power selector (ADR 0146), and the two selects that name the radio's two
// FM-spelling settings apart (ADR 0154).
//
// This header used to claim the frequency/channel/tone/mode rows were "covered through
// ControlPanel". They were not: ControlPanel.test.jsx mocks this whole component away to a stub, so
// those rows had never had a single assertion. The bandwidth relabel below is the first.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import TuneControls from "./TuneControls.jsx";
import { Unsupported } from "../api.js";

const allCaps = () => true;

function makeClient(overrides = {}) {
  return {
    power: vi.fn().mockResolvedValue({ power: "low" }),
    mode: vi.fn().mockResolvedValue({ mode: "NFM" }),
    modulation: vi.fn().mockResolvedValue({ modulation: "AM" }),
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

// Bandwidth and Demodulation are different radio settings that both spell one value "FM" — the
// confusion Capability.SET_MODE and SET_MODULATION were split for. The card must not say "FM" twice
// and mean two things (ADR 0154).
describe("TuneControls bandwidth", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says Bandwidth, not Mode, and spells its options out", () => {
    renderTune(makeClient());
    const select = screen.getByLabelText("Bandwidth");
    expect(select).toBeInTheDocument();
    expect(screen.queryByLabelText("Mode")).toBeNull();
    expect([...select.options].map((o) => o.textContent)).toEqual(["Wide (FM)", "Narrow (NFM)"]);
  });

  it("offers only what POST /mode accepts", () => {
    // AM/USB/LSB/CW used to be here. Every real backend raises a ValueError on all four, and AM in
    // particular read as a demodulator inside the control that is NOT the demodulator.
    renderTune(makeClient());
    expect(screen.getByLabelText("Bandwidth").options).toHaveLength(2);
  });

  it("still posts the raw value the API and presets speak", async () => {
    // The assertion that stops a relabel becoming a wire change: PresetControl's active-channel
    // highlight compares `state.mode` against a preset's raw "FM"/"NFM", and radio.toml spells it
    // the same way. Prose on screen, raw value on the wire.
    const client = makeClient();
    renderTune(client);
    const select = screen.getByLabelText("Bandwidth");
    fireEvent.change(select, { target: { value: "NFM" } });
    // Scoped to this row's own form. An index into every "Set" button on the card would silently
    // retarget the moment a row is added or reordered.
    fireEvent.click(within(select.closest("form")).getByRole("button", { name: "Set" }));
    await waitFor(() => expect(client.mode).toHaveBeenCalledWith("NFM"));
  });
});

describe("TuneControls demodulation", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers the two the API accepts and posts the choice straight away", async () => {
    const client = makeClient();
    renderTune(client, { modulation: "FM" });
    const select = screen.getByLabelText("Demodulation");
    expect([...select.options].map((o) => o.value)).toEqual(["FM", "AM"]);
    fireEvent.change(select, { target: { value: "AM" } });
    await waitFor(() => expect(client.modulation).toHaveBeenCalledWith("AM"));
  });

  it("shows what the radio confirmed, not what was clicked", async () => {
    // `apply_preset` rewrites the modulation on EVERY apply, so a local draft would sit here showing
    // a stale pick while the radio had been moved out from under it. Same rule as the power row.
    const client = makeClient();
    renderTune(client, { modulation: "FM" });
    fireEvent.change(screen.getByLabelText("Demodulation"), { target: { value: "AM" } });
    await waitFor(() => expect(client.modulation).toHaveBeenCalled());
    expect(screen.getByLabelText("Demodulation").value).toBe("FM");
  });

  it("claims nothing before the server has asserted a modulation", () => {
    // `null` is "not known". The firmware seeds FM, but that is the radio's state, not this
    // server's knowledge, so the control must not display a value it was never told.
    renderTune(makeClient(), { modulation: null });
    expect(screen.getByLabelText("Demodulation").value).toBe("");
  });

  it("is greyed on a backend that cannot set one", () => {
    renderTune(makeClient(), { modulation: null }, (c) => c !== "set_modulation");
    expect(screen.getByLabelText("Demodulation").disabled).toBe(true);
  });

  it("greys itself when a 501 reveals it at runtime", async () => {
    // No new mechanism: useAction reports the capability up to ControlPanel, which drops it from
    // hasCap and the disabled prop comes back false on the next render.
    const client = makeClient({
      modulation: vi.fn().mockRejectedValue(new Unsupported("set_modulation")),
    });
    const seen = [];
    render(
      <TuneControls
        client={client}
        state={{ modulation: "FM" }}
        hasCap={allCaps}
        catAvailable
        onAuthError={() => {}}
        onUnsupported={(c) => seen.push(c)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Demodulation"), { target: { value: "AM" } });
    await waitFor(() => expect(seen).toEqual(["set_modulation"]));
  });
});
