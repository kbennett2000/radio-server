#!/usr/bin/env bash
#
# setup-mumble.sh — stand up a Mumble voice server on a fresh Debian/Ubuntu
# cloud box (RackNerd, Hetzner, DigitalOcean, Vultr, ...).
#
# Idempotent. Re-run any time to change settings.
#
#   sudo ./setup-mumble.sh                       # interactive
#   sudo MUMBLE_JOIN_PASS=hunter2 ./setup-mumble.sh
#
# Environment variables (optional; prompts or defaults fill the gaps):
#   MUMBLE_SUPERUSER_PASS   admin password           (default: generated)
#   MUMBLE_JOIN_PASS        password to join         (default: generated; "-" = open)
#   MUMBLE_SERVER_NAME      server / root name       (default: Mumble)
#   MUMBLE_WELCOME          welcome message, HTML ok (default: from name)
#   MUMBLE_MAX_USERS        concurrent users         (default: 20)
#   MUMBLE_PORT             listen port              (default: 64738)
#   MUMBLE_USERNAME_MODE    none|us|intl|custom      (default: none)
#   MUMBLE_USERNAME_REGEX   required when MODE=custom
#   MUMBLE_SKIP_FIREWALL    1 = don't touch ufw
#   MUMBLE_NONINTERACTIVE   1 = never prompt
#
set -euo pipefail

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=''; G=''; Y=''; R=''; N=''; fi
say()  { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s[ ok ]%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s[fail]%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ]              || die "Run as root:  sudo $0"
command -v apt-get   >/dev/null   || die "Needs Debian/Ubuntu (no apt-get)."
command -v systemctl >/dev/null   || die "Needs systemd."

INTERACTIVE=1
[ "${MUMBLE_NONINTERACTIVE:-0}" = "1" ] && INTERACTIVE=0
[ -t 0 ] || INTERACTIVE=0

gen_pass() { tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16; }
ask() {
    local __v="$1" __p="$2" __d="${3:-}" __r=""
    [ "$INTERACTIVE" = "1" ] && read -r -p "    ${__p} [${__d}]: " __r </dev/tty || true
    printf -v "$__v" '%s' "${__r:-$__d}"
}

# ------------------------------------------------------------------ settings
say "Configuration"

SERVER_NAME="${MUMBLE_SERVER_NAME:-}"
[ -z "$SERVER_NAME" ] && ask SERVER_NAME "Server name" "Mumble"

SUPERUSER_PASS="${MUMBLE_SUPERUSER_PASS:-}"
if [ -z "$SUPERUSER_PASS" ]; then SUPERUSER_PASS="$(gen_pass)"; GENERATED_SU=1; fi

JOIN_PASS="${MUMBLE_JOIN_PASS:-}"
if   [ -z "$JOIN_PASS" ]; then JOIN_PASS="$(gen_pass)"; GENERATED_JOIN=1
elif [ "$JOIN_PASS" = "-" ]; then JOIN_PASS=""; fi

WELCOME="${MUMBLE_WELCOME:-Welcome to ${SERVER_NAME}.}"
MAX_USERS="${MUMBLE_MAX_USERS:-20}"
PORT="${MUMBLE_PORT:-64738}"

# Mumble validates usernames against a regex and FULL-MATCHES it (verified on
# 1.5.517: bare "AE9S" rejects "xxAE9S"), so anchors are optional. SuperUser is
# always exempt — the server special-cases it before the regex runs.
USERNAME_MODE="${MUMBLE_USERNAME_MODE:-none}"
case "$USERNAME_MODE" in
    none)   USERNAME_ARG='!username' ;;
    us)     USERNAME_ARG='username=^([AKNW][A-Z]?[0-9][A-Z]{1,3}( [(][^)]*[)])?)$' ;;
    intl)   USERNAME_ARG='username=^(([A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9][A-Z]{1,4}( [(][^)]*[)])?)$' ;;
    custom) USERNAME_ARG="username=${MUMBLE_USERNAME_REGEX:?MODE=custom needs MUMBLE_USERNAME_REGEX}" ;;
    *)      die "MUMBLE_USERNAME_MODE must be: none | us | intl | custom" ;;
esac

ok "name=${SERVER_NAME}  port=${PORT}  users=${MAX_USERS}  usernames=${USERNAME_MODE}"

# ------------------------------------------------------------------ install
say "Installing mumble-server"
export DEBIAN_FRONTEND=noninteractive
# Non-fatal: one broken third-party repo shouldn't abort the whole setup, as
# long as the distro repos still have the package. The install below is the
# step that must actually succeed.
apt-get update -qq 2>/dev/null || warn "apt-get update reported errors; continuing"
apt-get install -y -qq mumble-server >/dev/null 2>&1 \
    || die "could not install mumble-server (try: apt-get update && apt-get install mumble-server)"
ok "version $(dpkg-query -W -f='${Version}' mumble-server)"

# ------------------------------------------------------------------ locate
# Never hardcode this path — it moved between versions:
#   1.3.x  /etc/mumble-server.ini
#   1.5.x  /etc/mumble/mumble-server.ini
# The systemd unit is the only authority worth trusting.
say "Locating config"
INI="$(systemctl cat mumble-server 2>/dev/null | grep -m1 -oP '(?<=-ini )\S+' || true)"
if [ -z "$INI" ]; then
    for c in /etc/mumble/mumble-server.ini /etc/mumble-server.ini; do
        [ -f "$c" ] && INI="$c" && break
    done
fi
[ -n "${INI:-}" ] && [ -f "$INI" ] || die "Cannot find mumble-server.ini"
ok "config: ${INI}"

BIN="$(command -v mumble-server || command -v murmurd)" || die "server binary not found"

if [ ! -f "${INI}.orig" ]; then
    cp -a "$INI" "${INI}.orig"; ok "pristine backup: ${INI}.orig"
fi
cp -a "$INI" "${INI}.bak"

# ------------------------------------------------------------------ configure
# Encoded here are four behaviours verified against mumble-server 1.5.517:
#
#  1. [Ice] must stay last. Keys below it are scoped to Ice and silently ignored.
#  2. A value containing a COMMA must be quoted. Unquoted, Qt parses it as a
#     list; read back as a string it becomes EMPTY. For serverpassword that
#     means the server silently accepts *any* password. No warning is logged.
#  3. A value containing a SEMICOLON must be quoted or it is truncated there
#     (";" is the real comment character, despite docs saying "#").
#  4. Keys must be edited in place, not appended — duplicates are ambiguous.
say "Applying settings"
python3 - "$INI" \
    "port=${PORT}" \
    "host=0.0.0.0" \
    "serverpassword=${JOIN_PASS}" \
    "registerName=${SERVER_NAME}" \
    "welcometext=${WELCOME}" \
    "users=${MAX_USERS}" \
    "autobanAttempts=20" \
    "autobanTimeframe=120" \
    "autobanTime=60" \
    "${USERNAME_ARG}" <<'PYEOF'
import re, sys

path, args = sys.argv[1], sys.argv[2:]
lines = open(path).readlines()
ice = next((i for i, l in enumerate(lines) if re.match(r"^\s*\[Ice\]", l)), len(lines))

def quote(v):
    if v == "":
        return v
    if len(v) > 1 and v[0] == '"' and v[-1] == '"':
        return v
    if any(c in v for c in ",;") or v != v.strip():
        return '"%s"' % v.replace('"', '\\"')
    return v

def find(key):
    pat = re.compile(r"^[;#\s]*" + re.escape(key) + r"\s*=")
    for i in range(ice):
        if pat.match(lines[i]):
            return i
    return None

for arg in args:
    if arg.startswith("!"):                 # comment the key out
        key = arg[1:]
        i = find(key)
        if i is not None and not lines[i].lstrip().startswith(";"):
            lines[i] = ";" + lines[i]
        continue
    key, value = arg.split("=", 1)
    line = "%s=%s\n" % (key, quote(value))
    i = find(key)
    if i is None:
        lines.insert(ice, line)
        ice += 1
    else:
        lines[i] = line

open(path, "w").writelines(lines)
PYEOF
ok "settings written"

say "Setting SuperUser password"
"$BIN" -ini "$INI" -supw "$SUPERUSER_PASS" >/dev/null 2>&1 \
    && ok "SuperUser password set" || die "could not set SuperUser password"

# ------------------------------------------------------------------ firewall
if [ "${MUMBLE_SKIP_FIREWALL:-0}" = "1" ]; then
    warn "firewall skipped (MUMBLE_SKIP_FIREWALL=1)"
elif ! command -v ufw >/dev/null; then
    warn "ufw absent — open ${PORT}/tcp and ${PORT}/udp yourself"
else
    say "Firewall"
    # Allow SSH BEFORE enabling. Getting this wrong is the only step here that
    # locks you out of the box for good. Detect the real port, don't assume 22.
    SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}' || true)"
    [ -z "$SSH_PORT" ] && SSH_PORT="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/{print $2; exit}' /etc/ssh/sshd_config 2>/dev/null || true)"
    [ -z "$SSH_PORT" ] && SSH_PORT=22
    ufw allow "${SSH_PORT}/tcp" >/dev/null && ok "ssh allowed on ${SSH_PORT}/tcp"
    ufw allow "${PORT}/tcp" >/dev/null
    ufw allow "${PORT}/udp" >/dev/null && ok "mumble allowed on ${PORT} tcp+udp"
    ufw --force enable >/dev/null && ok "ufw enabled"
fi

# ------------------------------------------------------------------ start
say "Starting service"
systemctl enable mumble-server >/dev/null 2>&1 || true
systemctl restart mumble-server
sleep 3

# ------------------------------------------------------------------ verify
say "Verifying"
FAIL=0
LOG="$(journalctl -u mumble-server --since '2 min ago' --no-pager 2>/dev/null || true)"

systemctl is-active --quiet mumble-server && ok "service active" \
    || { warn "service is not active"; FAIL=1; }

# A rejected value does NOT stop the server — it reverts to a default and
# carries on. This check is the whole reason it exists.
if grep -q 'is of invalid format' <<<"$LOG"; then
    warn "config value REJECTED and silently reverted to default:"
    grep 'is of invalid format' <<<"$LOG" | sed 's/^/         /'
    FAIL=1
else
    ok "all values parsed"
fi

if grep -q 'Server listening' <<<"$LOG"; then
    ok "listening on ${PORT}"
elif ss -lnt 2>/dev/null | grep -q ":${PORT}\b"; then
    ok "listening on ${PORT}"
else
    warn "no listen confirmation — check: journalctl -u mumble-server -n 40"
    FAIL=1
fi

IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
      || ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || true)"

# ------------------------------------------------------------------ summary
echo
[ "$FAIL" = "0" ] && printf '%s  Mumble is up.%s\n' "$G$B" "$N" \
                  || printf '%s  Started, but see warnings above.%s\n' "$Y$B" "$N"
cat <<EOF

  Address ......... ${IP:-<your server IP>}
  Port ............ ${PORT}
  Join password ... ${JOIN_PASS:-(none — server is OPEN)}
  SuperUser pass .. ${SUPERUSER_PASS}
EOF
[ -z "$JOIN_PASS" ] && warn "no join password: anyone who knows the address can enter"
[ "${GENERATED_SU:-0}" = "1" ]   && echo "  ! SuperUser password generated — save it now, it is stored only as a hash."
[ "${GENERATED_JOIN:-0}" = "1" ] && echo "  ! Join password generated — also readable in ${INI}."
if [ "$USERNAME_MODE" != "none" ]; then
cat <<EOF

  Username policy (${USERNAME_MODE}) is active.
  Check names before inviting anyone:   ./check-username.sh 'AE9S (phone)'
EOF
fi
cat <<EOF

  Next steps
    1. Connect with the Mumble client; accept the certificate warning.
    2. Right-click your name -> Register.
    3. Reconnect as SuperUser, right-click Root -> Edit -> Groups -> admin,
       add your name, OK. Reconnect as yourself.

  Re-run this script any time. Pristine config kept at ${INI}.orig
EOF
