// TransportBanner — the dead-reader banner (ADR 0166).
//
// Two rules with reasons, and both are about what the banner must NOT do:
//
//  1. `null` is not a fault. A backend with no serial link (the mock, an audio-only radio) has
//     nothing to report, and decorating it with either a warning or a reassurance would turn "not
//     applicable" into a status. This is the same tri-state discipline `broadcast_fm` holds to.
//  2. It says the readings are stale, not just that something is wrong. The whole defect is that
//     the rest of the page keeps looking authoritative — a banner that only said "error" would
//     leave the operator trusting the frequency displayed under it.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TransportBanner from "./TransportBanner.jsx";

const DEAD = { alive: false, error: "device reports readiness to read but returned no data", port: "/dev/ttyACM0" };

function renderBanner(transport, client = { reconnectTransport: vi.fn().mockResolvedValue({ outcome: "reopened" }) }) {
  return render(
    <TransportBanner
      client={client}
      transport={transport}
      onAuthError={() => {}}
      onUnsupported={() => {}}
    />,
  );
}

describe("TransportBanner", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says nothing on a healthy link", () => {
    const { container } = renderBanner({ alive: true, error: null, port: "/dev/ttyACM0" });
    expect(container).toBeEmptyDOMElement();
  });

  it("says nothing on a backend with no serial link, rather than reassuring about one", () => {
    // `null` is "not applicable", not "fine". A mock station has no reader to lose.
    const { container } = renderBanner(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("survives a server too old to send the field at all", () => {
    const { container } = renderBanner(undefined);
    expect(container).toBeEmptyDOMElement();
  });

  it("tells the operator the rest of the page is stale, not merely that something failed", () => {
    // The defect is that everything else keeps looking authoritative. "Error" alone would leave
    // the frequency shown below it being trusted.
    renderBanner(DEAD);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/stopped answering/i);
    expect(alert.textContent).toMatch(/last thing this server knew/i);
  });

  it("shows the radio's own error text and the port to go and look at", () => {
    renderBanner(DEAD);
    expect(screen.getByText(/returned no data/)).toBeInTheDocument();
    expect(screen.getByText("/dev/ttyACM0")).toBeInTheDocument();
  });

  it("offers one reopen, and says it is one attempt rather than a retry loop", () => {
    const client = { reconnectTransport: vi.fn().mockResolvedValue({ outcome: "reopened" }) };
    renderBanner(DEAD, client);
    fireEvent.click(screen.getByRole("button", { name: /reopen/i }));
    expect(client.reconnectTransport).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert").textContent).toMatch(/not a retry loop/i);
  });

  it("surfaces the server's sentence when the reopen is refused", async () => {
    // A 503 here means another process holds the port. That is the operator's problem to solve and
    // the server's message names it — this component must not compose a friendlier lie over it.
    const client = {
      reconnectTransport: vi.fn().mockRejectedValue(new Error("/dev/ttyACM0 is held by another process")),
    };
    renderBanner(DEAD, client);
    fireEvent.click(screen.getByRole("button", { name: /reopen/i }));
    await waitFor(() =>
      expect(screen.getByText(/held by another process/)).toBeInTheDocument(),
    );
  });
});
