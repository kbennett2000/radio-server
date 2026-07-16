import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API has no path prefix — endpoints live at the root (/status, /capabilities, /ptt, ...).
// In `npm run dev` the SPA is served by Vite (:5173) but the API is the Python server (:8000);
// proxying these exact paths keeps everything same-origin from the browser's view, so no CORS is
// needed in dev either. In production the built bundle is served same-origin by FastAPI itself
// (ADR 0022), so the proxy is a dev-only convenience.
const API_TARGET = process.env.RADIO_DEV_API || "http://127.0.0.1:8000";
const REST_PATHS = [
  "/capabilities",
  "/status",
  "/ptt",
  "/transmit",
  "/frequency",
  "/channel",
  "/tone",
  "/mode",
  "/scan",
  "/services", // the Services card (list + trigger-by-digit)
  "/controller",
  "/link", // covers /link and /link/status by prefix (ADR 0041)
  "/auth", // covers /auth/totp by prefix (the login-code card)
  "/settings", // covers /settings and /settings/secrets/... by prefix (ADR 0026/0027)
];

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs so the bundle works regardless of the mount path FastAPI serves it from.
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      ...Object.fromEntries(
        REST_PATHS.map((p) => [p, { target: API_TARGET, changeOrigin: true }]),
      ),
      "/events": { target: API_TARGET, ws: true, changeOrigin: true },
      // Binary audio WebSockets (ADR 0023 RX playback; /audio/tx reserved for cycle 23).
      "/audio/rx": { target: API_TARGET, ws: true, changeOrigin: true },
      "/audio/tx": { target: API_TARGET, ws: true, changeOrigin: true },
    },
  },
});
