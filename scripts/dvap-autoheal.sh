#!/usr/bin/env bash
# dvap-autoheal — keep the DVAP dstarrepeater instances healthy without a human (ADR 0100).
#
# The DV Access Point Dongle (an FTDI-behind-USB radio) goes silently deaf two ways; this
# watchdog detects both and heals them purely in user space (this host has no passwordless
# sudo, and the USB device node is root-only, so a real USB reset is not available to us).
#
#   Mode 1 — USB RE-ENUM: the A602RQT5 re-enumerates roughly hourly. The dongle drops and
#     returns, often on a different /dev/ttyUSB, leaving dstarrepeater holding a STALE fd —
#     deaf, with NO error logged. Detected by: the ttyUSB the process has open no longer
#     matches the /dev/serial/by-id symlink target.
#
#   Mode 2 — OPEN WEDGE: the DV Access Point Dongle's first open after any abrupt close is
#     flaky and often fails ("The DVAP is not responding with its serial number" / "Cannot
#     open the D-Star modem"), so dstarrepeater falls back to a dummy controller — deaf.
#     Bench-confirmed the wedge ALTERNATES good/bad on each successive open. Detected by: the
#     latest open outcome in the dstarrepeater log is the failure, not a serial number.
#
# RECOVERY — restart-until-the-log-confirms-the-dongle-opened. Because the wedge alternates,
# a SINGLE restart heals only ~50% of the time and can even wedge a healthy dongle — the fatal
# flaw of the previous single-shot version. We restart and VERIFY, retrying a few times, then
# back off if the dongle is truly dead (needs a physical replug / USB reset).
#
# A direct USB reset (usbreset / driver unbind-bind) would avoid the alternation entirely but
# needs root; if you add the sudoers drop-in (see docs/adr/0100), swap RECOVER for that.
set -u
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# service | by-id device | log directory | log filename glob (the instance's own log)
B_SVC=dstarrepeater
B_DEV=/dev/serial/by-id/usb-Internet_Labs_DV_Access_Point_Dongle_A602RQT5-if00-port0
B_LOGDIR=/home/kb/applications/dstar-gateway/log
B_LOGGLOB='dstarrepeaterd-*.log'

C_SVC=dstarrepeater2
C_DEV=/dev/serial/by-id/usb-Internet_Labs_DV_Access_Point_Dongle_A602RQXT-if00-port0
C_LOGDIR=/home/kb/applications/dstar-gateway2/log
C_LOGGLOB='dstarrepeaterd_2-*.log'   # the #2 instance logs with a _2 suffix

RESTART_TRIES=4     # restart+verify attempts before giving up a heal pass
OPEN_WAIT=6         # seconds to let a (re)start open the dongle before reading the log
FAIL_COOLDOWN=60    # seconds to back off after a heal that could not open the dongle

latest_log() {   # newest log matching $2 in dir $1
  ls -t "$1"/$2 2>/dev/null | head -1
}

open_tty() {   # the /dev/ttyUSB the service's main PID currently holds open
  local pid; pid=$(systemctl --user show -p MainPID --value "$1" 2>/dev/null)
  [ -n "$pid" ] && [ "$pid" != 0 ] || return 0
  ls -l "/proc/$pid/fd" 2>/dev/null | grep -oE '/dev/ttyUSB[0-9]+' | sort -u | head -1
}

open_state() {   # $1=logdir $2=logglob -> "ok" | "wedged" | "unknown" (the dongle's last open)
  local log line
  log=$(latest_log "$1" "$2"); [ -n "$log" ] || { echo unknown; return; }
  line=$(grep -aE "DVAP Serial number|Cannot open the D-Star modem" "$log" | tail -1)
  case "$line" in
    *"Serial number"*) echo ok ;;
    *"Cannot open"*)   echo wedged ;;
    *)                 echo unknown ;;
  esac
}

heal() {   # $1=svc $2=logdir $3=logglob $4=reason -> 0 if the dongle opened, 1 if it stayed dead
  local n st
  for n in $(seq 1 "$RESTART_TRIES"); do
    logger -t dvap-autoheal "healing $1 ($4): restart $n/$RESTART_TRIES"
    systemctl --user restart "$1"
    sleep "$OPEN_WAIT"
    st=$(open_state "$2" "$3")
    if [ "$st" = ok ]; then
      logger -t dvap-autoheal "healed $1: dongle open OK after $n restart(s)"
      return 0
    fi
  done
  logger -t dvap-autoheal "FAILED to heal $1 after $RESTART_TRIES restarts (still $st) — needs a USB replug/reset"
  return 1
}

check() {   # $1=svc $2=by-id-dev $3=logdir $4=logglob -> 0 healthy/healed, 1 heal failed
  local cur open st
  # Mode 2 — the dongle isn't open at all (dummy controller). Cheapest, most definitive signal.
  st=$(open_state "$3" "$4")
  if [ "$st" = wedged ]; then heal "$1" "$3" "$4" "open wedge"; return $?; fi
  # Mode 1 — a re-enum left a stale fd: the open ttyUSB no longer matches the by-id target.
  cur=$(readlink -f "$2" 2>/dev/null) || return 0
  [ -n "$cur" ] || return 0
  open=$(open_tty "$1")
  [ -n "$open" ] || return 0            # not opened yet / mid-restart
  [ "$open" = "$cur" ] && return 0      # healthy
  sleep 1                               # re-confirm (avoid a udev-update race)
  cur=$(readlink -f "$2" 2>/dev/null); open=$(open_tty "$1")
  [ -n "$open" ] && [ -n "$cur" ] && [ "$open" != "$cur" ] || return 0
  heal "$1" "$3" "$4" "re-enum: had $open, dongle now $cur"; return $?
}

while true; do
  check "$B_SVC" "$B_DEV" "$B_LOGDIR" "$B_LOGGLOB" || sleep "$FAIL_COOLDOWN"
  check "$C_SVC" "$C_DEV" "$C_LOGDIR" "$C_LOGGLOB" || sleep "$FAIL_COOLDOWN"
  sleep 3
done
