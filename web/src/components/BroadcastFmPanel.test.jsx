// BroadcastFmPanel — the SECOND RECEIVER card (ADR 0164).
//
// This is the first control in the arc that can make the station worse on purpose, so the tests are
// mostly about what the operator is told and when. Three rules, and each has a reason that is not
// "it looks nicer":
//
//  1. The three consequences are in the DOM BEFORE any click, always — not behind a disclosure and
//     not in a modal. A control that hides what it does is worse than no control.
//  2. Consequence 2 is worded to the measurement. ADR 0163's M3 keyed a witness on the station's own
//     channel with broadcast FM selected and recovered its 1000 Hz tone at power 0.995: the radio
//     HEARS real overs, and the links are muted anyway because the probe cannot tell the difference.
//     "The station is deaf" is the thing M3 disproved, so it must not appear.
//  3. The repurposed-keypad warning belongs where broadcast FM is shown ACTIVE, not in the
//     pre-commit copy. The person it protects is whoever walks up to the radio later and never saw
//     the confirm; showing it at commit time shows it to the one person who does not need it.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import BroadcastFmPanel from "./BroadcastFmPanel.jsx";

const allCaps = () => true;

function makeClient(overrides = {}) {
  return {
    broadcastFm: vi.fn().mockResolvedValue({ broadcast_fm: { on: true, hz: 104300000, band: 0 } }),
    ...overrides,
  };
}

function renderPanel(client = makeClient(), broadcastFm = { on: false, hz: null }, hasCap = allCaps) {
  return render(
    <BroadcastFmPanel
      client={client}
      broadcastFm={broadcastFm}
      hasCap={hasCap}
      onAuthError={() => {}}
      onUnsupported={() => {}}
    />,
  );
}

const arm = () => screen.getByRole("button", { name: /turn on broadcast fm/i });

describe("BroadcastFmPanel — what the operator is agreeing to", () => {
  beforeEach(() => vi.clearAllMocks());

  it("states all three consequences before anything is clicked", () => {
    renderPanel();
    const notice = screen.getByRole("note", { name: /turning this on/i });
    expect(notice).toHaveTextContent(/both links go silent/i);
    expect(notice).toHaveTextContent(/withheld/i);
    expect(notice).toHaveTextContent(/station id/i);
  });

  it("says the radio still hears the overs it withholds, and never says the station is deaf", () => {
    // ADR 0163 M3. The mute acts on "broadcast FM is selected", which is coarser than "cannot hear",
    // and softening it to "deaf" would put a claim in front of the operator that the bench refuted.
    renderPanel();
    const notice = screen.getByRole("note", { name: /turning this on/i });
    expect(notice.textContent).toMatch(/even though the radio hears them/i);
    expect(notice.textContent).not.toMatch(/deaf/i);
  });

  it("tells the operator a transmission takes the receiver back, not that transmit is refused", () => {
    // `_clear_if_deafened` runs FIRST in `_key_on`, so by the time PTT asserts the F9 interlock has
    // nothing to interlock. "You cannot transmit" would be wrong and would leave an operator
    // wondering why their over went out anyway.
    renderPanel();
    const notice = screen.getByRole("note", { name: /turning this on/i });
    expect(notice.textContent).toMatch(/turns it back off/i);
  });

  it("does not fire the request on the first click", () => {
    const client = makeClient();
    renderPanel(client);
    fireEvent.click(arm());
    expect(client.broadcastFm).not.toHaveBeenCalled();
  });

  it("fires it on the confirm, with the frequency and band that were entered", async () => {
    const client = makeClient();
    renderPanel(client);
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: "98.5" } });
    fireEvent.change(screen.getByLabelText(/band/i), { target: { value: "1" } });
    fireEvent.click(arm());
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() =>
      expect(client.broadcastFm).toHaveBeenCalledWith({
        action: "on",
        hz: 98500000,
        band: 1,
      }),
    );
  });

  it("disarms itself so a stale armed button cannot be clicked much later", async () => {
    vi.useFakeTimers();
    try {
      const client = makeClient();
      render(
        <BroadcastFmPanel
          client={client}
          broadcastFm={{ on: false, hz: null }}
          hasCap={allCaps}
          onAuthError={() => {}}
          onUnsupported={() => {}}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /turn on broadcast fm/i }));
      expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(10000));
      expect(screen.queryByRole("button", { name: /confirm/i })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("BroadcastFmPanel — while it is running", () => {
  beforeEach(() => vi.clearAllMocks());

  it("warns about the repurposed keypad only while broadcast FM is active", () => {
    // ADR 0160 item 6, photographed: digits go into direct frequency entry on the BROADCAST band
    // (`147.-` typed under an `87.5-108M` band line) and `M` opens a save-to-channel prompt. The
    // keypad is not locked, it is repurposed — arguably worse, because an operator typing a
    // frequency believes they are moving the station.
    const { rerender } = renderPanel(makeClient(), { on: false, hz: null });
    expect(screen.queryByRole("alert")).toBeNull();

    rerender(
      <BroadcastFmPanel
        client={makeClient()}
        broadcastFm={{ on: true, hz: 104300000, band: 0 }}
        hasCap={allCaps}
        onAuthError={() => {}}
        onUnsupported={() => {}}
      />,
    );
    const warning = screen.getByRole("alert");
    expect(warning.textContent).toMatch(/keypad/i);
    expect(warning.textContent).toMatch(/save/i);
  });

  it("says only what ADR 0160 measured about the M key, and does not claim an overwrite", () => {
    // The bench photographed the `CH-01` / `SAVE?` PROMPT. Whether confirming it overwrites a stored
    // channel was never measured, and guardrail 1 says a hardware fact is not asserted from memory.
    renderPanel(makeClient(), { on: true, hz: 104300000, band: 0 });
    expect(screen.getByRole("alert").textContent).not.toMatch(/overwrit/i);
  });

  it("offers a way out with no confirm step", () => {
    // Leaving broadcast FM is always safe: it gives the station its ears back and un-mutes both
    // links. Arming a button in front of the remedy is how ADR 0161 finding 2 stayed open.
    const client = makeClient();
    renderPanel(client, { on: true, hz: 104300000, band: 0 });
    fireEvent.click(screen.getByRole("button", { name: /turn (it )?off/i }));
    return waitFor(() =>
      expect(client.broadcastFm).toHaveBeenCalledWith({ action: "off" }),
    );
  });

  it("shows where the receiver is, so the operator can see which station it took", () => {
    renderPanel(makeClient(), { on: true, hz: 104300000, band: 0 });
    expect(screen.getByText(/104\.3/)).toBeInTheDocument();
  });
});

describe("BroadcastFmPanel — tri-state and capabilities", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not render 'never asked' as 'off'", () => {
    // `null` is "this server has never learned whether the second receiver is running" — every
    // pre-F8 radio and every backend without a dock tuner. Rendering it as OFF is how a deaf station
    // gets trusted (ADR 0157), and it is the one claim this card must never make on no evidence.
    renderPanel(makeClient(), null);
    expect(screen.getByText(/not been asked|unknown|never asked/i)).toBeInTheDocument();
    expect(screen.queryByText(/^off$/i)).toBeNull();
  });

  it("hides itself entirely on a radio that has earned neither capability", () => {
    const { container } = renderPanel(makeClient(), null, () => false);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the way out while greying the way in, on a radio that can only clear", () => {
    // The reachable one-and-not-the-other: an image with no BK1080 answers ERR_NO_HAL, which earns
    // `clear_broadcast_fm` and must not earn `set_broadcast_fm`. Hiding the remedy while showing the
    // symptom is the silent-failure shape this project keeps closing (TuneControls.jsx:174-177).
    renderPanel(makeClient(), { on: true, hz: 104300000 }, (c) => c === "clear_broadcast_fm");
    expect(screen.getByRole("button", { name: /turn (it )?off/i })).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: /turn on broadcast fm/i })).toBeNull();
  });

  it("surfaces the server's own sentence when it refuses", async () => {
    // 422 (off the raster, out of band) and 409 (mid-over) both carry a real `detail`, and the
    // radio's own verdict is more use than anything this component could compose.
    const client = makeClient({
      broadcastFm: vi.fn().mockRejectedValue(new Error("104350000 Hz is off the 100000 Hz raster")),
    });
    renderPanel(client);
    fireEvent.click(arm());
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() =>
      expect(screen.getByText(/off the 100000 Hz raster/)).toBeInTheDocument(),
    );
  });
});
