# Deployment — running the server headless (Linux)

> **Advanced.** This covers leaving radio-server running unattended on a Linux server. For everyday use
> on your own computer you don't need any of it — see **[Try it first](getting-started.md)** and
> **[Using your station](using-it.md)**.

This guide covers running radio-server as a long-lived service on a Linux host — the typical
setup, where the box with the radio and the AIOC cable sits on your LAN and the server runs
unattended. For first-time install and per-OS setup see [install.md](install.md); for the radio
bench bring-up see [hardware-bringup.md](hardware-bringup.md).

> **New to this?** [**lan-server-setup.md**](lan-server-setup.md) is a start-to-finish runbook —
> move the code over, build, secrets (and whether you re-enroll your authenticator), the HTTPS cert,
> `radio.toml`, run it, and connect from your phone. This page is the reference it links into.

The server is a plain ASGI app run under uvicorn via `python -m radio_server`. It has no built-in
process supervision or daemonization — those are the deployment layer's job, below. It *can* serve
TLS directly (optional; see §5) — which a **phone on the LAN needs** for Listen/Talk to work at all.

---

## 1. Bind to the LAN

By default the server binds loopback (`127.0.0.1`), which is safe but unreachable from other
machines. To serve the LAN, in `radio.toml`:

```toml
[server]
host = "0.0.0.0"
port = 8090
```

The HTTP/WebSocket API is **closed by default** — the server refuses to bind without a
`RADIO_API_TOKEN` (see Secrets below). Anyone on the LAN with the token can drive the API, so treat
the token as the LAN gate and keep the host on a trusted network. (Over-the-air keying is separately
gated by TOTP — see [operating.md](operating.md).)

## 2. Build the web UI for production

The Python server serves the built SPA from `server.web_dir` (default `<repo>/web/dist`). Build it
once as part of deployment:

```sh
cd web && npm install && npm run build     # -> web/dist/
```

If you deploy the built bundle to a different location, point `server.web_dir` at it. An unbuilt
directory serves a "run the build" placeholder rather than crashing.

## 3. Secrets

The two secrets never live in `radio.toml`. Provide them one of two ways:

- **`radio-secrets.toml`** — the loader **requires mode `0600`** (it refuses a group/world-readable
  file). Point at a non-default path with `--secrets PATH`.

  ```toml
  # /etc/radio-server/radio-secrets.toml   (chmod 600, owned by the service user)
  api_token   = "a-long-random-lan-token"
  totp_secret = "JBSWY3DPEHPK3PXP"
  ```

- **Environment variables** — `RADIO_API_TOKEN` and `RADIO_TOTP_SECRET`. Under systemd, keep these
  in a root-owned `EnvironmentFile` (chmod 600), not inline in the unit.

## 4. Run under systemd

A minimal unit. Adjust the user, paths, and config location to your host:

```ini
# /etc/systemd/system/radio-server.service
[Unit]
Description=radio-server
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=radio
Group=radio
# 'dialout' is needed for AIOC serial PTT; the primary group is set above.
SupplementaryGroups=dialout
WorkingDirectory=/opt/radio-server
# uv resolves the project's venv from WorkingDirectory. Use an absolute uv path if it's not on PATH.
ExecStart=/usr/bin/uv run python -m radio_server --config /etc/radio-server/radio.toml --secrets /etc/radio-server/radio-secrets.toml
# ...or drop --secrets and provide the two RADIO_* secrets via an EnvironmentFile instead:
# EnvironmentFile=/etc/radio-server/secrets.env
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now radio-server
journalctl -u radio-server -f          # follow logs
```

Config changes take effect on **restart** (`systemctl restart radio-server`) — the server composes
its config once at startup; there is no hot-reload.

## 5. HTTPS

**You need HTTPS to use the web UI from a phone.** Browsers only expose the microphone
(`getUserMedia`) and `AudioWorklet` — i.e. **Talk** and **Listen** — in a *secure context*: HTTPS,
or `localhost`. Your PC works over plain `http://localhost:8090` because `localhost` is exempt; a
phone loading `http://<lan-ip>:8090` is **not** a secure context, so it can log in but cannot hear
or transmit (see ADR 0039). There are two ways to give the phone HTTPS.

### 5a. Built-in TLS (self-signed — simplest, works today)

radio-server can serve TLS itself. Generate a self-signed cert/key (the SANs must include the exact
LAN IP the phone types in the URL bar):

```sh
scripts/gen-selfsigned-cert.sh 192.168.1.62 radio.local
```

Point `radio.toml` at the files it prints, then restart:

```toml
[server]
tls_cert = "/abs/path/radio-cert.pem"
tls_key  = "/abs/path/radio-key.pem"
```

Setting **both** switches the server to HTTPS; leaving both empty keeps plain HTTP (the default).
Setting only one, or an unreadable path, **fails loud at startup** rather than silently serving
insecure HTTP. On the phone browse `https://<lan-ip>:8090` and tap through the one-time "Your
connection is not private" warning (expected for a self-signed cert) — the origin is then secure and
Listen/Talk work.

> **Android/Chrome** accepts the click-through cert and then grants mic access. **iOS/Safari** is
> stricter and often still blocks the mic on a merely-accepted self-signed cert — trust the cert at
> the OS level (or use `mkcert`, or the reverse-proxy path below) for reliable iPhone use.

### 5b. Reverse proxy / real cert (no browser warning)

For a permanent install, terminate TLS at a reverse proxy (Caddy, nginx) or a tunnel
(**Tailscale Serve**) in front of the plain-HTTP server — you get a real, trusted cert and no
warning. Leave `tls_cert`/`tls_key` empty so the app stays HTTP behind the proxy. The one thing that
matters: the app has **three WebSockets** — `/events`, `/audio/rx`, `/audio/tx` — so the proxy must
pass the WebSocket upgrade. nginx sketch:

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;      # WebSocket upgrade for /events, /audio/rx, /audio/tx
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

Caddy handles the upgrade automatically with a bare `reverse_proxy 127.0.0.1:8090`. TLS is a
transport wrapper only — nothing on RF is ever confidential (see
[operating.md](operating.md#security-reality)).

## 6. Operational notes

- **The sound card is single-open.** With a hardware backend, the running server owns the AIOC
  capture device. The `doctor` audio tools (`--rx-level`, `--tx-tone`) can't open it at the same
  time — **stop the service first** (`systemctl stop radio-server`) before running them, then start
  it again.
- **Rotate the operating log.** `logging.path` (default `radio-server.jsonl`) is an append-only
  JSONL ledger that grows without bound. Add a `logrotate` rule (or point it somewhere you rotate);
  the server reopens it fail-loud at startup if the path is unwritable.
- **Recordings grow too.** If `recording.enabled`/`recording.tx` are on, `recording.path` fills with
  WAV segments capped only by `recording.max_seconds` — provision disk and prune.
- **Backends:** only `mock` and `baofeng` work today. `server.backend = "v71"` raises
  `NotImplementedError` (the TM-V71A backend is still a stub).

## See also

- [install.md](install.md) — cross-platform install & configuration.
- [hardware-bringup.md](hardware-bringup.md) — AIOC wiring and the empirical bring-up flow.
- [operating.md](operating.md) — Part-97 behavior, the two auth planes, security reality.
