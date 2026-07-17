#!/usr/bin/env bash
#
# check-username.sh — test names against the username policy in the LIVE config,
# before you lock yourself or anyone else out.
#
#   ./check-username.sh 'AE9S' 'AE9S (phone)' 'notacall'
#   ./check-username.sh          # runs a built-in sample set
#
set -euo pipefail

INI="$(systemctl cat mumble-server 2>/dev/null | grep -m1 -oP '(?<=-ini )\S+' || true)"
if [ -z "$INI" ]; then
    for c in /etc/mumble/mumble-server.ini /etc/mumble-server.ini; do
        [ -f "$c" ] && INI="$c" && break
    done
fi
[ -n "${INI:-}" ] && [ -f "$INI" ] || { echo "cannot find mumble-server.ini" >&2; exit 1; }

if [ "$#" -gt 0 ]; then NAMES=("$@")
else NAMES=("AE9S" "AE9S (phone)" "KD0YCR" "AG1I (radio server)" "W1AW" \
            "SuperUser" "KB" "notacall" "xxAE9S" "AE9S/M"); fi

python3 - "$INI" "${NAMES[@]}" <<'PYEOF'
import re, sys

ini, names = sys.argv[1], sys.argv[2:]
lines = open(ini).readlines()
ice = next((i for i, l in enumerate(lines) if re.match(r"^\s*\[Ice\]", l)), len(lines))

pattern, active = None, False
for l in lines[:ice]:
    m = re.match(r"^([;#\s]*)username\s*=\s*(.*?)\s*$", l)
    if m:
        active = not m.group(1).strip().startswith((";", "#"))
        pattern = m.group(2)
        break

print("config: %s" % ini)

if pattern is None or not active or pattern == "":
    print("policy: NONE — Mumble's permissive default applies.\n")
    sys.exit(0)

raw = pattern
if len(raw) > 1 and raw[0] == '"' and raw[-1] == '"':
    raw = raw[1:-1]
# The ini requires backslashes be doubled; undo that for real regex use.
raw = raw.replace("\\\\", "\\")

print("policy: %s\n" % raw)

try:
    # Mumble full-matches the pattern (verified on 1.5.517), so fullmatch here.
    rx = re.compile(raw)
except re.error as e:
    print("!! pattern does not compile: %s" % e)
    print("!! mumble-server would log 'is of invalid format' and fall back")
    print("!! to its permissive default — meaning NO policy at all.")
    sys.exit(2)

for n in names:
    verdict = "ALLOW" if rx.fullmatch(n) else "DENY "
    note = "  (SuperUser is always exempt)" if n == "SuperUser" else ""
    print("  [%s] %r%s" % (verdict, n, note))
print()
PYEOF
