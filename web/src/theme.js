// Theme selection (ADR 0044, extended ADR 0048): the ONE piece of client behavior added by the
// visual refresh. Day is the default (no `data-theme`); every other theme is `data-theme="<name>"`
// on <body>, which the CSS token overrides key off. The masthead toggle cycles through THEMES in
// order. The choice persists in localStorage and is restored before first render, with every
// storage touch guarded so a disabled/private-mode localStorage never crashes (the App.jsx
// remembered-token pattern).

const THEME_KEY = "radio.theme";

//: The theme cycle order (day → night → red → day). Day is the default/absent-attribute theme.
export const THEMES = ["day", "night", "red"];

export function getTheme() {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return THEMES.includes(stored) ? stored : "day";
  } catch {
    return "day";
  }
}

//: The next theme in the cycle, wrapping back to the first. An unknown current theme starts at day.
export function nextTheme(theme) {
  const i = THEMES.indexOf(theme);
  return THEMES[(i + 1) % THEMES.length];
}

export function setTheme(theme) {
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage unavailable — the theme still applies for this tab */
  }
  applyTheme(theme);
}

export function applyTheme(theme) {
  if (theme !== "day" && THEMES.includes(theme)) document.body.dataset.theme = theme;
  else delete document.body.dataset.theme;
}

// Called once from main.jsx before the first render so the page never flashes the wrong theme.
export function initTheme() {
  applyTheme(getTheme());
}
