"""Credentials never reach a log line (ADR 0165).

The seven WebSocket routes authenticate with ``?token=`` in the query string, because a browser
``WebSocket`` constructor cannot set an ``Authorization`` header. uvicorn's
``get_path_with_query_string`` appends that query string verbatim, and its WebSocket protocol logs
the result at INFO on every accept — so the LAN bearer token landed in journald once per socket
connect (**measured: 1072 lines in 72 hours** on the live station). ADR 0025 kept secrets off the
settings surface so nothing could "render, log, round-trip, or overwrite" them; that argument covered
every plane the secret is *stored* on and none of the plane it is *used* on.

**Where the transformation happens, and why there.** ``logging.setLogRecordFactory`` runs before any
handler, formatter or filter — including handlers this process does not own. That last part is not a
nicety: uvicorn's ``dictConfig`` gives the ``uvicorn`` logger ``propagate: False`` and gives
``uvicorn.error`` no handlers of its own, so those records reach uvicorn's own stderr handler and
*never reach root*. A filter installed on the root handler would be blind to 100% of the measured
lines. A rule applied at each call site would be blind to uvicorn entirely, since none of the call
sites are ours.

**Two layers, and the first one is the load-bearing one.**

- **By name** — ``token=``, ``api_token=``, ``Authorization: Bearer …`` and friends. Works before any
  secret is known, survives rotation, and redacts a *wrong* token too (uvicorn logs the 403 it writes
  for a rejected socket, and a mistyped credential is still somebody's credential). The name list is
  derived from `KNOWN_SECRETS` and `MUMBLE_PASSWORD_PREFIX`, so adding a secret to
  ``config/secrets.py`` extends the redaction with nobody remembering to come back here.
- **By value** — `register_secret_values` catches a live secret in a shape no ``name=`` pattern can
  span, e.g. the ``websockets`` library logging a header's name and value as two separate arguments.
  It is a backstop, not the defence: see `MIN_VALUE_LENGTH`.

**The one rule that is not obvious.** Rewriting ``record.msg`` while ``record.args`` is non-empty can
delete a ``%s`` and break arity. ``logging`` then calls ``Handler.handleError``, which prints
``Message:`` *and* ``Arguments: (<the raw secret>,)`` straight to stderr — a "redaction" that
guarantees a cleartext dump of the thing it was protecting. So ``msg`` is only rewritten when there
are no args, and otherwise only the ``str`` elements of ``args`` are touched, preserving arity and
element types (``uvicorn.logging.AccessFormatter`` unpacks a 5-tuple and calls ``int(status_code)``).

**What this deliberately does not cover**, because a partial guarantee stated as a total one is worse
than no guarantee:

- ``extra=`` fields. ``Logger.makeRecord`` applies them to ``record.__dict__`` *after* the factory
  returns, so they are unreachable from here. Nothing passes a credential via ``extra`` today.
- ``exc_text``. Computed by ``Formatter.format`` long after the factory. No current exception message
  carries a URL.
- ``workers=`` / ``reload=`` child processes, which never run ``__main__.main``. ``uvicorn.run`` is
  called with an app *instance*, which forecloses both — if that ever changes, this must move.
- ``print()``. ``enroll.py`` deliberately prints a fresh TOTP secret to a terminal; that is the
  feature, not a leak, and it is not a log record.

If any of those acquires a live vector, the successor is a post-format **Formatter** layer, which
sees all four at once at the cost of owning uvicorn's ``log_config``. Not built on a hypothetical.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config.secrets import KNOWN_SECRETS, MUMBLE_PASSWORD_PREFIX

__all__ = [
    "REDACTED",
    "MIN_VALUE_LENGTH",
    "VALUE_EXCLUDED",
    "install",
    "installed",
    "uninstall",
    "redact",
    "register_secret_values",
]

logger = logging.getLogger(__name__)

#: What replaces a credential. Deliberately visible: a reader who sees it knows a value was removed
#: on purpose, rather than wondering whether the line was truncated.
REDACTED = "<redacted>"

#: Shortest secret this will redact **by value**. A short secret is likely to be a substring of
#: ordinary log text — ``radio-serv`` inside ``/etc/radio-server/radio.toml`` — and blanking it
#: everywhere would corrupt the journal far more than it protects. Short secrets are still redacted
#: by NAME, which is what covers the query-string leak this module exists for.
MIN_VALUE_LENGTH = 12

#: Excluded from value redaction regardless of length. ``config/secrets.py`` already says a static
#: 6-digit code is low-security by nature; registering it would blank every matching frequency,
#: byte count and duration in the log.
VALUE_EXCLUDED = frozenset({"fixed_code"})

#: Extra parameter names worth covering that are not secret *names*: the query parameter is
#: ``token`` (not ``api_token``), and ``password`` is the generic spelling of the Mumble entries.
_EXTRA_NAMES = ("token", "password")

#: How deep to walk a container argument. uvicorn's TRACE-level message logger passes the whole ASGI
#: scope as one dict, with the raw query string inside it.
_MAX_DEPTH = 4

# Longest-first so `api_token=` wins over `token=` and the replacement names the full key.
_NAMES = sorted(
    {*KNOWN_SECRETS, *_EXTRA_NAMES, MUMBLE_PASSWORD_PREFIX.rstrip("_")}, key=len, reverse=True
)
#: ``name=value``, value ending at anything that could not be part of it. `mumble_password_home`
#: needs the trailing `\w*`, since the entry name is part of the key.
_NAME_PAT = re.compile(
    r"(?i)\b(" + "|".join(re.escape(n) for n in _NAMES) + r")(\w*)=([^&\s\"'#,;)\]}]+)"
)
#: ``Authorization: Bearer <x>`` and the bare ``Bearer <x>`` a header dump produces.
_BEARER_PAT = re.compile(r"(?i)\b(bearer\s+)([^\s\"'#,;)\]}]+)")

#: Rebound as a whole on registration — never mutated in place — so a reader in another thread sees
#: either the old pattern or the new one, never a half-built list.
_VALUE_PAT: re.Pattern[str] | None = None

#: A ``%``-style conversion specifier, named or positional, with the ``*`` width/precision forms that
#: each eat an extra positional argument.
_SPEC_PAT = re.compile(r"%(?:\((\w+)\))?[-#0 +]*(\*)?\d*(?:\.(\*)?\d*)?[hlL]?([diouxXeEfFgGcrsa%])")
#: The name-then-specifier shape: ``logger.info("api_token=%s", value)``. The credential is split
#: across ``msg`` and ``args``, so neither the string rule nor the value rule can see it.
_TRAILING_NAME_PAT = re.compile(
    r"(?i)(?:\b(?:" + "|".join(re.escape(n) for n in _NAMES) + r")\w*=|\bbearer\s+)$"
)


def redact(text: str) -> str:
    """Return `text` with every credential-shaped run replaced by `REDACTED`."""
    out = _NAME_PAT.sub(lambda m: f"{m.group(1)}{m.group(2)}={REDACTED}", text)
    out = _BEARER_PAT.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    pattern = _VALUE_PAT
    if pattern is not None:
        out = pattern.sub(REDACTED, out)
    return out


def _redact_arg(value: Any, depth: int = 0) -> Any:
    """Redact inside one logging argument, preserving its type and shape.

    Containers are rebuilt rather than mutated: a dict argument is the caller's live object (see
    ``LogRecord.__init__``, which unwraps a single Mapping), and rewriting it in place would corrupt
    caller state rather than just the log line.
    """
    if isinstance(value, str):
        return redact(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, bytes):
        try:
            redacted = redact(value.decode("latin-1"))
        except Exception:  # pragma: no cover - latin-1 decodes every byte string
            return value
        return redacted.encode("latin-1")
    if isinstance(value, dict):
        return {k: _redact_arg(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_arg(v, depth + 1) for v in value]
        return type(value)(rebuilt) if isinstance(value, list) else tuple(rebuilt)
    return value


def _credential_slots(msg: str) -> set[int | str]:
    """Which arguments of ``msg`` are named as credentials by the text right before them.

    ``logger.info("api_token=%s", value)`` puts the name in the format string and the value in the
    args, so the string rules see a specifier and a bare token respectively and neither fires. This
    walks the specifiers in order and reports the ones introduced by a credential name, identified
    by position for ``%s`` and by key for ``%(name)s``.
    """
    slots: set[int | str] = set()
    position = 0
    for match in _SPEC_PAT.finditer(msg):
        key, star_width, star_precision, kind = match.groups()
        if kind == "%":  # a literal `%%`, which consumes no argument
            continue
        position += bool(star_width) + bool(star_precision)
        slot: int | str = key if key is not None else position
        if _TRAILING_NAME_PAT.search(msg[: match.start()]):
            slots.add(slot)
        if key is None:
            position += 1
    return slots


def _blank_slots(args: Any, slots: set[int | str]) -> Any:
    """Replace the named argument slots with `REDACTED`, preserving arity and container type."""
    if isinstance(args, dict):
        return {k: (REDACTED if k in slots else v) for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(REDACTED if i in slots else v for i, v in enumerate(args))
    return args


def _make_factory(previous):
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if record.args:
            # NEVER rewrite `msg` here — see the module docstring. Dropping a `%s` from the format
            # string sends `logging` into `handleError`, which prints the raw args to stderr.
            record.args = _redact_arg(record.args)
            if isinstance(record.msg, str):
                slots = _credential_slots(record.msg)
                if slots:
                    record.args = _blank_slots(record.args, slots)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return record

    factory._radio_server_logsafe = True  # type: ignore[attr-defined]
    factory._radio_server_previous = previous  # type: ignore[attr-defined]
    return factory


def installed() -> bool:
    """Whether the redacting factory is the one currently in force."""
    return getattr(logging.getLogRecordFactory(), "_radio_server_logsafe", False) is True


def install() -> bool:
    """Install the redacting record factory. Returns ``False`` if it was already in force.

    The guard reads the *current* factory rather than a module flag on purpose: if another library
    installs its own factory afterwards, ours is no longer in force and re-wrapping is the correct
    response, not a no-op.
    """
    if installed():
        return False
    logging.setLogRecordFactory(_make_factory(logging.getLogRecordFactory()))
    return True


def uninstall() -> None:
    """Restore the factory this one wrapped, and forget every registered value (test seam)."""
    global _VALUE_PAT
    current = logging.getLogRecordFactory()
    previous = getattr(current, "_radio_server_previous", None)
    if previous is not None:
        logging.setLogRecordFactory(previous)
    _VALUE_PAT = None


def register_secret_values(secrets) -> list[str]:
    """Arm value-based redaction for the loaded secrets. Returns the names armed.

    Names only are logged, never values — and the skipped ones are named too, so an operator whose
    short token is *not* value-covered can see that from the journal rather than inferring it.
    """
    global _VALUE_PAT
    armed: list[str] = []
    skipped: list[str] = []
    for name, value in sorted(_secret_items(secrets)):
        if not isinstance(value, str) or not value:
            continue
        if name in VALUE_EXCLUDED:
            skipped.append(f"{name} (excluded by name)")
        elif len(value) < MIN_VALUE_LENGTH:
            skipped.append(f"{name} (under {MIN_VALUE_LENGTH} chars)")
        else:
            armed.append(name)

    values = {v for n, v in _secret_items(secrets) if n in armed and isinstance(v, str)}
    _VALUE_PAT = (
        re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)))
        if values
        else None
    )
    logger.info(
        "log redaction armed by value for: %s%s",
        ", ".join(armed) or "(nothing)",
        f"; redacted by name only: {', '.join(skipped)}" if skipped else "",
    )
    return armed


def _secret_items(secrets) -> tuple[tuple[str, Any], ...]:
    """Every ``(name, value)`` a `Secrets` holds — fixed names and Mumble entries alike.

    Asking the secrets object what it holds, rather than listing names here, is what makes a new
    secret category covered on the day it is added instead of the day someone remembers this file.
    """
    items = getattr(secrets, "items", None)
    return tuple(items()) if callable(items) else ()
