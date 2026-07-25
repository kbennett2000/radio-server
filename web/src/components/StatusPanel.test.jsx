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
