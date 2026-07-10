"""The station ledger (ADR 0018): a passive subscriber of the event flow that writes durable,
timestamped records.

:class:`EventLog` is *not* an instrumenter — it adds no new emissions to the tree. It consumes the
:class:`~radio_server.api.events.Event` stream that already flows through the ``EventHub`` and
translates each into a flat, clock-stamped ledger record for a :class:`~radio_server.eventlog.sink.LogSink`.

Two invariants make it safe to hang off the live event path:

- **No secrets, ever.** :meth:`EventLog._record_for` *whitelists* the fields each record type
  emits; it never spreads ``event.data`` wholesale. Even if an upstream event carried a TOTP code,
  the API token, or the shared secret, none of it can reach the ledger — the record simply doesn't
  copy unrecognized keys. A rejected-auth record records *that* auth failed and *when*, never the
  digits.
- **Failure isolation.** :meth:`handle` catches everything and drops the record. A slow disk, a
  full filesystem, or a bug in a record builder can never propagate back into the event pump or a
  transmission. The ledger is a place data goes to rest, never a place a fault comes from.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from .sink import LogSink

if TYPE_CHECKING:
    # Annotation-only (this file has `from __future__ import annotations`). Importing `Event` at
    # runtime creates an eventlog↔api cycle (api.app imports eventlog), which surfaces the moment
    # any module — e.g. `config.spec` — imports eventlog before api. Guarding it keeps eventlog a
    # leaf of the api package it feeds off.
    from ..api.events import Event

#: The clock seam every time-sensitive object in the tree shares: a zero-arg callable returning a
#: unix timestamp. Injected as ``FakeClock`` in tests; defaults to ``time.time`` in production.
Clock = Callable[[], float]


class EventLog:
    """Translate ``Event``s into durable ledger records and write them to a :class:`LogSink`.

    Stateful only where the taxonomy requires it: a TX key-up remembers its timestamp so the
    paired key-down can record the keyed duration (the Part 97 operating-log value). Everything
    else is a pure function of the event.
    """

    def __init__(self, sink: LogSink, *, clock: Clock | None = None) -> None:
        self._sink = sink
        self._clock = clock or time.time
        #: Timestamp of the last unpaired TX key-up, or None. Drives key-down duration.
        self._keyup_at: float | None = None

    def handle(self, event: Event) -> None:
        """Record ``event`` if it maps to a ledger entry — never raising into the caller.

        A logging failure (bad record, unwritable disk) is caught and the record dropped, so this
        is safe to call from the event pump: the ledger never breaks the flow or a transmission.
        """
        try:
            record = self._record_for(event)
            if record is not None:
                self._sink.write(record)
        except Exception:
            # Failure isolation (ADR 0018): the ledger is a passive consumer; a write fault must
            # not propagate into the event/audio path. Drop the record and carry on.
            pass

    def close(self) -> None:
        """Flush and release the sink (called on app shutdown)."""
        self._sink.close()

    def _record_for(self, event: Event) -> dict[str, Any] | None:
        """Build the ledger record for ``event``, or None if it is not a logged event.

        Dispatches on ``event.type`` and **whitelists** the fields each record carries — it never
        copies ``event.data`` wholesale, which is what keeps secrets out of the log.
        """
        now = self._clock()
        data = event.data or {}

        if event.type == "ptt":
            if data.get("on"):
                self._keyup_at = now
                return {"ts": now, "type": "tx_key_up"}
            # key-down: duration since the paired key-up (None if we never saw the key-up).
            duration = None if self._keyup_at is None else now - self._keyup_at
            self._keyup_at = None
            return {"ts": now, "type": "tx_key_down", "duration": duration}

        if event.type == "scan":
            # `active` carries the frequency of a hit — the operationally meaningful record; other
            # phases (scanning/dwelling/resumed) record the state transition. freq/channel are
            # included only when present.
            record: dict[str, Any] = {"ts": now, "type": "scan", "phase": data.get("phase")}
            if data.get("frequency") is not None:
                record["frequency"] = data["frequency"]
            if data.get("channel") is not None:
                record["channel"] = data["channel"]
            return record

        if event.type == "session":
            phase = data.get("phase")
            if phase == "session_open":
                return {"ts": now, "type": "session_open"}
            if phase == "session_close":
                record = {"ts": now, "type": "session_close"}
                # reason (logout vs timeout) and signed_off flag if the controller supplied them.
                if data.get("reason") is not None:
                    record["reason"] = data["reason"]
                if data.get("signed_off") is not None:
                    record["signed_off"] = data["signed_off"]
                return record
            if phase == "id":
                record = {"ts": now, "type": "station_id"}
                if data.get("callsign") is not None:
                    record["callsign"] = data["callsign"]
                if data.get("mode") is not None:
                    record["mode"] = data["mode"]
                return record
            return None

        # --- forward-compatible types: the mapper is ready, but nothing publishes these to the hub
        # yet (ADR 0018, deferred). A future instrumentation cycle that adds the `hub.publish` gets
        # these records for free. NOTE the whitelist: an `auth` event never contributes a code.
        if event.type == "auth":
            result = data.get("result")
            if result == "accepted":
                return {"ts": now, "type": "auth_accepted"}
            if result == "rejected":
                # Deliberately minimal: no code, no secret, no digits — only that it failed and when.
                return {"ts": now, "type": "auth_rejected"}
            return None

        if event.type == "command":
            record = {"ts": now, "type": "command_dispatched"}
            if data.get("service") is not None:
                record["service"] = data["service"]
            return record

        if event.type == "arbiter":
            record = {"ts": now, "type": "arbiter_mode"}
            if data.get("mode") is not None:
                record["mode"] = data["mode"]
            return record

        # `status` snapshots and any unknown type are not ledger events.
        return None
