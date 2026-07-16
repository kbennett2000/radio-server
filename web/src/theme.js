// Day/Night theme (ADR 0044): the ONE piece of client behavior added by the visual refresh.
// Night mode is `data-theme="night"` on <body> (the CSS token overrides key off it); Day is the
// attribute's absence. The choice persists in localStorage and is restored before first render,
// with every storage touch guarded so a disabled/private-mode localStorage never crashes (the
// App.jsx remembered-token pattern).

const THEME_KEY = "radio.theme";

export function getTheme() {
  try {
    return window.localStorage.getItem(THEME_KEY) === "night" ? "night" : "day";
  } catch {
    return "day";
  }
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
  if (theme === "night") document.body.dataset.theme = "night";
  else delete document.body.dataset.theme;
}

// Called once from main.jsx before the first render so the page never flashes the wrong theme.
export function initTheme() {
  applyTheme(getTheme());
}
