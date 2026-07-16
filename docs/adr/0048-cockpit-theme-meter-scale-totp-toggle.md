# ADR 0048: Military-night-cockpit theme, level-meter removal, and a TOTP auth on/off toggle

## Status

Accepted

## Context

Three operator-facing requests against the retro-ham web UI (ADR 0044) and the over-RF auth plane:

1. The Day theme reads well, but the Night theme's browns are not to taste; a "military night
   cockpit" look is wanted as an option — near-black panels with green phosphor instrument readouts
   and red warning lamps, same layout.
2. The Monitor (RX) and Transmit (MIC) level meters don't convey anything useful and are to be
   removed.
3. Some deployments want to run without over-the-air TOTP auth — callers issuing DTMF commands
   directly, no login code — with a clear on-screen tell that access is un-gated.

## Decision

### Military-night-cockpit theme (three themes, not two)

Keep Day and the brown Night; **add** a third `red` theme rather than replacing Night. The ADR 0044
token system already supports this: Day values live on `body`, overrides on `body[data-theme="…"]`.
We add a `body[data-theme="red"]` block overriding the same tokens, and the masthead toggle becomes a
3-way cycle (Day → Night → Red), still persisted in `localStorage["radio.theme"]` and applied before
first paint. The look is a military night cockpit: **near-black neutral backgrounds** (not
red-tinted), **green phosphor** on the instrument surfaces (the LCD readouts via `--lcd`/`--lcdink`,
the dial ticks, and the live/monitor `--green` state) plus dim-green secondary labels (`--muted`),
and **red** reserved for warnings — on-air, PTT, and the un-gated auth alert (`--red`) — and the
primary body text (`--ink`). The **amber-text rule** is preserved: elements on the amber gradient
keep literal `#3a1d0b` text, so the accent gradient (`--amber`/`--amber2`) is kept a light warm
red-orange so that dark text stays legible on it.

### Remove the level meters

The Monitor (RX) and Transmit (MIC) LED level meters conveyed nothing useful, so they are removed
outright: the `LevelMeter` component and its CSS, the two render sites, and the per-frame peak/level
tracking in the `useRxAudio`/`useTxAudio` hooks (which drove ~60 fps re-renders purely to feed the
bar). The audio paths themselves are untouched.

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
- The Monitor/Transmit panels no longer show a live audio level; Listen/Talk state is conveyed by the
  button state and the LIVE badge alone.
- No new runtime dependency. No JS test suite (ADR 0044) — theme/UI fidelity is verified by
  screenshot; the Python changes (gate bypass, build gate, `/auth/totp`) are unit-tested.
- `radio.toml.example` gains an `[auth]` section (regenerated from the schema).
