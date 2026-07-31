// StatusPanel's PA row (ADR 0134). The station could not previously answer "what did that over
// actually radiate?" — a transmission that reached nothing looked identical to one that worked,
// because every API call returns 200 either way. The wrong-band case is the one worth reading: the
// UV-K5's dock firmware sets the PA up from the radio's own VFO, and the bias byte it computes is
// unreadable calibration from the *other* band whenever the host transmits outside it.

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusPanel from "./StatusPanel.jsx";

const allCaps = () => true;

describe("the PA row", () => {
  it("calls out a wrong-band PA rather than showing an uninterpretable number", () => {
    render(
      <StatusPanel
        state={{ backend: "uvk5", pa: { bias: 12, gain: 0x88, band_matched: false, tx_frequency: 147_555_000 } }}
        hasCap={allCaps}
      />,
    );
    expect(screen.getByText(/WRONG BAND, power uncharacterised/)).toBeTruthy();
  });

  it("reports the band it was set up for when the firmware got it right", () => {
    render(
      <StatusPanel
        state={{ backend: "uvk5", pa: { bias: 12, gain: 0xa2, band_matched: true, tx_frequency: 443_525_000 } }}
        hasCap={allCaps}
      />,
    );
    expect(screen.getByText(/bias 12 · 443\.5250 MHz/)).toBeTruthy();
    expect(screen.queryByText(/WRONG BAND/)).toBeNull();
  });

  it("shows no PA row at all before the first over, rather than a zero", () => {
    render(<StatusPanel state={{ backend: "uvk5", pa: null }} hasCap={allCaps} />);
    expect(screen.queryByText(/PA \(last over\)/)).toBeNull();
  });
});

// ADR 0139. In Baofeng mode the operator's front panel holds the channel and nothing in the repo
// can read it back — and without `set_frequency` the radio face's LCD is hidden too. So a screen
// that merely omitted the frequency would show an operator no reason to think one was involved,
// immediately before a button that transmits on it.
describe("where it transmits, on a radio the server cannot tune", () => {
  const noCat = (name) => name !== "set_frequency" && name !== "set_split";

  it("says the frequency lives on the radio instead of hiding it", () => {
    render(<StatusPanel state={{ backend: "baofeng" }} hasCap={noCat} />);
    expect(screen.getByText(/set on the radio — not readable from here/)).toBeTruthy();
  });

  it("marks it as a warning, not as a settled good state", () => {
    render(<StatusPanel state={{ backend: "baofeng" }} hasCap={noCat} />);
    const value = screen.getByText(/set on the radio — not readable from here/);
    expect(value.className).toContain("warn");
    expect(value.className).not.toContain(" on");
  });

  it("stays out of the way on a radio that can actually be tuned", () => {
    render(<StatusPanel state={{ backend: "uvk5", tx_frequency: 443_525_000 }} hasCap={allCaps} />);
    expect(screen.queryByText(/not readable from here/)).toBeNull();
    expect(screen.getByText(/443\.5250 MHz/)).toBeTruthy();
  });
});

// The demodulator (ADR 0150/0154). Three answers, not two: FM, AM, and not-known — the server does
// not default it to FM even though the firmware seeds FM, because that state belongs to the radio.
describe("the demodulation row", () => {
  it("reports what the radio confirmed", () => {
    render(<StatusPanel state={{ backend: "baofeng", modulation: "FM", tx_ok: true }} hasCap={allCaps} />);
    expect(screen.getByText("FM")).toBeTruthy();
  });

  it("says AM costs the transmitter, and marks it as an alarm", () => {
    render(<StatusPanel state={{ backend: "baofeng", modulation: "AM", tx_ok: false }} hasCap={allCaps} />);
    const value = screen.getByText(/AM — transmit disabled/);
    expect(value.className).toContain("warn");
    // Never the good/active accent: an alarm painted the "session open" colour reads as reassurance.
    expect(value.className).not.toContain(" on");
  });

  it("does not claim AM when the radio only said it will not key", () => {
    // `tx_ok: false` means non-FM. USB is reserved on this wire, so the row must not name a
    // demodulator the radio never reported.
    render(<StatusPanel state={{ backend: "baofeng", modulation: null, tx_ok: false }} hasCap={allCaps} />);
    expect(screen.getByText(/not FM — transmit disabled/)).toBeTruthy();
  });

  it("shows a dash, not a guess, before the server has asserted one", () => {
    render(<StatusPanel state={{ backend: "mock", modulation: null, tx_ok: null }} hasCap={allCaps} />);
    // Scoped to this row — several rows render "—" and a bare text query would match any of them.
    const row = screen.getByText("Demodulation").closest(".status-row");
    expect(row.querySelector(".status-value").textContent).toBe("—");
    expect(row.querySelector(".status-value").className).not.toContain("warn");
  });

  it("is absent on a backend that has no demodulator to report", () => {
    // The panel's rule: CAT rows appear only where the capability was advertised, so an audio-only
    // radio shows Backend rather than a column of "—".
    render(
      <StatusPanel
        state={{ backend: "kv4p", modulation: null, tx_ok: null }}
        hasCap={(c) => c !== "set_modulation"}
      />,
    );
    expect(screen.queryByText("Demodulation")).toBeNull();
  });

  it("still warns if a runtime 501 withdrew the capability while the refusal stands", () => {
    render(
      <StatusPanel
        state={{ backend: "baofeng", modulation: "AM", tx_ok: false }}
        hasCap={(c) => c !== "set_modulation"}
      />,
    );
    expect(screen.getByText(/AM — transmit disabled/)).toBeTruthy();
  });
});
