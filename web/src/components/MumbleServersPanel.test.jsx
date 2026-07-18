// MumbleServersPanel — after ADR 0083's settings-screen tidy it folds like the schema-driven groups
// (a native <details className="settings-group">) instead of a flat non-collapsing card. Proof: the
// panel renders as that collapsible with the "Mumble servers" summary and a server count.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import MumbleServersPanel from "./MumbleServersPanel.jsx";

function makeClient(servers = []) {
  return {
    mumbleServers: vi.fn().mockResolvedValue({ servers }),
    saveMumbleServers: vi.fn(),
    setMumblePassword: vi.fn(),
  };
}

describe("MumbleServersPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders as a collapsible settings-group (matches the other settings sections)", async () => {
    const client = makeClient([{ name: "home", host: "h", port: 64738, dtmf: "13" }]);
    const { container } = render(<MumbleServersPanel client={client} />);
    await act(async () => {}); // flush the mount fetch

    const details = container.querySelector("details.settings-group");
    expect(details).not.toBeNull();
    const summary = details.querySelector("summary");
    expect(summary.textContent).toContain("Mumble servers");
    // The count chip reflects the loaded list, like GroupPanel's "N settings".
    expect(summary.textContent).toContain("1 server");
    // The bespoke editor body lives in the standard group body wrapper.
    expect(details.querySelector(".settings-group-body")).not.toBeNull();
  });

  it("pluralizes the server count", async () => {
    const client = makeClient([
      { name: "a", host: "h", port: 64738, dtmf: "13" },
      { name: "b", host: "h", port: 64738, dtmf: "14" },
    ]);
    const { container } = render(<MumbleServersPanel client={client} />);
    await act(async () => {});
    expect(container.querySelector("summary").textContent).toContain("2 servers");
  });
});
