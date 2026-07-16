# 0047 — Restarting the server from the settings screen

Status: Accepted

## Context

Settings are restart-to-apply (v1, ADR 0026): every save ends with "restart the server to
apply", and the operator then has to SSH in and run `restart-radio-server.sh`. The obvious
missing piece is a Restart button next to that banner. The deployment runs radio-server as a
**systemd user service** (`restart-radio-server.sh` = `systemctl --user restart radio-server`),
but the unit's `Restart=` policy lives on the box, not in the repo — the sample unit in
`docs/deployment.md` uses `Restart=on-failure`, under which a clean self-`exit(0)` would simply
stop the service. Self-restart via `os.execv` is worse: it skips the lifespan teardown that
reaps multimon-ng and releases the single-open AIOC sound card.

## Decision

`POST /server/restart` (token-gated) runs a **configured command** and lets the supervisor do
the restart: `server.restart_command`, default
`systemctl --user --no-block restart radio-server` (a marked per-deployment default, guardrail
1; empty disables). The command is `shlex.split` and spawned with no shell,
`start_new_session=True`, **delayed ~0.3 s** so the HTTP response reaches the browser before the
stop signal lands; `--no-block` queues the job inside the systemd manager, so the child's own
death in the service cgroup cannot cancel it. systemd then stops the process normally — the
existing lifespan teardown runs — and starts it fresh.

`GET /settings` reports `restart_available` so the UI hides the button where the command is
unconfigured (bare bench runs), the hide-when-unconfigured house pattern. The button renders in
the settings intro and inside the post-save banner ("Restart now"), with a two-step inline
confirm that disarms after 5 s — a restart drops live audio and the OTA session, so one stray
click must not do it.

## Consequences and trade-offs accepted

- The server can execute an operator-configured command. Accepted: the config file is already
  the trust boundary (it selects serial ports and binaries like `dtmf.multimon_bin`), and the
  endpoint sits behind the same LAN token that keys the transmitter.
- A restart is not confirmed end-to-end — the response only means "the command was queued". The
  browser observes the outcome as a WS drop + reconnect; if the unit fails to come back, that is
  the supervisor's (and journal's) story, which the server can no longer tell.
- On a box without the unit (or with a different supervisor), the operator must set
  `server.restart_command` appropriately or leave it empty and restart by hand.
