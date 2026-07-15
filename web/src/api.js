// REST client for the radio-server API (ADR 0022).
//
// One thin wrapper around fetch that attaches the in-memory bearer token and maps the API's
// documented status codes to typed errors the UI can branch on:
//   401 -> Unauthorized            (bad/missing token -> back to the token gate)
//   501 -> Unsupported(capability) (CAT method on an audio-only radio -> grey that control)
//   503 -> ControllerUnavailable   (POST /controller with no controller wired)
// Everything is same-origin: the SPA is served by FastAPI in prod and proxied by Vite in dev, so
// relative paths ("/status", ...) always resolve to the API.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export class Unauthorized extends ApiError {
  constructor() {
    super("Invalid or missing API token", 401);
    this.name = "Unauthorized";
  }
}

export class Unsupported extends ApiError {
  // `capability` is the machine-readable enum the API names in the 501 body
  // (e.g. "set_frequency"); the UI greys exactly that control.
  constructor(capability) {
    super(`Not supported on this radio: ${capability ?? "unknown"}`, 501);
    this.name = "Unsupported";
    this.capability = capability ?? null;
  }
}

export class ControllerUnavailable extends ApiError {
  constructor(detail) {
    super(detail || "Controller not configured in this deployment", 503);
    this.name = "ControllerUnavailable";
  }
}

// Build a client bound to a token. Kept in a closure so the token never lives in a global or
// in storage — it exists only for the lifetime of the React state that holds it.
export function makeClient(token) {
  async function request(method, path, body) {
    const opts = { method, headers: { Authorization: `Bearer ${token}` } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(`Network error: ${e.message}`, 0);
    }

    if (res.status === 401) throw new Unauthorized();
    if (res.status === 501) {
      const cap = await readCapability(res);
      throw new Unsupported(cap);
    }
    if (res.status === 503) {
      throw new ControllerUnavailable(await readDetail(res));
    }
    if (!res.ok) {
      throw new ApiError(`Request failed (${res.status}): ${await readDetail(res)}`, res.status);
    }
    // 200 bodies are always JSON on this API.
    return res.status === 204 ? null : res.json();
  }

  return {
    token,
    capabilities: () => request("GET", "/capabilities"),
    status: () => request("GET", "/status"),
    ptt: (on) => request("POST", "/ptt", { on }),
    frequency: (hz) => request("POST", "/frequency", { hz }),
    channel: (n) => request("POST", "/channel", { n }),
    // tone accepts a float to set or null to clear.
    tone: (tone) => request("POST", "/tone", { tone }),
    mode: (mode) => request("POST", "/mode", { mode }),
    // Scan is async (ADR 0028): POST /scan starts a background scan (409 if one is already running),
    // POST /scan/stop ends it. The live phase and running state come over /events.
    scan: (plan) => request("POST", "/scan", plan),
    scanStop: () => request("POST", "/scan/stop"),
    controller: (on) => request("POST", "/controller", { on }),
    // The DTMF services/commands wired in this deployment, and firing one over the air by digit
    // (the web trigger panel). triggerService transmits immediately — the token is the operator's
    // credential, like ptt/transmit. 503 (ControllerUnavailable) when no controller is configured.
    services: () => request("GET", "/services"),
    triggerService: (digit) => request("POST", `/services/${digit}`),
    // Tier-0 channel-activity rollup (ADR 0039/0040): "is this channel actually dead?" A zeroed
    // summary (empty ledger) is a normal 200, not an error — the card renders it, not a failure.
    activitySummary: () => request("GET", "/activity/summary"),
    // Settings surface (ADR 0026/0027). The schema drives the UI; PATCH sends only changed keys.
    settings: () => request("GET", "/settings"),
    updateSettings: (values) => request("PATCH", "/settings", { values }),
    // Write-only secret rotation — a bodyless POST when no explicit value is given (the server
    // generates one). The returned secret is shown to the operator exactly once.
    rotateApiToken: (token) =>
      request("POST", "/settings/secrets/api-token/rotate", token ? { token } : undefined),
    enrollTotp: (account) =>
      request("POST", "/settings/secrets/totp/enroll", account ? { account } : undefined),
  };
}

// The 501 body is `{"detail": {"error": ..., "capability": "set_frequency"}}`.
async function readCapability(res) {
  try {
    const body = await res.json();
    return body?.detail?.capability ?? null;
  } catch {
    return null;
  }
}

// FastAPI error bodies are `{"detail": ...}` where detail is a string or object.
async function readDetail(res) {
  try {
    const body = await res.json();
    const d = body?.detail;
    return typeof d === "string" ? d : d ? JSON.stringify(d) : res.statusText;
  } catch {
    return res.statusText;
  }
}
