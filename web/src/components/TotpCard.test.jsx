// TotpCard — the masthead login-code chip. Covers the fixed-code state (ADR 0083): when /auth/totp
// reports {enforced:true, fixed:true} the card shows a locked "fixed code" chip and NEVER a rotating
// code, and clicking it still opens a session. Drives with a fake `client`, no network.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TotpCard from "./TotpCard.jsx";

function makeClient(totpBody) {
  return {
    totpCode: vi.fn().mockResolvedValue(totpBody),
    openSession: vi.fn().mockResolvedValue({ opened: true, session_open: true }),
  };
}

describe("TotpCard fixed-code state", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows a locked fixed-code chip and no rotating code", async () => {
    const client = makeClient({ enforced: true, fixed: true });
    render(<TotpCard client={client} />);
    expect(await screen.findByText("fixed code")).toBeTruthy();
    // No countdown fill and no numeric code are rendered for a fixed code.
    const chip = screen.getByRole("button", { name: /fixed over-the-air login code/i });
    expect(chip.querySelector(".totp-countdown")).toBeNull();
    expect(chip.querySelector(".totp-code")).toBeNull();
  });

  it("opens a session when the fixed chip is clicked", async () => {
    const client = makeClient({ enforced: true, fixed: true });
    render(<TotpCard client={client} />);
    const chip = await screen.findByRole("button", { name: /fixed over-the-air login code/i });
    await userEvent.click(chip);
    expect(client.openSession).toHaveBeenCalled();
  });

  it("still shows the rotating code when not in fixed mode", async () => {
    const client = makeClient({ enforced: true, code: "424242", seconds_remaining: 20, interval: 30 });
    render(<TotpCard client={client} />);
    expect(await screen.findByText("424242")).toBeTruthy();
  });
});
