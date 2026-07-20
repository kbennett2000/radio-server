# 0100 — The DVAP autoheal must restart-until-the-dongle-opens, in user space (fix the deaf DVAP)

Status: Accepted

## Context

The two DV Access Point Dongles that back the D-STAR gateway's RF modules (`dstarrepeater` = module B on
441.6 / A602RQT5, `dstarrepeater2` = module C on 441.0 / A602RQXT) go **silently deaf** — the DVAP keeps
running but stops passing RF, with the reflector dashboard never updating. It is not always caught while
the operator is home to cycle it. A previous `dvap-autoheal.sh` (a `--user` systemd service) already
existed but did **not** reliably heal it. A bench session on the live box (2026-07-20, everything on dummy
loads) characterised why, empirically:

**Two distinct deaf modes.**
- **Mode 1 — USB re-enum.** The A602RQT5 re-enumerates on USB roughly hourly (`ttyUSB4`↔`ttyUSB2` observed
  bouncing). The dongle drops and returns, often on a *different* `/dev/ttyUSB`, leaving `dstarrepeater`
  holding a **stale fd** — deaf, with **no error logged**.
- **Mode 2 — open wedge.** The DV Access Point Dongle's **first open after any abrupt close is flaky**: it
  answers the firmware-version query but then times out on the serial-number query — `The DVAP is not
  responding with its serial number` → `Cannot open the D-Star modem` → `dstarrepeater` falls back to a
  **dummy controller**, deaf. (Same hardware quirk as the AMBE2000 DV Dongle wedge in ADR 0094/0099.)

**The wedge ALTERNATES.** Restarting `dstarrepeater` repeatedly, reading the log's open outcome each time,
gave: `WEDGED, OK, WEDGED, OK, WEDGED` — every successive open toggles. So a **single** `systemctl restart`
heals only ~50% of the time and, on a *healthy* dongle, can even **create** a wedge. The old autoheal did
exactly one restart with no verification — which is why it left the DVAP deaf so often (and is how a manual
"kick" during this session wedged a dongle that had been decoding fine minutes earlier).

**No USB reset is available to us.** A direct USB reset (the FTDI's `usbreset`/`USBDEVFS_RESET` ioctl, or a
driver unbind/bind) would sidestep the alternation, but this host has **no passwordless sudo** and the USB
device node is `crw-rw-r-- root root` (the `kb` user cannot open it read-write). So recovery must be **pure
user space**. A full **reboot** does recover both dongles cleanly, but that is not an acceptable auto-remedy.

## Decision

Rewrite `dvap-autoheal.sh` to **restart-until-the-log-confirms-the-dongle-opened**, detecting both deaf
modes, entirely in user space (no root). Version it in the repo under `scripts/` with its `--user` unit.

- **Detection.**
  - *Mode 2 (open wedge)* — read the instance's own `dstarrepeaterd*.log`; if the latest open outcome is
    `Cannot open the D-Star modem` (not a `DVAP Serial number`), the dongle is not open → heal. This is the
    new, definitive signal that also catches the wedge a naive restart itself causes.
  - *Mode 1 (re-enum stale fd)* — keep the prior check: the `/dev/ttyUSB` the process holds open (via
    `/proc/PID/fd`) no longer matches the `/dev/serial/by-id` target. Re-confirmed after a 1 s settle to
    avoid a udev-update race.
- **Recovery — `heal()`.** `systemctl --user restart` the instance, wait `OPEN_WAIT` (6 s), and **verify**
  the log now shows a successful open; retry up to `RESTART_TRIES` (4). Because the wedge alternates, this
  converges in 1–2 restarts. If it still will not open (a truly dead dongle needing a physical replug), log
  it and **back off** `FAIL_COOLDOWN` (60 s) so it never becomes a restart storm.
- **Tunables** (`RESTART_TRIES`, `OPEN_WAIT`, `FAIL_COOLDOWN`, per-instance device/log globs) are marked
  constants at the top (guardrail 1) — the log filename differs per instance (`dstarrepeaterd-*.log` vs the
  `#2` instance's `dstarrepeaterd_2-*.log`).
- **Optional root upgrade (documented, not enabled).** If a `sudoers` NOPASSWD drop-in for a specific
  `usbreset`/unbind helper is added, `heal()` can do a real USB reset instead of restart-until-open,
  removing the alternation dependency. Left as a note because it needs a one-time root install.

## Consequences

- The DVAP **self-heals both deaf modes without a human and without root** — proven on the bench: a wedged
  A602RQT5 recovered to `DVAP Serial number` in ~5 s (`healing … (open wedge): restart 1/4` → `healed …
  after 1 restart(s)`), and a healthy dongle saw **0** restarts over the following window (no flapping).
- The fatal flaw of the old version — a single unverified restart that heals ~50% and can wedge a healthy
  dongle — is gone: every heal is verified against the dongle actually opening.
- A genuinely dead dongle (needs replug) is logged and backed off, not hammered.
- Scope: host tooling for the D-STAR gateway (dstarrepeater/DVAP), independent of the radio-server app.
  Installed per-operator as a `--user` service (`scripts/dvap-autoheal.service`); needs `loginctl
  enable-linger` to run while logged out. The residual case a *silent same-`ttyUSB` re-enum with no logged
  error* would need an event-driven udev trigger (root) — noted as future work; the log-based Mode 2 check
  already catches every wedge that reaches "cannot open".
