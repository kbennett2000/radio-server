// Vitest global setup (ADR 0077): register the @testing-library/jest-dom matchers (toBeDisabled,
// toBeInTheDocument, ...) and auto-clean the DOM between tests.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
