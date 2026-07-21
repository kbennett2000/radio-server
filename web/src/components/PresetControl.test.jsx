// PresetControl (ADR 0116) — the channel-presets card. Proves the config-absence hide, that tapping
// a channel applies it by name, that a skipped honoured field is surfaced (non-silently) and a mid-TX
// 409 shows the same way a /frequency failure does, and that the active-channel highlight is DERIVED
// from live status (incl. the exactly-one-match ambiguity rule). Idioms match DvapPanel/BackendPanel
// (hand-rolled client of vi.fn(), act() to flush the mount fetch, role="alert" for errors).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import PresetControl, { activePresetName } from "./PresetControl.jsx";
import { ApiError, Unsupported } from "../api.js";

const PRESETS = [
  { name: "2m Simplex", frequency: 146_520_000, tone: null, mode: "FM", honoured: [], unsupported: [] },
  { name: "Club Output", frequency: 146_940_000, tone: 100.0, mode: "FM", honoured: [], unsupported: [] },
];

// Full-CAT: every honoured-field test returns true (kv4p/uvk5/mock all advertise these).
const allCaps = () => true;

function makeClient(presets = PRESETS, overrides = {}) {
  return {
    presets: vi.fn().mockResolvedValue({ presets }),
    applyPreset: vi.fn().mockResolvedValue({ applied: ["set_frequency", "set_mode"], skipped: [] }),
    ...overrides,
  };
}

function renderCard(client, state = {}, hasCap = allCaps) {
  return render(
    <PresetControl client={client} state={state} hasCap={hasCap} onAuthError={() => {}} onUnsupported={() => {}} />,
  );
}

describe("PresetControl", () => {
  beforeEach(() => vi.clearAllMocks());

  it("hides when no presets are configured", async () => {
    const client = makeClient([]);
    const { container } = renderCard(client, {});
    await act(async () => {});
    expect(container.querySelector(".card")).toBeNull();
  });

  it("renders a button per configured preset", async () => {
    renderCard(makeClient(), {});
    expect(await screen.findByRole("button", { name: "2m Simplex" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Club Output" })).toBeInTheDocument();
  });

  it("applies a preset by name when its button is tapped", async () => {
    const client = makeClient();
    renderCard(client, {});
    const btn = await screen.findByRole("button", { name: "Club Output" });
    fireEvent.click(btn);
    await waitFor(() => expect(client.applyPreset).toHaveBeenCalledWith("Club Output"));
  });

  it("surfaces skipped honoured fields from the apply response (non-silent)", async () => {
    const client = makeClient(PRESETS, {
      applyPreset: vi.fn().mockResolvedValue({
        applied: ["set_frequency", "set_mode"],
        skipped: [{ field: "tone", capability: "set_tone" }],
        status: {},
      }),
    });
    renderCard(client, {});
    const btn = await screen.findByRole("button", { name: "Club Output" });
    fireEvent.click(btn);
    expect(await screen.findByText(/tone not supported/i)).toBeInTheDocument();
  });

  it("shows a mid-TX 409 the same way a /frequency failure shows", async () => {
    const client = makeClient(PRESETS, {
      applyPreset: vi
        .fn()
        .mockRejectedValue(new ApiError("Request failed (409): cannot apply a preset while transmitting", 409)),
    });
    renderCard(client, {});
    const btn = await screen.findByRole("button", { name: "2m Simplex" });
    fireEvent.click(btn);
    expect(await screen.findByRole("alert")).toHaveTextContent(/while transmitting/i);
  });

  it("greys the offending control on a 501 (audio-only backend)", async () => {
    const onUnsupported = vi.fn();
    const client = makeClient(PRESETS, {
      applyPreset: vi.fn().mockRejectedValue(new Unsupported("set_frequency")),
    });
    render(
      <PresetControl
        client={client}
        state={{}}
        hasCap={allCaps}
        onAuthError={() => {}}
        onUnsupported={onUnsupported}
      />,
    );
    const btn = await screen.findByRole("button", { name: "2m Simplex" });
    fireEvent.click(btn);
    await waitFor(() => expect(onUnsupported).toHaveBeenCalledWith("set_frequency"));
  });

  it("highlights the preset whose fields match the live tuned state", async () => {
    renderCard(makeClient(), { frequency: 146_940_000, mode: "FM", tone: 100.0 });
    const active = await screen.findByRole("button", { name: "Club Output" });
    expect(active).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "2m Simplex" })).toHaveAttribute("aria-pressed", "false");
  });

  it("highlights nothing when the tuned state matches no preset (tune-away clears it)", async () => {
    renderCard(makeClient(), { frequency: 145_000_000, mode: "FM", tone: null });
    const btn = await screen.findByRole("button", { name: "2m Simplex" });
    expect(btn).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Club Output" })).toHaveAttribute("aria-pressed", "false");
  });
});

describe("activePresetName (derivation)", () => {
  it("returns the sole matching preset name", () => {
    expect(activePresetName(PRESETS, { frequency: 146_520_000, mode: "FM", tone: null }, allCaps)).toBe(
      "2m Simplex",
    );
  });

  it("returns null on ambiguity — two tone-less presets on the same frequency", () => {
    const dupes = [
      { name: "A", frequency: 146_520_000, tone: null, mode: "FM" },
      { name: "B", frequency: 146_520_000, tone: null, mode: "FM" },
    ];
    expect(activePresetName(dupes, { frequency: 146_520_000, mode: "FM", tone: null }, allCaps)).toBeNull();
  });

  it("ignores tone/mode the backend cannot honour", () => {
    // A backend advertising only set_frequency: two presets differing only in tone both match on
    // frequency alone → ambiguous → no highlight.
    const freqOnly = (cap) => cap === "set_frequency";
    const presets = [
      { name: "No tone", frequency: 146_520_000, tone: null, mode: "FM" },
      { name: "With tone", frequency: 146_520_000, tone: 100.0, mode: "FM" },
    ];
    expect(activePresetName(presets, { frequency: 146_520_000, mode: "FM", tone: null }, freqOnly)).toBeNull();
    // A single preset on that frequency matches on frequency alone.
    expect(activePresetName([presets[1]], { frequency: 146_520_000 }, freqOnly)).toBe("With tone");
  });

  it("returns null when the radio has no tuned frequency yet", () => {
    expect(activePresetName(PRESETS, {}, allCaps)).toBeNull();
  });
});
