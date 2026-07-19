// DvapPanel (ADR 0096) — the DVAP control card: one row per gateway module, CONFIRMED link state,
// per-module Connect/Disconnect. Proves it hides when unconfigured, renders module rows with their
// frequency + confirmed pill, and wires Connect/Disconnect to the module-scoped client calls.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import DvapPanel from "./DvapPanel.jsx";

const BLOCK = {
  configured: true,
  remote: { host: "127.0.0.1", port: 10022 },
  modules: [
    { module: "B", label: "DVAP 70cm #1", frequency_hz: 441_600_000, reachable: true, linked: true, reflector: "REF001 C" },
    { module: "C", label: "DVAP 70cm #2", frequency_hz: 441_000_000, reachable: true, linked: false, reflector: "" },
  ],
};

function makeClient(dvap = BLOCK) {
  return {
    dvapStatus: vi.fn().mockResolvedValue({ dvap }),
    dvapLink: vi.fn().mockResolvedValue({ dvap }),
    dvapUnlink: vi.fn().mockResolvedValue({ dvap }),
  };
}

describe("DvapPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("hides when no DVAP module is configured", async () => {
    const client = makeClient(null);
    const { container } = render(<DvapPanel client={client} dvap={null} onAuthError={() => {}} />);
    await act(async () => {});
    expect(container.querySelector(".card")).toBeNull();
  });

  it("renders a row per module with frequency and confirmed state", async () => {
    const client = makeClient();
    render(<DvapPanel client={client} dvap={null} onAuthError={() => {}} />);
    await act(async () => {});

    expect(await screen.findByText("DVAP 70cm #1")).toBeTruthy();
    expect(screen.getByText(/441\.6 MHz/)).toBeTruthy();
    expect(screen.getByText(/441 MHz/)).toBeTruthy();
    expect(screen.getByText("Linked · REF001 C")).toBeTruthy();
    expect(screen.getByText("Not linked")).toBeTruthy();
  });

  it("marks an unreachable module rather than a link", async () => {
    const client = makeClient({
      ...BLOCK,
      modules: [{ ...BLOCK.modules[0], reachable: false, linked: false, reflector: "" }],
    });
    render(<DvapPanel client={client} dvap={null} onAuthError={() => {}} />);
    expect(await screen.findByText("Unreachable")).toBeTruthy();
  });

  it("Connect links the module by letter with the typed reflector", async () => {
    const client = makeClient();
    render(<DvapPanel client={client} dvap={null} onAuthError={() => {}} />);
    await act(async () => {});

    const input = screen.getByLabelText("Reflector for module C (name and module letter)");
    fireEvent.change(input, { target: { value: "XLX999 A" } });
    fireEvent.click(input.closest("form").querySelector("button[type=submit]"));

    await waitFor(() => expect(client.dvapLink).toHaveBeenCalledWith("C", "XLX999 A"));
  });

  it("Disconnect unlinks a linked module", async () => {
    const client = makeClient();
    render(<DvapPanel client={client} dvap={null} onAuthError={() => {}} />);
    await act(async () => {});

    // Module B is linked → its Disconnect is enabled.
    const bInput = screen.getByLabelText("Reflector for module B (name and module letter)");
    const disconnect = Array.from(bInput.closest("form").querySelectorAll("button")).find(
      (b) => b.textContent === "Disconnect",
    );
    fireEvent.click(disconnect);
    await waitFor(() => expect(client.dvapUnlink).toHaveBeenCalledWith("B"));
  });
});
