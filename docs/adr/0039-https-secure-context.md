# 0039 — Serve the web UI over HTTPS so live audio works from a phone (secure context)

Status: Accepted

## Context

An operator can open the web UI from an Android phone on the LAN and **log in**, but cannot
**hear**, **transmit**, or (usefully) **control** the radio. On the local PC everything works.

The cause is not a bug in radio-server. Both live-audio paths use browser Web APIs that are
**gated behind a secure context** — HTTPS, with a standard exemption for `localhost`/`127.0.0.1`:

- **Receive** (`web/src/useRxAudio.js`): `AudioContext.audioWorklet.addModule(...)`. `AudioWorklet`
  is secure-context-only. On an insecure origin `ctx.audioWorklet` is `undefined`, so `listen()`
  throws and falls back to idle with only a `console.error` — invisible on a phone.
- **Transmit** (`web/src/useTxAudio.js`): `navigator.mediaDevices.getUserMedia(...)`, also
  secure-context-only. On an insecure origin `navigator.mediaDevices` is `undefined`, so `startTalk()`
  throws and (misleadingly) reports "Microphone permission denied".
- **Control**: in Baofeng mode the CAT tuning cards are greyed by design (ADR 0029 capability split),
  so the only live control is Talk/PTT — dead for the reason above.

The **PC works** because it loads `http://localhost:8090`; browsers treat `localhost` as a secure
context. The **phone** loads `http://192.168.x.x:8090` — a plain-HTTP LAN origin, which is **not**
secure — so those two APIs disappear. Login, `/events`, and status keep working because they use
plain `fetch`/`WebSocket`, which are not gated.

The frontend and backend are already fully same-origin (no hardcoded `localhost`; every URL is built
from `window.location`), and `radio.toml` already binds `host = "0.0.0.0"`, so reachability is fine.
What the phone lacks is a **secure origin scheme**. The only real fix is to serve the UI over HTTPS.

## Decision

Let radio-server serve TLS directly, as an **opt-in**, so a phone can load `https://<lan-ip>:8090`
and get a secure context.

- **Two new optional settings** (`radio_server/config/spec.py`), both empty-string by default:
  - `server.tls_cert` — env `RADIO_TLS_CERT` — PEM certificate path.
  - `server.tls_key` — env `RADIO_TLS_KEY` — PEM private-key path.
- **Entrypoint wiring** (`radio_server/__main__.py`): when **both** are set, pass
  `ssl_certfile`/`ssl_keyfile` to `uvicorn.run(...)`; otherwise serve plain HTTP exactly as before.
  A **half-configured** TLS setup (only one of the two set) or a **missing/unreadable** cert/key
  **fails loud at startup** rather than silently downgrading to HTTP — consistent with the "never
  binds open by accident" posture (ADR 0025).
- **Self-signed by default, Android/Chrome first.** A `scripts/gen-selfsigned-cert.sh` helper emits a
  cert with the LAN IP and hostname as SANs. On Android Chrome the operator taps through the one-time
  "Your connection is not private" warning; the origin is then a secure context and Listen/Talk work.
  This is a per-deployment fact (guardrail 1): the cert paths are marked-default config with a
  "verify against your host" note, not a baked-in assumption.
- **A UI safety net.** The control panel shows a banner when `!window.isSecureContext`, explaining
  that Listen/Talk need HTTPS — so an insecure origin explains itself instead of failing silently
  (RX) or with a wrong "mic denied" message (TX).

## Alternatives considered

- **A reverse proxy / tunnel (Caddy, nginx, Tailscale Serve).** Gives a *real* cert and no browser
  warning, and is the recommended path for a permanent install — but it needs extra software or an
  account and a trusted hostname. Kept as documented follow-up, not the built-in default: the goal
  here is "one operator, one LAN, works today with no external dependency."
- **Trust the self-signed cert at the OS level / mkcert.** More reliable on stricter browsers (iOS
  Safari in particular often refuses `getUserMedia` on a merely-click-through self-signed cert). Out
  of scope for the Android/Chrome target confirmed for this change; noted in the docs as the iOS
  path.
- **Do nothing / "use the PC".** Rejected — the phone is the intended field client.

## Consequences

- **Listen and Talk work from the phone** once TLS is configured and the cert is accepted once.
- **HTTP stays the default and is unchanged.** With the two settings empty, the server behaves
  exactly as before — the PC on `http://localhost:8090` keeps working with no cert at all.
- **Self-signed means a one-time browser warning** on the phone, and (guardrail 1) the cert must
  carry the right SANs for the host it is served from; the helper script and docs cover this. iOS and
  no-warning setups are documented follow-ups (OS-trusted cert / mkcert / Tailscale / reverse proxy).
- **A half-configured or missing cert fails loud** at startup instead of silently serving insecure
  audio-less HTTP — the failure names the problem.
