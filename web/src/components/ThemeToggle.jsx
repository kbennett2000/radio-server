// The masthead Day/Night toggle (ADR 0044). The label names the theme it switches TO, so the
// button reads as an action ("Night" while in Day mode). Presentation-only beyond theme.js.

import { useState } from "react";
import { getTheme, setTheme } from "../theme.js";

export default function ThemeToggle() {
  const [theme, setCurrent] = useState(getTheme);
  const next = theme === "night" ? "day" : "night";

  const flip = () => {
    setTheme(next);
    setCurrent(next);
  };

  return (
    <button type="button" className="theme-toggle" onClick={flip} title="Switch the panel theme">
      <span className="theme-lamp" aria-hidden="true" />
      {next === "night" ? "Night" : "Day"}
    </button>
  );
}
