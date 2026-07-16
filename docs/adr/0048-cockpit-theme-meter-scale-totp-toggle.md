# ADR 0048: Red cockpit theme, meter dB scale, and a TOTP auth on/off toggle

## Status

Accepted

## Context

Three operator-facing requests against the retro-ham web UI (ADR 0044) and the over-RF auth plane:

1. The Day theme reads well, but the Night theme's browns are not to taste; a red "cockpit at
   night" look (red-on-near-black, night-vision friendly) is wanted as an option — same layout.
2. The Monitor (RX) and Transmit (MIC) level meters barely move: even at maxed volume the bar fills
   a sliver.
3. Some deployments want to run without over-the-air TOTP auth — callers issuing DTMF commands
   directly, no login code — with a clear on-screen tell that access is un-gated.

## Decision

### Red cockpit theme (three themes, not two)

Keep Day and the brown Night; **add** a third `red` theme rather than replacing Night. The ADR 0044
token system already supports this: Day values live on `body`, overrides on `body[data-theme="…"]`.
We add a `body[data-theme="red"]` block overriding the same ~30 tokens (red-on-near-black), and the
masthead toggle becomes a 3-way cycle (Day → Night → Red), still persisted in
`localStorage["radio.theme"]` and applied before first paint. The **amber-text rule** is preserved:
elements on the amber gradient keep literal `#3a1d0b` text, so the red theme's accent gradient
(`--amber`/`--amber2`) is kept a light warm red-orange so that dark text stays legible on it.

### Meter dB scale

The meters mapped a **linear** peak amplitude (0..1) straight to bar width. Speech/radio audio sounds
loud while its linear peak sits ~0.05–0.3, so the bar barely filled. Fix is display-only: a shared
`levelToPct(level)` (new `web/src/meterScale.js`) maps a **−60..0 dBFS** window onto 0..100%, the way
a real meter reads. Both call sites (Monitor, Transmit) use it; the hooks' attack/decay smoothing and
the underlying 0..1 level are unchanged. No backend involvement — the level is computed client-side
from the PCM streams.

### TOTP auth on/off toggle

A new schema setting **`auth.totp_enabled`** (default **on**) gates the over-RF TOTP/DTMF plane
(`radio_server.auth`); it does not touch the LAN bearer-token plane (`radio_server.api.auth`). When
off:

- `AuthGate` is built with `enforce=False` (and no verifier): `on_dtmf` implicitly authenticates the
  session and routes every keyed entry straight to command dispatch — no code, no burn, no rejection.
- `Controller.step` detects the implicit open (first command on an un-authenticated session) and
  arms the station ID + emits `session_open` with **no** login announcement, so automatic
  identification coverage stays intact: each command auto-IDs, the periodic-ID net runs, and idle
  timeout still signs off with a closing ID (Part 97, guardrail 5).
- `build_app` builds the controller when a TOTP secret is present **or** auth is disabled, so DTMF
  works un-gated even on a deployment that never enrolled a secret. A callsign (and a voice for voice
  services) is still required — you can never run un-ID'd.
- `GET /auth/totp` reports `{"enforced": false}` (reflecting the **running** controller, honest under
  restart-to-apply); the masthead chip then shows an open-padlock "no auth" indicator with a red
  pulse instead of a login code. When auth is on and no secret is enrolled, the endpoint still 503s
  and the chip stays hidden, as before.

Because the settings system is restart-to-apply (v1), the toggle takes effect on the next restart,
consistent with every other setting.

## Consequences

- **Posture (guardrail 4):** with `auth.totp_enabled` off, there is no access control on the RF
  plane — anyone in range can trigger any service and key the transmitter as the licensee's station.
  This is a deliberate, default-off operator opt-in; the setting description says so plainly and the
  UI's red "no auth" indicator makes the state obvious. Automatic station ID (guardrail 5) is
  unaffected and still fires. TX-capable services are no more exposed than the existing LAN-token
  transmit paths a token holder already has.
- The theme set is now three; future components keep using the token set and the amber-text rule.
- No new runtime dependency. No JS test suite (ADR 0044) — theme/meter/UI fidelity is verified by
  screenshot; the Python changes (gate bypass, build gate, `/auth/totp`) are unit-tested.
- `radio.toml.example` gains an `[auth]` section (regenerated from the schema).
