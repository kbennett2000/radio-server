// The masthead theme toggle (ADR 0044, extended ADR 0048). The label names the theme it switches
// TO, so the button reads as an action ("Night" while in Day mode), cycling Day → Night → Red.
// Presentation-only beyond theme.js.

import { useState } from "react";
import { getTheme, setTheme, nextTheme } from "../theme.js";

const LABELS = { day: "Day", night: "Night", red: "Red" };

export default function ThemeToggle() {
  const [theme, setCurrent] = useState(getTheme);
  const next = nextTheme(theme);

  const flip = () => {
    setTheme(next);
    setCurrent(next);
  };

  return (
    <button type="button" className="theme-toggle" onClick={flip} title="Switch the panel theme">
      <span className="theme-lamp" aria-hidden="true" />
      {LABELS[next] ?? "Day"}
    </button>
  );
}
