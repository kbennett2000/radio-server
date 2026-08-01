// BroadcastFmPanel — the SECOND RECEIVER card (ADR 0164, reshaped by ADR 0168).
//
// ADR 0164's tests were about what the operator is TOLD before they commit. ADR 0168 removed that
// furniture at the operator's instruction (#225), so these are about what the operator can DO —
// and about the defect the old shape hid.
//
// Four rules, each with a reason that is not "it looks nicer":
//
//  1. One click. The arm/confirm is gone, and the absence is pinned rather than left to be noticed
//     when somebody re-adds it "for safety". The Part 97 control is the relay mute (ADR 0162) and
//     the pre-key-up clear (ADR 0161); neither is in this file and neither moved.
//  2. The frequency and band controls are on screen WHILE THE RECEIVER RUNS. ADR 0164 gated them on
//     `!on` and gated Retune on `on`, so Retune could only ever re-send the frequency the radio was
//     already on (#226) and moving the receiver meant stopping it (#227). That shipped green
//     because `"tune"` appeared nowhere in this file — so it appears here now, repeatedly.
//  3. The box follows the RADIO, not the last thing typed. It seeds from the read-back, so a reload
//     or a front-panel `F+0` cannot leave a stale 104.3 in a field whose button will send it — but
//     a status frame that does not move the receiver must not erase a half-typed frequency either.
//  4. Every action shows the radio's own answer. A retune to the frequency it is already on changes
//     nothing else on the card, and without an answer that is #226's symptom outliving its cause.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import BroadcastFmPanel from "./BroadcastFmPanel.jsx";

const allCaps = () => true;
const ON_AIR = { on: true, hz: 104300000, band: 0 };

function makeClient(overrides = {}) {
  return {
    broadcastFm: vi.fn().mockResolvedValue({ broadcast_fm: { on: true, hz: 104300000, band: 0 } }),
    ...overrides,
  };
}

function panel(client, broadcastFm, hasCap) {
  return (
    <BroadcastFmPanel
      client={client}
      broadcastFm={broadcastFm}
      hasCap={hasCap}
      onAuthError={() => {}}
      onUnsupported={() => {}}
    />
  );
}

function renderPanel(client = makeClient(), broadcastFm = { on: false, hz: null }, hasCap = allCaps) {
  return render(panel(client, broadcastFm, hasCap));
}

const freq = () => screen.getByLabelText(/frequency/i);
const bandSelect = () => screen.getByLabelText(/band/i);
const turnOn = () => screen.getByRole("button", { name: /turn on broadcast fm/i });
const retune = () => screen.getByRole("button", { name: /^retune$/i });

describe("BroadcastFmPanel — turning it on", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fires on the first click, with the frequency and band that were entered", async () => {
    const client = makeClient();
    renderPanel(client);
    fireEvent.change(freq(), { target: { value: "98.5" } });
    fireEvent.change(bandSelect(), { target: { value: "1" } });
    fireEvent.click(turnOn());
    await waitFor(() =>
      expect(client.broadcastFm).toHaveBeenCalledWith({ action: "on", hz: 98500000, band: 1 }),
    );
    expect(client.broadcastFm).toHaveBeenCalledTimes(1);
  });

  it("has no confirm step to click through", async () => {
    const client = makeClient();
    renderPanel(client);
    fireEvent.click(turnOn());
    expect(screen.queryByRole("button", { name: /confirm/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
    await waitFor(() => expect(client.broadcastFm).toHaveBeenCalledTimes(1));
  });

  it("does not put a consequence notice in front of the button (#225)", () => {
    // The three consequences are still true and still written down — `docs/troubleshooting.md` and
    // ADR 0164. They are not in the card, and this pins that rather than leaving it to be noticed
    // when somebody re-adds them.
    renderPanel();
    expect(screen.queryByRole("note")).toBeNull();
    expect(document.body.textContent).not.toMatch(/turning this on/i);
    expect(document.body.textContent).not.toMatch(/both links go silent/i);
  });

  it("does not warn about the keypad while broadcast FM is running (#225)", () => {
    renderPanel(makeClient(), ON_AIR);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.body.textContent).not.toMatch(/keypad/i);
  });
});

describe("BroadcastFmPanel — retuning without stopping it (#226, #227)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps the frequency and band controls on screen while the receiver is running", () => {
    // The defect, stated as a test: ADR 0164 unmounted these the moment `on` went true, which left
    // Retune with nothing it could possibly change.
    renderPanel(makeClient(), ON_AIR);
    expect(freq()).toBeInTheDocument();
    expect(bandSelect()).toBeInTheDocument();
  });

  it("sends a tune carrying the newly typed frequency", async () => {
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.change(freq(), { target: { value: "98.5" } });
    fireEvent.click(retune());
    await waitFor(() =>
      expect(client.broadcastFm).toHaveBeenCalledWith({ action: "tune", hz: 98500000, band: 0 }),
    );
  });

  it("tunes rather than re-starting: no off, no second on", async () => {
    // #227 in one assertion — the operator does not have to stop playback to move the receiver, and
    // `tune` is deliberately not a cheaper `on` (ADR 0156 D9), so the verb has to be right.
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.change(freq(), { target: { value: "88.7" } });
    fireEvent.click(retune());
    await waitFor(() => expect(client.broadcastFm).toHaveBeenCalledTimes(1));
    expect(client.broadcastFm.mock.calls[0][0].action).toBe("tune");
  });

  it("applies on Enter in the frequency field", async () => {
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.change(freq(), { target: { value: "90.1" } });
    fireEvent.keyDown(freq(), { key: "Enter" });
    await waitFor(() =>
      expect(client.broadcastFm).toHaveBeenCalledWith({ action: "tune", hz: 90100000, band: 0 }),
    );
  });

  it("seeds the box from the radio, so a retune cannot fling it to a stale default", () => {
    // Mounting while broadcast FM is already running — a reload, or FM switched on at the radio's
    // front panel. The old card would have sent its hardcoded 104.3 to a receiver sitting on 98.5.
    renderPanel(makeClient(), { on: true, hz: 98500000, band: 1 });
    expect(freq()).toHaveValue(98.5);
    expect(bandSelect()).toHaveValue("1");
  });

  it("does not clobber a half-typed frequency with a status frame that moved nothing", () => {
    // `status` frames arrive continuously and most carry the same reading (a `rescues` tick, an
    // unrelated field). Syncing on every one of them would erase what the operator is typing.
    const client = makeClient();
    const { rerender } = render(panel(client, { ...ON_AIR, rescues: 0 }, allCaps));
    fireEvent.change(freq(), { target: { value: "98.5" } });
    rerender(panel(client, { ...ON_AIR, rescues: 1 }, allCaps));
    expect(freq()).toHaveValue(98.5);
  });

  it("follows the radio when the reading really does move", () => {
    const client = makeClient();
    const { rerender } = render(panel(client, ON_AIR, allCaps));
    expect(freq()).toHaveValue(104.3);
    rerender(panel(client, { on: true, hz: 88700000, band: 0 }, allCaps));
    expect(freq()).toHaveValue(88.7);
  });

  it("reports the radio's own answer, even when the retune moved nothing", async () => {
    // The remaining half of #226: retuning to the frequency it is already on leaves every other
    // line on the card identical, so without the read-back the operator sees a disabled flicker and
    // concludes the button is dead.
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.click(retune());
    const answer = await screen.findByRole("status");
    expect(answer.textContent).toMatch(/104\.3 MHz/);
  });

  it("names the frequency the RADIO reports, not the one that was typed", async () => {
    // ADR 0156's read-back doctrine. An echo of the request would be the one thing that cannot
    // detect a radio that took a different frequency from the one it was sent.
    const client = makeClient({
      broadcastFm: vi
        .fn()
        .mockResolvedValue({ broadcast_fm: { on: true, hz: 98500000, band: 0 } }),
    });
    renderPanel(client, ON_AIR);
    fireEvent.change(freq(), { target: { value: "98.4" } });
    fireEvent.click(retune());
    const answer = await screen.findByRole("status");
    expect(answer.textContent).toMatch(/98\.5 MHz/);
    expect(answer.textContent).not.toMatch(/98\.4/);
  });

  it("clears the radio's answer rather than letting it go stale", async () => {
    vi.useFakeTimers();
    try {
      const client = makeClient();
      render(panel(client, ON_AIR, allCaps));
      fireEvent.click(retune());
      await act(async () => {});
      expect(screen.getByRole("status")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(6000));
      expect(screen.queryByRole("status")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("refuses to send an empty frequency box", () => {
    // An empty box serialises `hz: null` and earns a 422 about the wrong thing. The raster and the
    // band's own limits stay the radio's verdict — this catches only "no number was entered".
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.change(freq(), { target: { value: "" } });
    expect(retune()).toBeDisabled();
    fireEvent.click(retune());
    expect(client.broadcastFm).not.toHaveBeenCalled();
  });
});

describe("BroadcastFmPanel — while it is running", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers a way out with no confirm step", () => {
    // Leaving broadcast FM is always safe: it gives the station its ears back and un-mutes both
    // links. Arming a button in front of the remedy is how ADR 0161 finding 2 stayed open.
    const client = makeClient();
    renderPanel(client, ON_AIR);
    fireEvent.click(screen.getByRole("button", { name: /turn (it )?off/i }));
    return waitFor(() => expect(client.broadcastFm).toHaveBeenCalledWith({ action: "off" }));
  });

  it("shows where the receiver is, so the operator can see which station it took", () => {
    renderPanel(makeClient(), ON_AIR);
    expect(screen.getByText(/104\.3/)).toBeInTheDocument();
  });

  it("labels the way in and the way on differently", () => {
    const { rerender } = render(panel(makeClient(), { on: false, hz: null }, allCaps));
    expect(turnOn()).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^retune$/i })).toBeNull();
    rerender(panel(makeClient(), ON_AIR, allCaps));
    expect(retune()).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /turn on broadcast fm/i })).toBeNull();
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
    renderPanel(makeClient(), ON_AIR, (c) => c === "clear_broadcast_fm");
    expect(screen.getByRole("button", { name: /turn (it )?off/i })).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: /turn on broadcast fm/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^retune$/i })).toBeNull();
    expect(screen.queryByLabelText(/frequency/i)).toBeNull();
  });

  it("surfaces the server's own sentence when it refuses", async () => {
    // 422 (off the raster, out of band) and 409 (mid-over, or a tune on a receiver that is off) all
    // carry a real `detail`, and the radio's own verdict is more use than anything this component
    // could compose.
    const client = makeClient({
      broadcastFm: vi.fn().mockRejectedValue(new Error("104350000 Hz is off the 100000 Hz raster")),
    });
    renderPanel(client);
    fireEvent.click(turnOn());
    await waitFor(() =>
      expect(screen.getByText(/off the 100000 Hz raster/)).toBeInTheDocument(),
    );
  });
});
