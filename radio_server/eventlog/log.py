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
- **Failure isolation.** :meth:`handle` catches everything and drops the record. A full filesystem
  or a bug in a record builder can never propagate back into the event pump or a transmission. The
  ledger is a place data goes to rest, never a place a fault comes from.

  This covers a sink that **raises**. It says nothing about one that **blocks**, and until ADR 0181
  this docstring claimed a slow disk was covered too — it was not. :meth:`handle` is called from
  ``app.py``'s ``_drain_log`` task on the event loop, so a blocking write stalls the loop that keys
  and unkeys the transmitter; a hung filesystem meant a keyed carrier the server could not drop.
  Not blocking is the **sink's** contract (see :class:`~radio_server.eventlog.sink.LogSink`), and
  the shipped composition satisfies it with
  :class:`~radio_server.eventlog.sink.ThreadedSink`.
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

        A logging failure (bad record, unwritable disk) is caught and the record dropped, so a
        *raising* sink never breaks the flow or a transmission.

        **What this does not give you** (ADR 0181): it does not make a *blocking* sink safe. This
        runs on the event loop, so ``self._sink.write`` blocks it for as long as the write takes —
        and the loop is where ``/audio/tx`` keys, and where the ``finally`` that unkeys runs. Wrap
        any sink that touches a disk, socket or database in
        :class:`~radio_server.eventlog.sink.ThreadedSink`, which is what ``build_app`` does.
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

    def sink_stats(self) -> dict[str, int] | None:
        """The sink's own counters, or ``None`` for a sink that does not keep any (ADR 0181).

        Optional by design: :class:`~radio_server.eventlog.sink.LogSink` is a one-method protocol
        and widening it would force every future sink (and every test double) to invent counters.
        ``None`` reaches ``GET /status`` as ``ledger: null`` and means "not reported", never "zero".
        """
        stats = getattr(self._sink, "stats", None)
        return stats() if callable(stats) else None

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
            if phase == "tx_failed":
                # The negative of `station_id` (ADR 0151): a station-keying call that RAISED —
                # the radio refused its own PTT path (AM, ADR 0150), the audio device died. It
                # belongs in the ledger and not only in a live event stream, because this is the
                # durable Part 97 artifact: an operator auditing whether the station identified
                # sees the gap and its reason, instead of inferring it from records that are
                # simply absent. `what` says which call; `reason` is the backend's own sentence.
                record = {"ts": now, "type": "tx_failed"}
                if data.get("what") is not None:
                    record["what"] = data["what"]
                if data.get("reason") is not None:
                    record["reason"] = data["reason"]
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
