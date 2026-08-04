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

**The unit ships with the code: [`scripts/radio-server.service`](../scripts/radio-server.service).**
Copy it and adjust `WorkingDirectory`/`ExecStart` for your host; the install commands for both user
scope and system scope are in its header.

It used to be a fenced block right here, and that was a real problem rather than an untidiness: the
docs contract test deliberately blanks fenced code blocks, so **nothing in the repo could see the one
number the whole stop budget is sized against**, and it drifted. Now `TimeoutStopSec` in the shipped
file is asserted against `radio_server.shutdown.stop_budget_seconds()` by
`tests/test_the_stop_budget_fits_its_deadline.py`, and `scripts/bench/acceptance.py` checks what is
actually *installed* on the box — because shipping a file does not make a machine adopt it.

### The stop budget, and why `TimeoutStopSec` is 35 (ADR 0185)

Derived, not chosen. `stop_budget_seconds()` sums every bound a stop can spend and the identity test
asserts the shipped deadline covers it plus an explicit margin. At the shipped defaults:

| | s |
|---|---|
| signal delivery (loop notices SIGTERM) — measured max over n=201 | 0.20 |
| uvicorn graceful window (`GRACEFUL_SHUTDOWN_SECONDS`) | 5.00 |
| the teardown's per-step bounds (`teardown_budget_seconds()`) | 25.25 |
| process exit reserve — measured | 0.25 |
| explicit margin | 2.00 |
| **required** | **32.70** |
| **shipped** | **35** |

Raise `dstar.tx_hang` and you raise the requirement; the test will say so.

**This is a raise, and it is not the raise this station's history refuted.** The journal records
`TimeoutStopSec` going 10 → 20 against an *unbounded* graceful window — the deadline moved, nothing
else did, and 31 more SIGKILLs followed at the new one. What fixed that was bounding the window in
code (ADR 0127). This number is sized to a *finite, enumerated* budget that a test adds up. Do not
raise it against a stall; derive the stall's bound instead.

What a longer deadline costs is only a delay to SIGKILL, which fires when the teardown has already
failed — and the measured teardown is 241 ms at its worst over n=201 stops.

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
