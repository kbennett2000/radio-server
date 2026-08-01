#!/usr/bin/env bash
# One-command update for the deployed box: fetch, fast-forward, sync, rebuild the web bundle, restart.
#
# Three load-bearing details, each of which has broken an update at least once:
#
#  1. `uv sync` is EXACT — run bare, it REMOVES every package not named on that invocation, including
#     previously-installed extras. That is how the Mumble link kept losing pymumble on updates. Every
#     extra this deployment uses must be named here, every time. (`uv run`, the service launcher, is
#     safe — its implicit sync is inexact.)
#
#  2. **This box is normally on a DETACHED HEAD, and that is deliberate.** `docs/server-notes.md`
#     deploys with `git switch --detach <ref>` so a bench measurement is attributable to an exact
#     commit rather than to a branch name that moves underneath it. `git pull` cannot work there —
#     it prints "You are not currently on a branch" and exits 1 — so this script used to fail on
#     every run against the box it was written for, while the deploy documentation told the operator
#     to put it in exactly that state. The update is therefore expressed as **fast-forward to a
#     target ref**, which is meaningful detached and on a branch alike, instead of as "pull".
#
#  3. **`uv` is not on `PATH` in a non-interactive shell.** It lives in `~/.local/bin`, which is added
#     by the login profile, so a bare `uv` works when a human runs this and fails when anything
#     automated does. Resolved explicitly below rather than assumed.
#
# Usage:
#   ./update-radio-server.sh                    # fast-forward to origin/master
#   ./update-radio-server.sh origin/some-branch # deploy a specific ref, including a non-fast-forward
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-origin/master}"
EXPLICIT=$#

git fetch origin --prune

if ! git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null; then
  echo "update: '$TARGET' is not a commit this checkout knows about." >&2
  echo "        Give a ref that exists here — origin/master, or origin/<branch> after a fetch." >&2
  exit 1
fi

BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"   # empty when detached, which is normal here
BEFORE="$(git rev-parse --short HEAD)"
TARGET_SHA="$(git rev-parse --short "$TARGET")"

# A run with no argument may only FAST-FORWARD, and that refusal is the whole guard.
#
# This box gets deployed onto bench branches on purpose — ADR 0164 ran the station on
# `adr-0164-the-on-path` for a full cycle, and every ADR in that arc records "the station runs
# <commit>". Silently yanking such a checkout back to master mid-experiment would destroy the thing
# being measured, and it would do it during a routine "just update the server". Naming the target is
# how the operator says they mean it. Once the bench branch merges, its commits ARE in master, the
# ancestor test passes, and the plain no-argument update starts working again on its own — which is
# the "put the box back on the mainline once the PR merges" step, no longer a manual ritual.
if [ "$EXPLICIT" -eq 0 ] && ! git merge-base --is-ancestor HEAD "$TARGET"; then
  echo "update: refusing to move this checkout." >&2
  echo "        HEAD is $BEFORE${BRANCH:+ (branch $BRANCH)} and is NOT an ancestor of" >&2
  echo "        $TARGET ($TARGET_SHA), so this would not be a fast-forward — this box is" >&2
  echo "        carrying commits that $TARGET does not have. That is what a bench deployment" >&2
  echo "        looks like, so it is never moved by accident." >&2
  echo "" >&2
  echo "        To update onto it anyway, name it:" >&2
  echo "            ./update-radio-server.sh $TARGET" >&2
  exit 1
fi

# Preserve whichever mode the checkout is in. Detached is this deployment's normal state (see 2
# above); switching a branch checkout to detached behind the operator's back would be its own
# surprise, so a branch fast-forwards in place.
if [ -n "$BRANCH" ]; then
  git merge --ff-only "$TARGET"
else
  git switch --detach "$TARGET"
fi

AFTER="$(git rev-parse --short HEAD)"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "update: already at $AFTER — re-syncing dependencies and rebuilding anyway."
else
  echo "update: $BEFORE -> $AFTER ($TARGET)"
fi

# See 3 above. `UV=` overrides for a box that keeps it somewhere else.
UV="${UV:-$(command -v uv 2>/dev/null || true)}"
if [ -z "$UV" ]; then
  for candidate in "$HOME/.local/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
    [ -x "$candidate" ] && UV="$candidate" && break
  done
fi
if [ -z "$UV" ]; then
  echo "update: cannot find 'uv'. It is normally at ~/.local/bin/uv, which is only on PATH in a" >&2
  echo "        login shell — so this fails under cron, a wrapper script or 'ssh host ./update...'." >&2
  echo "        Set UV=/path/to/uv and re-run." >&2
  exit 1
fi

"$UV" sync --extra hardware --extra tts --extra mumble
(cd web && npm install && npm run build)
./restart-radio-server.sh

# This project asks "which commit is that box on?" in every ADR and every bench write-up. Print it.
echo "update: deployed $(git log --oneline -1)"
