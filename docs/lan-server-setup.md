# Setting up radio-server on a LAN server

A start-to-finish runbook for moving radio-server off your desktop and onto the box that will run it
unattended — the machine with the radio and the AIOC cable, reachable from your phone and other
computers on the LAN over **HTTPS**.

The worked example throughout uses a server at **`192.168.1.62`**, hostname **`radio.local`**, the
**Baofeng/AIOC** backend, and the default port **`8090`**. Substitute your own values.

> **Why HTTPS matters here.** Browsers only allow the microphone and live audio (Talk and Listen) in
> a *secure context*: HTTPS, or `localhost`. Your desktop works over plain `http://localhost:8090`
> because `localhost` is exempt. A phone loading `http://192.168.1.62:8090` is **not** a secure
> context — it can log in but **cannot hear or transmit**. Serving HTTPS (step 4) fixes that. See
> [ADR 0039](adr/0039-https-secure-context.md).

---

## What you need on the server

- A Linux box (your `.62` machine) with the **AIOC cable and radio plugged in**.
- `git` (or `rsync`), **`uv`**, and **Node/npm** — plus the audio system packages (PortAudio/ALSA,
  `multimon-ng`, optionally `piper`). Follow [install.md](install.md) for the per-OS package list.
- Your service user in the **`dialout`** group (needed for AIOC serial PTT):
  `sudo usermod -aG dialout $USER` then log out/in.

---

## 1. Get the code onto the server

From your desktop, or by cloning on the server:

```sh
# On the server:
git clone https://github.com/kbennett2000/radio-server.git /opt/radio-server
cd /opt/radio-server

# ...or copy your working tree over (rsync from the desktop):
# rsync -av --exclude .venv --exclude web/dist --exclude 'radio-secrets.toml' \
#   ~/Desktop/projects/radio-server/  user@192.168.1.62:/opt/radio-server/
```

Any path works; this guide uses `/opt/radio-server`. If you keep the desktop's exact path
(`/home/you/Desktop/projects/radio-server`), the absolute paths in `radio.toml` (below) carry over
unchanged — otherwise update `web_dir` and `tts.voice` to the new location.

## 2. Install dependencies and build the web UI

```sh
cd /opt/radio-server
uv sync                              # Python deps into .venv
cd web && npm install && npm run build   # builds web/dist (served by the app)
cd ..
```

The server serves the built UI from `server.web_dir` (default `<repo>/web/dist`). An unbuilt
directory serves a "run the build" placeholder rather than crashing, so if a page looks empty, you
skipped `npm run build`.

## 3. Secrets — and your phone's authenticator

The two secrets never live in `radio.toml`. Put them in `radio-secrets.toml` **mode 0600** (the
loader refuses a group/world-readable file), or provide them as `RADIO_API_TOKEN` /
`RADIO_TOTP_SECRET` env vars.

```toml
# /opt/radio-server/radio-secrets.toml   (chmod 600)
api_token   = "a-long-random-lan-token"
totp_secret = "JBSWY3DPEHPK3PXP"
```

```sh
chmod 600 radio-secrets.toml
```

### Do I need to re-enroll my authenticator?

**No — as long as you carry over the same `totp_secret`.** A TOTP code depends only on the shared
secret and the current time; it is **not** tied to the host or the port. Copy the same `totp_secret`
(and `api_token`) your desktop used and:

- your phone's **authenticator keeps working** — no re-enrollment, and
- your saved **API token still logs you in** — no re-entry.

You only re-enroll (or re-enter the token) if you deliberately **generate a new secret** on the
server. If you do want a fresh code, enroll from the web UI: **Settings → rotate the TOTP secret**,
which prints a QR / `otpauth://` URI to scan with Google Authenticator. (Moving machines or changing
the port on its own never requires re-enrollment.)

## 4. Generate the HTTPS certificate

Run the helper on the server, passing the **exact LAN IP** your phone will type and the hostname —
they become the certificate's SANs, which must match the address in the URL bar:

```sh
scripts/gen-selfsigned-cert.sh 192.168.1.62 radio.local
```

It writes `radio-cert.pem` / `radio-key.pem` (key `0600`) and prints the two absolute paths to paste
into `radio.toml` next. Self-signed means a **one-time browser warning** on the phone, which is
expected — you tap through it once (see step 7).

> No-warning alternatives (real trusted cert): terminate TLS at a reverse proxy or a **Tailscale
> Serve** tunnel in front of the plain-HTTP server. See [deployment.md §5b](deployment.md). On
> **iOS/Safari** a click-through self-signed cert often still blocks the mic — trust the cert at the
> OS level, use `mkcert`, or use the proxy/Tailscale route.

## 5. Write `radio.toml`

Point `[server]` at the LAN, the built UI, and the cert from step 4. A minimal server block:

```toml
[server]
backend  = "baofeng"
host     = "0.0.0.0"                                   # serve the LAN, not just loopback
port     = 8090                                        # the default; browse https://<ip>:8090
web_dir  = "/opt/radio-server/web/dist"                # must exist on THIS box (step 2)
tls_cert = "/opt/radio-server/radio-cert.pem"          # from step 4
tls_key  = "/opt/radio-server/radio-key.pem"           # from step 4
```

Set **both** `tls_cert` and `tls_key` to serve HTTPS; leave both empty for plain HTTP. Setting only
one, or an unreadable path, **fails loud at startup** rather than silently serving insecure HTTP.

For the rest of the file (station callsign, `[baofeng]` device names, `[tts]` voice path, the
service `base_url`s, announcements), start from [`radio.toml.example`](../radio.toml.example) or copy
your desktop's `radio.toml` and fix any absolute paths for the new location. Two things to check when
moving:

- **`tts.voice`** and any other absolute paths must point at files that exist **on the server**.
- The service **`base_url`s** (weather/quote/battery/bible) that pointed at `192.168.1.62` are now on
  the same host — they keep working via that IP (or you can switch them to `127.0.0.1`).

## 6. Run it

Try it in the foreground first to confirm it binds HTTPS and finds the radio:

```sh
uv run python -m radio_server --config radio.toml --secrets radio-secrets.toml
```

Then install it as a service so it survives reboots and restarts on failure — the systemd unit,
log/recording rotation, and the single-open sound-card caveat are all in
**[deployment.md](deployment.md)** (§4 and §6). Config changes take effect on **restart**; there is
no hot-reload.

## 7. Connect from your phone

1. On the phone (Android/Chrome), browse to **`https://192.168.1.62:8090`** — note **https**, and
   the IP that matches the cert.
2. You'll get a one-time **"Your connection is not private"** warning (expected for a self-signed
   cert). Tap **Advanced → Proceed**.
3. Log in with your **API token**, then use **Listen** and **Talk** — they now work because the page
   is a secure context.

---

## Troubleshooting

- **Logs in on the phone but can't hear or transmit.** You're on `http://`, or you didn't accept the
  cert. The address must be `https://192.168.1.62:8090` and the cert warning must be cleared. A red
  banner in the UI ("this page isn't secure") confirms an insecure origin.
- **The phone can't reach the page at all.** Confirm `host = "0.0.0.0"` and that the server's
  firewall allows the port. The desktop still works on `http://localhost:8090` regardless.
- **"No input/output device matching …".** The AIOC name/index differs on this box — run
  `python -m radio_server.doctor` to print the exact device to put in `[baofeng]`.
- **`doctor` audio tools say the device is busy.** The sound card is single-open; **stop the
  service** before running `doctor --rx-level` / `--tx-tone`, then start it again.
- **Startup aborts naming `server.tls_cert`/`tls_key`.** Either only one is set, or the file isn't
  readable — set both (or clear both), and check the paths and permissions.

## See also

- [deployment.md](deployment.md) — systemd, HTTPS options in depth, reverse proxy / Tailscale,
  operational notes.
- [install.md](install.md) — per-OS packages and first-run setup.
- [operating.md](operating.md) — the two auth planes (LAN token + over-the-air TOTP), Part-97 behavior.
- [ADR 0039](adr/0039-https-secure-context.md) — why HTTPS is required for phone audio.
