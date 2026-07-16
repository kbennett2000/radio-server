#!/usr/bin/env bash
# One-command update for the deployed box: pull, sync, rebuild the web bundle, restart.
#
# The load-bearing line is the sync: `uv sync` is EXACT — run bare, it REMOVES every package not
# named on that invocation, including previously-installed extras. That is how the Mumble link
# kept losing pymumble on updates. Every extra this deployment uses must be named here, every
# time. (`uv run`, the service launcher, is safe — its implicit sync is inexact.)
set -euo pipefail
cd "$(dirname "$0")"
git pull
uv sync --extra hardware --extra tts --extra mumble
(cd web && npm install && npm run build)
./restart-radio-server.sh
