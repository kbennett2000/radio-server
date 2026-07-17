# Mumble server: technical reference

New here? Start at [the friendly guide](README.md) — this page is the deep end.

Stand up a Mumble voice server on a fresh Debian/Ubuntu cloud box in about a
minute. Written for RackNerd, works anywhere with apt + systemd.

Verified end-to-end against `mumble-server 1.5.517-1ubuntu2` on Ubuntu 24.04:
config generated, server booted, and real clients logged in to confirm every
claim below.

```
sudo ./setup-mumble.sh
```

Idempotent — re-run any time to change settings. Never produces duplicate keys.

## Usage

Interactive:

```bash
sudo ./setup-mumble.sh
```

Unattended:

```bash
sudo MUMBLE_NONINTERACTIVE=1 \
     MUMBLE_SERVER_NAME='AE9S Mumble' \
     MUMBLE_JOIN_PASS='hunter2' \
     MUMBLE_SUPERUSER_PASS='...' \
     MUMBLE_USERNAME_MODE=us \
     ./setup-mumble.sh
```

| Variable | Default | Notes |
|---|---|---|
| `MUMBLE_SUPERUSER_PASS` | generated | Admin password. Stored only as a hash — save it. |
| `MUMBLE_JOIN_PASS` | generated | `-` disables it (open server). |
| `MUMBLE_SERVER_NAME` | `Mumble` | Root/server name. |
| `MUMBLE_WELCOME` | from name | HTML allowed. |
| `MUMBLE_MAX_USERS` | `20` | |
| `MUMBLE_PORT` | `64738` | tcp + udp |
| `MUMBLE_USERNAME_MODE` | `none` | `none` \| `us` \| `intl` \| `custom` |
| `MUMBLE_USERNAME_REGEX` | — | Required for `custom`. |
| `MUMBLE_SKIP_FIREWALL` | `0` | `1` leaves ufw alone. |
| `MUMBLE_NONINTERACTIVE` | `0` | `1` never prompts. |

## Username policy (amateur radio)

`MUMBLE_USERNAME_MODE=us` requires every username to be a callsign, optionally
followed by a parenthetical so one operator can connect from several devices:

```
AE9S                    allowed
AE9S (phone)            allowed
AG1I (radio server)     allowed
KB                      denied
notacall                denied
```

`intl` accepts international callsign forms. `custom` takes your own regex.

Check names against the live config before inviting anyone:

```bash
./check-username.sh 'AE9S (phone)' 'W1AW/4'
```

**This enforces well-formed, not licensed.** Nothing stops someone typing
`W1AW`. Real verification against a roster or the FCC ULS needs an Ice
authenticator — the only place your code runs at login.

## Why the script looks like this

Each of these is a real behaviour of mumble-server 1.5.517, confirmed by
booting the server and logging in — not read off a wiki.

**The config path moved.** `1.3.x` used `/etc/mumble-server.ini`; `1.5.x` uses
`/etc/mumble/mumble-server.ini`. Writing to the wrong one leaves an orphan file
nothing reads, and the server starts happily on defaults. The script reads the
path out of the systemd unit instead of guessing.

**`[Ice]` must stay last.** Keys placed after that header get scoped to Ice and
are silently ignored. The editor always inserts above it.

**A comma in `serverpassword`, unquoted, disables the password entirely.** Qt
parses `a,b,c` as a list; read back as a string it comes out empty. The server
then accepts *any* password, including none, and logs nothing:

| ini line | wrong password |
|---|---|
| `serverpassword=pass,with,commas` | **accepted — server is open** |
| `serverpassword="pass,with,commas"` | rejected |

**A semicolon truncates the value.** `serverpassword=pass;word` becomes `pass`.
`;` is the real comment character, despite the docs saying `#`. This bites
`welcometext` too — any HTML entity (`&nbsp;`) would cut the message short.

The script auto-quotes any value containing `,` or `;`. That rule is
load-bearing, not cosmetic.

**A rejected value doesn't stop the server.** It logs
`Configuration variable "x" is of invalid format`, reverts to the default, and
carries on — so a username policy you think is active may not be. The script
greps the journal for this after restart and tells you.

**SuperUser is exempt from the username regex.** The server special-cases it
before the regex runs, so you cannot lock yourself out of admin this way.

**Mumble full-matches the username regex.** Bare `AE9S` rejects `xxAE9S`;
anchors are optional. The shipped patterns keep `^...$` for readability.

**SSH is allowed before ufw is enabled**, on its real port (read from `sshd -T`,
not assumed to be 22). It's the one step here that can lock you out for good.

## After setup

1. Connect with the Mumble client. Accept the certificate warning — the server
   generated its own, and it's yours. It won't ask again.
2. Right-click your name → **Register**.
3. Reconnect as `SuperUser`, right-click **Root** → **Edit** → **Groups** →
   `admin`, add your name, OK.
4. Reconnect as yourself. You're admin; you shouldn't need SuperUser again.

Registered names bind to a client certificate, so each device is a separate
registration. `AE9S` and `AE9S (phone)` are two users — add both to `admin` if
you want rights from both.

Add channels: right-click **Root** → **Add**. Leave "Temporary" unchecked.

## Troubleshooting

```bash
systemctl status mumble-server
journalctl -u mumble-server -n 40 --no-pager
journalctl -u mumble-server --no-pager | grep 'invalid format'   # silent config rejects
./check-username.sh 'somename'
```

`Main process exited, code=exited, status=15` on restart is the old process
taking SIGTERM. Harmless — the packaging just doesn't declare
`SuccessExitStatus=15`.

`Failed to set IPV6_RECVPKTINFO` is expected with `host=0.0.0.0` (IPv4 only).

`Registration needs nonempty 'registername'...` is the public server directory
declining to list you. That's the intended outcome for a private server.

The pristine config is preserved at `<ini>.orig` on first run, and the previous
version at `<ini>.bak` on every run.

## Notes

Firewall: `22/tcp` (or your real SSH port), plus `64738/tcp` and `64738/udp`.
Both protocols matter — Mumble does control over TCP and voice over UDP, and it
silently falls back to TCP-tunnelled voice if UDP is blocked. That fallback
works, which is why it's a trap: you get worse latency and no error.

TLS: the server self-signs on first boot. For a Let's Encrypt cert, point
`sslCert`/`sslKey` at your fullchain and privkey, make the key readable by the
`mumble-server` user, and reload on renewal.
