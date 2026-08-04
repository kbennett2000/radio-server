# Deployment — running the server headless (Linux)

> **Advanced.** This covers leaving radio-server running unattended on a Linux server. For everyday use
> on your own computer you don't need any of it — see **[Try it first](getting-started.md)** and
> **[Using your station](using-it.md)**.

This guide covers running radio-server as a long-lived service on a Linux host — the typical
setup, where the box with the radio and the AIOC cable sits on your LAN and the server runs
unattended. For first-time install and per-OS setup see [install.md](install.md); for the radio
bench bring-up see [hardware-bringup.md](hardware-bringup.md).

The server is a plain ASGI app run under uvicorn via `uv run python -m radio_server`. It has no built-in
process supervision or daemonization — those are the deployment layer's job, below. It *can* serve
TLS directly (optional; see §5) — which a **phone on the LAN needs** for Listen/Talk to work at all.

---

## 1. Bind to the LAN

By default the server binds loopback (`127.0.0.1`), which is safe but unreachable from other
machines. To serve the LAN, in `radio.toml`:

```toml
[server]
host = "0.0.0.0"
port = 8000
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
# `python -m radio_server` is the only supported entrypoint, and since ADR 0165 that matters for more
# than argument parsing: `main()` installs the log redaction that keeps the API token out of
# journald. A unit that invokes uvicorn directly (`uvicorn radio_server...:app`) skips it and the
# token goes back into the journal on every WebSocket connect.
Restart=on-failure
RestartSec=2
# Shutdown budget (ADR 0104): a clean stop closes the D-STAR bridge, the DV Dongle vocoder, the
# Mumble link, and the RX pump. Give SIGTERM room to finish; a SIGKILL that severs a USB vocoder
# (DV Dongle) mid-operation wedges the device until a re-open or power-cycle, so the stop timeout
# must never be the thing that fires first.
#
# This said "worst case well under 10 s". It is not, and ADR 0184 did the arithmetic: 5.0 graceful
# + 6.0 D-STAR + 2.0 Mumble + 2.0 cadence + 6.25 holder + 2.0 ledger = 23.25 s, plus radio.ptt() and
# radio.close() which carry no bound at all. The worst case needs D-STAR and Mumble both up and both
# wedged, which is why it has never fired — but do NOT read 20 s as a proven ceiling.
#
# And do not raise it. That was tried on this station: the deadline went 10 -> 20 and 31 more
# SIGKILLs followed at the new one. The kills stopped when GRACEFUL_SHUTDOWN_SECONDS bounded
# uvicorn's graceful phase in code (ADR 0127), not when the timeout got longer.
TimeoutStopSec=20
# A clean SIGTERM stop exits 143 (128+15), not 0: uvicorn restores the default signal handlers and
# re-raises the SIGTERM it captured, so the process dies with the signal's disposition on purpose.
# Declaring it keeps `systemctl stop` from reporting `Failed with result 'exit-code'` (ADR 0127).
# This is a truthful declaration, NOT a way to hide a bad shutdown — what guarantees the process is
# gone in time is the bounded graceful phase, not the number above (measured: 20.0 s + SIGKILL
# before, 6.5 s + clean exit after).
SuccessExitStatus=143

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
or `localhost`. Your PC works over plain `http://localhost:8000` because `localhost` is exempt; a
phone loading `http://<lan-ip>:8000` is **not** a secure context, so it can log in but cannot hear
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
insecure HTTP. On the phone browse `https://<lan-ip>:8000` and tap through the one-time "Your
connection is not private" warning (expected for a self-signed cert) — the origin is then secure and
Listen/Talk work.

> **Android/Chrome** accepts the click-through cert and then grants mic access. **iOS/Safari** is
> stricter and often still blocks the mic on a merely-accepted self-signed cert — trust the cert at
> the OS level (or use `mkcert`, or the reverse-proxy path below) for reliable iPhone use.

### 5b. Reverse proxy / real cert (no browser warning)

For a permanent install, terminate TLS at a reverse proxy (Caddy, nginx) or a tunnel
(**Tailscale Serve**) in front of the plain-HTTP server — you get a real, trusted cert and no
warning. Leave `tls_cert`/`tls_key` empty so the app stays HTTP behind the proxy. The one thing that
matters: the app has **seven WebSockets** and the proxy must pass the upgrade on **all** of them —

| Socket | Carries |
|---|---|
| `/events` | the live event stream every card in the UI is driven by |
| `/audio/rx`, `/audio/tx` | browser listen / talk on RF |
| `/audio/mumble/rx`, `/audio/mumble/tx` | browser listen / talk on a linked Mumble channel (ADR 0050) |
| `/audio/dstar/rx`, `/audio/dstar/tx` | browser listen / talk on a linked D-STAR reflector (ADR 0088) |

Proxy `/` as a whole rather than enumerating paths — an allow-list of a few of them is how the
Mumble and D-STAR audio ends up silently broken while the rest of the UI looks fine. nginx sketch:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;      # WebSocket upgrade — needed on all seven sockets
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

Caddy handles the upgrade automatically with a bare `reverse_proxy 127.0.0.1:8000`. TLS is a
transport wrapper only — nothing on RF is ever confidential (see
[operating.md](operating.md#security-reality)).

## 6. Updating the server

**Use the script:**

```sh
./update-radio-server.sh                      # fast-forward to origin/master, sync, build, restart
./update-radio-server.sh origin/some-branch   # deploy a specific ref instead
```

It is the four steps below with the two traps already handled — see
[server-notes.md](server-notes.md) for why a deployment is normally on a **detached HEAD** and why
`git pull` therefore cannot be the first step (ADR 0169).

By hand, the same four steps, and the second one has teeth:

```sh
git fetch origin && git switch --detach origin/master  # NOT `git pull`: see ADR 0169
uv sync --extra hardware --extra tts --extra mumble    # name EVERY extra you use — see below
cd web && npm run build && cd ..
systemctl --user restart radio-server                  # or: sudo systemctl restart radio-server
```

> **Why the extras must be repeated:** `uv sync` is **exact** — it removes every package not named
> on that invocation. A bare `uv sync` (or one missing an extra) silently *uninstalls* the extras
> you installed at setup, and the feature they back breaks at the next restart with an
> "install the extra" error. This is not hypothetical; it's how the Mumble link kept losing
> pymumble on updates. (`uv run` in the systemd unit is safe: its implicit sync is inexact and
> never removes packages.)

`update-radio-server.sh` in the repo root encodes this flow (with this deployment's extras) as
one command. It refuses a move that is **not a fast-forward** unless you name the target ref, so a
box deployed onto a bench branch is never yanked back to master by a routine update — and it resolves
`uv` explicitly, because `~/.local/bin` is only on `PATH` in a login shell.

## 7. Operational notes

- **The sound card is single-open.** With a hardware backend, the running server owns the AIOC
  capture device. The `doctor` audio tools (`--rx-level`, `--tx-tone`) can't open it at the same
  time — **stop the service first** (`systemctl stop radio-server`) before running them, then start
  it again.
- **Rotate the operating log.** `logging.path` (default `radio-server.jsonl`) is an append-only
  JSONL ledger that grows without bound. Add a `logrotate` rule (or point it somewhere you rotate);
  the server reopens it fail-loud at startup if the path is unwritable.
- **Recordings grow too.** If `recording.enabled`/`recording.tx` are on, `recording.path` fills with
  WAV segments capped only by `recording.max_seconds` — provision disk and prune.
- **Backends:** `mock`, `baofeng`, `kv4p` and `uvk5` work today — the kv4p is an ESP32+SA818 board on
  one USB-UART ([Setting up a KV4P HT board](kv4p-setup.md)), and `uvk5` is a Quansheng UV-K5/K6 on
  a dock firmware over an AIOC — nicsure's for a classic radio, our V3 fork for a UV-K5 V3
  ([Setting up a UV-K5](uvk5-setup.md)). `server.backend = "v71"` raises
  `NotImplementedError` (the Kenwood TM-V71A/TM-D710-family backend is still a stub).
- **Switching backends live has a known crasher.** `POST /radio/select` from `baofeng` to `uvk5`
  segfaults the process (ADR 0140/0141/0142; still open). systemd restarts it, but any transmission
  in flight is lost. Set `server.backend` in `radio.toml` and restart if you need that transition.

## See also

- [install.md](install.md) — cross-platform install & configuration.
- [hardware-bringup.md](hardware-bringup.md) — AIOC wiring and the empirical bring-up flow.
- [operating.md](operating.md) — Part-97 behavior, the two auth planes, security reality.
