import React from "react";
import { createRoot } from "react-dom/client";
// IBM Plex Mono is vendored (ADR 0044) — LAN deployments may be offline, so no font CDN.
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/600.css";
import "@fontsource/ibm-plex-mono/700.css";
import App from "./App.jsx";
import "./styles.css";
import { initTheme } from "./theme.js";

initTheme(); // restore Day/Night before first paint so the theme never flashes

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
