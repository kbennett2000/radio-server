// SecretsPanel (ADR 0027) — the fixed-login-code control (ADR 0083). Drives the panel with a fake
// `client` and the `secrets` presence prop, no network. Proofs: presence renders from the prop; a
// valid 6-digit code POSTs write-only via setFixedCode; a non-6-digit code keeps Set disabled and
// shows the hint; and the security warning is always present.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SecretsPanel from "./SecretsPanel.jsx";

function makeClient(overrides = {}) {
  return {
    rotateApiToken: vi.fn(),
    enrollTotp: vi.fn(),
    setFixedCode: vi.fn().mockResolvedValue({ set: true, restart_required: true }),
    ...overrides,
  };
}

const SECRETS = {
  api_token: { set: true },
  totp_secret: { set: true },
  fixed_code: { set: false },
};

describe("SecretsPanel fixed login code", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the fixed-code row with presence and the security warning", () => {
    render(<SecretsPanel client={makeClient()} secrets={SECRETS} />);
    expect(screen.getByText("Fixed login code")).toBeTruthy();
    // The row's presence badge reads "not set" (fixed_code.set === false).
    expect(screen.getAllByText("not set").length).toBeGreaterThan(0);
    // The security warning is always shown.
    expect(screen.getByText(/Less secure/i)).toBeTruthy();
    expect(screen.getByText(/reuse it/i)).toBeTruthy();
  });

  it("POSTs a valid 6-digit code write-only", async () => {
    const client = makeClient();
    render(<SecretsPanel client={client} secrets={SECRETS} />);
    const input = screen.getByPlaceholderText(/6 digits, write-only/i);
    await userEvent.type(input, "135790");
    await userEvent.click(screen.getByRole("button", { name: "Set" }));
    expect(client.setFixedCode).toHaveBeenCalledWith("135790");
  });

  it("keeps Set disabled and warns until exactly 6 digits are entered", async () => {
    const client = makeClient();
    render(<SecretsPanel client={client} secrets={SECRETS} />);
    const input = screen.getByPlaceholderText(/6 digits, write-only/i);
    const setBtn = screen.getByRole("button", { name: "Set" });
    await userEvent.type(input, "123"); // too short
    expect(setBtn).toBeDisabled();
    expect(screen.getByText(/exactly 6 digits/i)).toBeTruthy();
    await userEvent.type(input, "456"); // now 6
    expect(setBtn).not.toBeDisabled();
    expect(client.setFixedCode).not.toHaveBeenCalled(); // not until Set is clicked
  });

  it("strips non-digits from the input so only digits are ever sent", async () => {
    const client = makeClient();
    render(<SecretsPanel client={client} secrets={SECRETS} />);
    const input = screen.getByPlaceholderText(/6 digits, write-only/i);
    await userEvent.type(input, "12ab34cd56"); // letters filtered out -> "123456"
    await userEvent.click(screen.getByRole("button", { name: "Set" }));
    expect(client.setFixedCode).toHaveBeenCalledWith("123456");
  });
});
