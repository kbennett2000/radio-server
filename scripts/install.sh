#!/usr/bin/env bash
# radio-server one-command installer for macOS and Linux (ADR 0053).
#
# Gets you from nothing to the control panel — pointed at the demo server, so keying 10# on a radio
# links to a worldwide voice channel out of the box. Installs uv (which brings its own Python),
# fetches the pieces, builds the web page, and writes a starter config.
#
# Run it either way:
#   curl -LsSf https://raw.githubusercontent.com/kbennett2000/radio-server/master/scripts/install.sh | sh
#   ./scripts/install.sh                      # from inside a checkout
#
# Options:
#   --with-hardware   also install the real-radio extras (a real Baofeng/AIOC, TTS voice, Mumble)
#   --force-web       rebuild the web page even if it is already built
#   --run             start the server when the install finishes
#   -h, --help        show this and exit
#
# It is safe to run again any time. It never overwrites an existing radio.toml or radio-secrets.toml,
# so a re-run can't clobber your callsign, password, or login secret — if a step fails, fix what it
# printed and run the same line again; it picks up where it left off.
set -euo pipefail

REPO_URL="https://github.com/kbennett2000/radio-server.git"
REPO_TARBALL="https://github.com/kbennett2000/radio-server/archive/refs/heads/master.tar.gz"
PORT=8000

WITH_HARDWARE=0
FORCE_WEB=0
RUN_AFTER=0
for arg in "$@"; do
  case "$arg" in
    --with-hardware) WITH_HARDWARE=1 ;;
    --force-web) FORCE_WEB=1 ;;
    --run) RUN_AFTER=1 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- little helpers -------------------------------------------------------------------------------
# Colour only when attached to a terminal (piped-to-sh output stays clean).
if [ -t 1 ]; then B=$(printf '\033[1m'); G=$(printf '\033[32m'); Y=$(printf '\033[33m'); R=$(printf '\033[0m'); else B=; G=; Y=; R=; fi
step() { printf '%s\n' "${B}==>${R} $*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '%s\n' "${G}    ok${R} $*"; }
warn() { printf '%s\n' "${Y}    !${R} $*"; }
die()  { printf '%s\n' "${Y}install stopped:${R} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Prompt reading from the real terminal, so it still works under `curl | sh` (where stdin is the
# pipe, not a keyboard). No usable terminal (a cron/CI run, or a /dev/tty that won't open) → skip the
# prompt and return the default, silently.
ask() { # ask "Question" "default"; echoes the answer
  local q="$1" default="${2:-}" reply=""
  if { : < /dev/tty; } 2>/dev/null; then
    printf '%s ' "$q" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
  fi
  printf '%s' "${reply:-$default}"
}

# --- 1. find or fetch the repo --------------------------------------------------------------------
step "Finding the radio-server files"
find_root() {
  # Inside a checkout (script run directly)? Walk up from the script's own dir.
  local d
  d="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || d=""
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    if [ -f "$d/pyproject.toml" ] && grep -q 'name = "radio-server"' "$d/pyproject.toml" 2>/dev/null; then
      printf '%s' "$d"; return 0
    fi
    d="$(dirname "$d")"
  done
  # Or the current directory (piped `cd repo && curl | sh`).
  if [ -f "pyproject.toml" ] && grep -q 'name = "radio-server"' pyproject.toml 2>/dev/null; then
    pwd; return 0
  fi
  return 1
}

if ROOT="$(find_root)"; then
  ok "using $ROOT"
else
  info "no checkout here — downloading a fresh copy into ./radio-server"
  if [ -e radio-server ]; then
    die "a ./radio-server already exists but isn't a valid checkout; move it aside and re-run"
  fi
  if have git; then
    git clone --depth 1 "$REPO_URL" radio-server
  elif have curl && have tar; then
    curl -LsSf "$REPO_TARBALL" | tar -xz
    mv radio-server-master radio-server
  else
    die "need either git, or curl+tar, to download the files. Install git and re-run."
  fi
  ROOT="$(cd radio-server && pwd)"
  ok "downloaded to $ROOT"
fi
cd "$ROOT"

# --- 2. uv (brings its own Python) ----------------------------------------------------------------
step "Checking for uv (the helper that gathers everything else)"
if ! have uv; then
  info "installing uv from astral.sh …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv writes a shell env file; source it so this session sees uv without a new terminal.
  for envf in "$HOME/.local/bin/env" "$HOME/.cargo/env"; do
    [ -f "$envf" ] && . "$envf"
  done
  have uv || export PATH="$HOME/.local/bin:$PATH"
fi
have uv || die "uv still isn't on PATH. Open a new terminal and run this script again."
ok "$(uv --version 2>/dev/null || echo uv ready)"

# --- 3. Node (for the web page) -------------------------------------------------------------------
step "Checking for Node.js (used only to build the control panel)"
if ! have npm; then
  warn "Node.js isn't installed."
  info "Install the LTS version from https://nodejs.org/ then run this script again —"
  info "it picks up right here where it left off."
  exit 2
fi
ok "$(node --version 2>/dev/null || echo node ready)"

# --- 4. Python deps -------------------------------------------------------------------------------
step "Gathering radio-server's pieces (uv sync)"
if [ "$WITH_HARDWARE" -eq 1 ]; then
  info "including the real-radio extras (hardware, tts, mumble) — some need system libraries too;"
  info "see docs/install.md if a build fails."
  uv sync --extra hardware --extra tts --extra mumble
else
  uv sync
  info "practice-mode install (no radio needed). Re-run with --with-hardware when you wire a radio."
fi
ok "dependencies ready"

# --- 5. build the web page ------------------------------------------------------------------------
step "Building the control panel"
if [ -f web/dist/index.html ] && [ "$FORCE_WEB" -eq 0 ]; then
  ok "already built (use --force-web to rebuild)"
else
  ( cd web && npm install && npm run build )
  ok "control panel built"
fi

# --- 6. first-run config (never overwrites what's already there) ----------------------------------
step "Setting up your configuration"
if [ -f radio.toml ]; then
  ok "radio.toml already exists — leaving it untouched"
else
  cp radio.toml.example radio.toml
  info "wrote radio.toml (starts on the practice radio, already pointed at the demo server)"
  callsign="$(ask "  Your FCC callsign (needed before transmitting; press Enter to skip for now):" "")"
  if [ -n "$callsign" ]; then
    # Uncomment/replace the callsign line in the freshly-copied example.
    up="$(printf '%s' "$callsign" | tr '[:lower:]' '[:upper:]')"
    if grep -q '^# callsign = ' radio.toml; then
      # Portable in-place edit (BSD/GNU sed differ on -i); rewrite via a temp file.
      sed "s|^# callsign = .*|callsign = \"$up\"|" radio.toml > radio.toml.tmp && mv radio.toml.tmp radio.toml
    elif ! grep -q '^callsign = ' radio.toml; then
      printf '\n[station]\ncallsign = "%s"\n' "$up" >> radio.toml
    fi
    ok "callsign set to $up"
  else
    info "no callsign yet — set it in radio.toml before you transmit (looking around is fine without)."
  fi
fi

step "Control-panel password"
if [ -f radio-secrets.toml ] && grep -q '^api_token' radio-secrets.toml 2>/dev/null; then
  ok "already set (in radio-secrets.toml) — leaving it untouched"
else
  TOKEN="$(uv run python -c "from radio_server.config.secrets import rotate; print(rotate('radio-secrets.toml','api_token'))")"
  info "This is the password you'll type in the browser (saved in radio-secrets.toml):"
  printf '\n      %s%s%s\n\n' "$B" "$TOKEN" "$R"
fi

step "Over-the-air login (optional — only needed to log in from a radio)"
ans="$(ask "  Set up a phone login code now? [y/N]:" "n")"
case "$ans" in
  y|Y|yes|YES)
    uv run python -m radio_server.enroll || warn "enrollment skipped (you can run it later: uv run python -m radio_server.enroll)"
    ;;
  *)
    info "skipped — run 'uv run python -m radio_server.enroll' any time to set it up."
    ;;
esac

# --- 7. done --------------------------------------------------------------------------------------
step "All set."
cat <<EOF

Start radio-server with:

    ${B}uv run python -m radio_server${R}

then open ${B}http://127.0.0.1:${PORT}${R} in your browser and enter the password above.

  • First time here?           docs/getting-started.md
  • Connecting a real radio?    docs/install.md   (then re-run with --with-hardware)
  • Running your own server?    docs/mumble-server/
EOF

if [ "$RUN_AFTER" -eq 1 ]; then
  step "Starting radio-server (Ctrl+C to stop) …"
  exec uv run python -m radio_server
fi
