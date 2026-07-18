// BackendPanel (ADR 0077): the runtime radio selector. Drives the component with a fake `client`
// (the api.js seam) so no network is touched. Proofs: it renders the configured list with the active
// radio marked; picking one POSTs that backend; an in-flight switch shows "Switching…" and disables
// the control; a failed switch (503 — server already rolled back) surfaces the error and snaps the
// dropdown back to the radio you're still on; and it warns while transmitting that a switch drops PTT.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BackendPanel from "./BackendPanel.jsx";

const BACKENDS = {
  active: "baofeng",
  active_capabilities: ["ptt", "receive", "status", "transmit"],
  backends: [
    { name: "baofeng", active: true, settings: {} },
    { name: "kv4p", active: false, settings: {} },
  ],
};

function makeClient(overrides = {}) {
  return {
    backends: vi.fn().mockResolvedValue(BACKENDS),
    selectBackend: vi.fn().mockResolvedValue({ backend: "kv4p", ...BACKENDS }),
    ...overrides,
  };
}

describe("BackendPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("self-hides when fewer than two backends are configured", async () => {
    const client = makeClient({
      backends: vi.fn().mockResolvedValue({ active: "baofeng", backends: [{ name: "baofeng", active: true }] }),
    });
    const { container } = render(<BackendPanel client={client} active="baofeng" />);
    await act(async () => {}); // flush the mount fetch; the card must stay hidden
    expect(client.backends).toHaveBeenCalled();
    expect(container.querySelector(".card")).toBeNull();
  });

  it("renders the configured list with the active radio marked", async () => {
    const client = makeClient();
    render(<BackendPanel client={client} active="baofeng" />);

    const select = await screen.findByRole("combobox");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toEqual(["baofeng (active)", "kv4p"]);
    expect(select).toHaveValue("baofeng");
  });

  it("selecting a backend POSTs that backend", async () => {
    const client = makeClient();
    render(<BackendPanel client={client} active="baofeng" />);

    const select = await screen.findByRole("combobox");
    await userEvent.selectOptions(select, "kv4p");
    expect(client.selectBackend).toHaveBeenCalledWith("kv4p");
  });

  it("shows an in-progress state and disables the control while switching", async () => {
    let resolveSelect;
    const client = makeClient({
      selectBackend: vi.fn().mockReturnValue(new Promise((res) => (resolveSelect = res))),
    });
    render(<BackendPanel client={client} active="baofeng" />);

    const select = await screen.findByRole("combobox");
    await userEvent.selectOptions(select, "kv4p");

    expect(await screen.findByText("Switching…")).toBeInTheDocument();
    expect(select).toBeDisabled();

    // Let the switch settle so its state update doesn't leak past the test.
    await act(async () => {
      resolveSelect({ backend: "kv4p" });
    });
  });

  it("on a failed switch surfaces the error and snaps back to the previous radio", async () => {
    const client = makeClient({
      selectBackend: vi.fn().mockRejectedValue(new Error("kv4p failed to boot; still on baofeng")),
    });
    // `active` stays "baofeng" the whole test — a 503 rolled the server back, so state.backend never
    // changed. The dropdown must reflect that, not the pick that failed.
    render(<BackendPanel client={client} active="baofeng" />);

    const select = await screen.findByRole("combobox");
    await userEvent.selectOptions(select, "kv4p");

    expect(await screen.findByRole("alert")).toHaveTextContent("kv4p failed to boot; still on baofeng");
    await waitFor(() => expect(select).toHaveValue("baofeng"));
    expect(select).not.toBeDisabled();
  });

  it("warns that switching drops the transmission while on air", async () => {
    const client = makeClient();
    render(<BackendPanel client={client} active="baofeng" transmitting />);
    await screen.findByRole("combobox");
    expect(screen.getByText(/switching radios drops the current transmission/i)).toBeInTheDocument();
  });
});
