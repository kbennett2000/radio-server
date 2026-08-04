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

// --- Talk-slot occupancy and RX demand (ADR 0170) ---------------------------------------------
//
// Before these rows a stranded slot was invisible from the dashboard: `Transmitting` is PTT state
// and `Receiving` is squelch state, and BOTH read false while a slot is stuck — so the card
// positively suggested an idle station while every Talk press was refused.

const FREE = { held: false, holder: null, since: null, held_s: null, stale_after_s: 180, refused: {} };

describe("the talk-slot row", () => {
  it("names who holds the transmitter and for how long", () => {
    render(
      <StatusPanel
        state={{ backend: "uvk5" }}
        hasCap={allCaps}
        slots={{
          tx: { ...FREE, held: true, holder: "browser", held_s: 12.4 },
          mumble: FREE,
          dstar: null,
        }}
      />,
    );
    // The age is the load-bearing half: "held" is ambiguous between an operator mid-over and a
    // socket that died in March.
    expect(screen.getByText(/RF: browser, 12s/)).toBeTruthy();
  });

  it("says so plainly when nothing is held", () => {
    render(<StatusPanel state={{}} hasCap={allCaps} slots={{ tx: FREE, mumble: null, dstar: null }} />);
    expect(screen.getByText("all free")).toBeTruthy();
  });

  it("calls out a hold longer than any legal over", () => {
    render(
      <StatusPanel
        state={{}}
        hasCap={allCaps}
        slots={{ tx: { ...FREE, held: true, holder: "browser", held_s: 15600 }, mumble: null, dstar: null }}
      />,
    );
    // Past the transmitter time-out no legitimate holder exists. This must not render like the
    // ordinary busy case beside it — that equivalence is the whole defect.
    expect(screen.getByText(/4h 20m — STUCK\?/)).toBeTruthy();
  });

  it("renders no row at all against a server that does not report slots", () => {
    // An unanswered question must not look like a clear answer: absent is not "all free".
    render(<StatusPanel state={{}} hasCap={allCaps} />);
    expect(screen.queryByText(/Talk slots/)).toBeNull();
  });
});

describe("the RX demand row", () => {
  it("is worded as demand, never as live listeners", () => {
    // MEASURED (ADR 0170): a dropped `/audio/rx` listener stays counted until the next frame is
    // published, and on a squelched quiet channel that may be never. So this counts REQUESTS, and
    // the label is the only thing that travels with the value to the operator reading it — a note in
    // api.md does not reach this card.
    render(<StatusPanel state={{}} hasCap={allCaps} rxDemand={{ requested: 2, reader_running: true }} />);
    expect(screen.getByText("RX demand")).toBeTruthy();
    expect(screen.getByText(/2 requested · reader running/)).toBeTruthy();
    expect(screen.queryByText(/listener/i)).toBeNull();
    expect(screen.queryByText(/active/i)).toBeNull();
  });

  it("is a separate row from the talk slots, not folded in with them", () => {
    // Talk slots are self-reaping and this number is not. One row holding both would lend this one
    // their trustworthiness, which is exactly the confusion the naming is there to prevent.
    render(
      <StatusPanel
        state={{}}
        hasCap={allCaps}
        slots={{ tx: FREE, mumble: null, dstar: null }}
        rxDemand={{ requested: 1, reader_running: true }}
      />,
    );
    expect(screen.getByText("all free")).toBeTruthy();
    expect(screen.getByText(/1 requested/)).toBeTruthy();
  });
});

// The instrument rows (ADR 0179). Two counters this server has been keeping for cycles reach the
// browser for the first time here — and only the two whose nonzero value means something is wrong
// right now. The rest of both blocks stays in `/status`, where an operator diagnosing a station
// reads it, because a card that shows eight healthy diagnostics has buried the four operational
// facts it exists for.
describe("the instrument rows", () => {
  it("renders nothing at all on a station whose instruments are healthy", () => {
    // TransportBanner's rule, and the one that keeps this card usable: "absence of a fault is not a
    // status worth a banner". A zero here is the normal reading, not news.
    render(
      <StatusPanel
        state={{
          backend: "baofeng",
          wire: { key_ups: 412, wire_busy_at_key_up: 0, key_ups_with_wire_traffic: 0,
                  keyed_with_wire_busy: 0, key_ups_that_waited_for_the_wire: 68,
                  longest_wire_wait_ms: 97.4 },
          rssi_cadence: { polls: 900, unknown: 4, skipped: 11, pause_errors: 0,
                          age_s: 0.25, stale_after_s: 1.5 },
        }}
        hasCap={allCaps}
      />,
    );
    expect(screen.queryByText(/control wire/i)).toBeNull();
    expect(screen.queryByText(/signal meter/i)).toBeNull();
  });

  it("calls out key-ups that went out with the control wire still busy", () => {
    // ADR 0177 measured what this costs when it goes wrong: one register exchange in flight across
    // the PTT assert and the station did not radiate at all — 0 of 81 carrier polls at the witness.
    // Every other counter in the block says the drain HANDLED the race; this one says it did not.
    render(
      <StatusPanel
        state={{
          backend: "baofeng",
          wire: { key_ups: 412, wire_busy_at_key_up: 3, key_ups_with_wire_traffic: 3,
                  keyed_with_wire_busy: 2, key_ups_that_waited_for_the_wire: 68,
                  longest_wire_wait_ms: 97.4 },
        }}
        hasCap={allCaps}
      />,
    );
    expect(screen.getByText(/2 of 412/)).toBeTruthy();
  });

  it("does not raise the alarm for the races the drain handled", () => {
    // The distinction the whole cycle turns on: a poll in flight at key-up is the race firing and
    // being handled, which is common and costs a bounded wait. Rendering that as a fault would
    // train an operator to ignore the row that matters.
    render(
      <StatusPanel
        state={{
          backend: "baofeng",
          wire: { key_ups: 412, wire_busy_at_key_up: 9, key_ups_with_wire_traffic: 9,
                  keyed_with_wire_busy: 0, key_ups_that_waited_for_the_wire: 68,
                  longest_wire_wait_ms: 97.4 },
        }}
        hasCap={allCaps}
      />,
    );
    expect(screen.queryByText(/control wire/i)).toBeNull();
  });

  it("says the meter's transmit guard is broken, not that a transmission was damaged", () => {
    // The naming rule, carried to the card: `pause_errors` counts ticks whose pause hook RAISED.
    // It does not say any over was clipped — only RF at a witness says that — and a row that
    // implied otherwise would send an operator to the wrong instrument.
    render(
      <StatusPanel
        state={{
          backend: "baofeng",
          rssi_cadence: { polls: 900, unknown: 4, skipped: 0, pause_errors: 7,
                          age_s: 0.25, stale_after_s: 1.5 },
        }}
        hasCap={allCaps}
      />,
    );
    expect(screen.getByText(/guard is broken/i)).toBeTruthy();
    expect(screen.queryByText(/damaged/i)).toBeNull();
  });

  it("treats an absent block and a null one alike, and neither as a fault", () => {
    // `undefined` is a server too old to send the field; `null` is a backend with no shared wire
    // and nothing polling it. Both are "nothing to say" — the tri-state rule this project keeps —
    // and a station that cannot have this fault must not be decorated with a reassurance either.
    render(<StatusPanel state={{ backend: "mock", wire: null, rssi_cadence: null }} hasCap={allCaps} />);
    expect(screen.queryByText(/control wire/i)).toBeNull();
    expect(screen.queryByText(/signal meter/i)).toBeNull();

    render(<StatusPanel state={{ backend: "mock" }} hasCap={allCaps} />);
    expect(screen.queryByText(/control wire/i)).toBeNull();
    expect(screen.queryByText(/signal meter/i)).toBeNull();
  });
});
