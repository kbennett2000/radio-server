"""Import a CHIRP CSV channel export into ``[[presets]]`` (ADR 0133).

CHIRP is what most operators already keep their repeater list in, so the channel list does not need
retyping — it needs translating. This module is the translation, and its whole posture is **never
guess**: a row it does not fully understand is skipped and printed, not approximated. A silently
mistranslated repeater is worse than a missing one, because it fails on the air rather than at the
terminal, and it fails by transmitting.

Two mappings are load-bearing and easy to get backwards:

* **`TSQL` takes its tone from `cToneFreq`, not `rToneFreq`.** In a real export most `TSQL` rows
  leave `rToneFreq` at its 88.5 default while the *actual* repeater tone sits in `cToneFreq` — in
  the 37-channel list this was written against, 26 rows are exactly that shape. Reading `rToneFreq`
  (which IS correct for `Tone` rows) would transmit 88.5 Hz at repeaters expecting 100.0 / 103.5 /
  107.2 / 123.0 / 141.3, and not one of them would key. The two rows where the columns agree cannot
  distinguish the mappings, so any regression test has to pin a row where they differ.
* **Frequencies are the repeater's OUTPUT.** `Duplex` + `Offset` say where to transmit; we listen on
  the column value. Getting the sign backwards points the transmitter at another repeater's output.

All arithmetic is integer. `Decimal` per field, then integer subtraction — never
``(float(freq) - float(offset)) * 1e6``, which for ``145.145 - 0.600`` yields
``144545000.00000003`` and across the 2 m/70 cm bands lands 1 Hz low in 2160 cases. The backends
reject anything off the 10 Hz raster, so that is a hard 422 for one repeater out of forty: the
worst-shaped failure available.

Run it::

    python -m radio_server.chirp channels.csv --into radio.toml
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .presets import CTCSS_TONES, MAX_NAME_LENGTH, VALID_MODES, resolve_presets

__all__ = ["ChirpImport", "parse_chirp_csv", "main"]

#: Columns every supported row is read from. A file missing one of these is not a CHIRP export we
#: understand, and saying which column is missing beats a KeyError.
REQUIRED_COLUMNS = ("Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq", "Mode")

#: ``Cross Mode`` is optional because a hand-trimmed export often drops it — but then a ``Cross``
#: row's receive tone cannot be *known*, only assumed. See :func:`parse_chirp_csv`.
CROSS_COLUMN = "Cross Mode"
SUPPORTED_CROSS = "Tone->Tone"

#: `Duplex` values that mean "transmit somewhere else". `split` (Offset holds an absolute TX
#: frequency) and `off` (transmit disabled) are real CHIRP values this does not implement, so rows
#: carrying them are skipped out loud rather than silently read as simplex.
DUPLEX_SIMPLEX = ("", "simplex")
DUPLEX_SIGNS = {"-": -1, "+": 1}


@dataclass
class ChirpImport:
    """The result of a parse: what to write, and everything that did not survive it."""

    #: Preset tables ready for `config.save.save_presets` / `presets.resolve_presets`.
    presets: list[dict] = field(default_factory=list)
    #: Rows dropped, each a full human sentence naming the row and the reason.
    skipped: list[str] = field(default_factory=list)
    #: Things assumed rather than read. Printed like the skips: an assumption that announces itself
    #: is not an assertion (guardrail 1).
    assumed: list[str] = field(default_factory=list)


class ChirpError(Exception):
    """The file is not usable at all — a missing column, not a bad row."""


def _hz(text: str, column: str) -> int:
    """MHz as written -> whole Hz, exactly. Integer arithmetic only; no float ever touches this."""
    try:
        value = Decimal((text or "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"{column}={text!r} is not a number") from exc
    hz = value.scaleb(6)
    if hz != hz.to_integral_value():
        raise ValueError(f"{column}={text!r} is not a whole number of Hz")
    return int(hz)


def _tone(text: str, column: str) -> float:
    try:
        value = float((text or "").strip())
    except ValueError as exc:
        raise ValueError(f"{column}={text!r} is not a CTCSS frequency") from exc
    if value not in CTCSS_TONES:
        raise ValueError(f"{column}={text!r} is not a standard EIA CTCSS tone")
    return value


def parse_chirp_csv(text: str) -> ChirpImport:
    """Translate a CHIRP CSV export into preset tables. Unreadable rows are skipped, never guessed.

    Tone mapping (the whole reason this function is not three lines):

    ============ ================ ================
    ``Tone``     ``tx_tone``      ``rx_tone``
    ============ ================ ================
    (blank)      —                —
    ``Tone``     ``rToneFreq``    —
    ``TSQL``     ``cToneFreq``    ``cToneFreq``
    ``Cross``    ``rToneFreq``    ``cToneFreq``
    ============ ================ ================

    ``DTCS`` / ``TSQL-R`` / ``DTCS-R`` are skipped: reading a DTCS row as tone-less would produce a
    preset that quietly fails to open its repeater, which is the same silent wrongness as inverting
    TSQL.

    ``Cross`` needs the ``Cross Mode`` column to know it is ``Tone->Tone`` rather than
    ``Tone->DTCS`` or ``->Tone``. When the column is present it is *required* to be ``Tone->Tone``;
    when the export does not carry it at all, ``Tone->Tone`` is assumed and the affected row is
    named in :attr:`ChirpImport.assumed`.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ChirpError("the file is empty — expected a CHIRP CSV with a header row")
    have = {name.strip() for name in reader.fieldnames}
    missing = [column for column in REQUIRED_COLUMNS if column not in have]
    if missing:
        raise ChirpError(
            f"not a CHIRP export this understands: missing column(s) {', '.join(missing)}. "
            f"Found: {', '.join(sorted(have))}"
        )
    has_cross_column = CROSS_COLUMN in have

    out = ChirpImport()
    seen: dict[str, str] = {}
    for number, row in enumerate(reader, start=2):  # start=2: row 1 is the header, as an editor counts
        where = f"row {number}"
        name = (row.get("Name") or "").strip()
        if name:
            where = f"{where} ({name})"
        if not name or len(name) > MAX_NAME_LENGTH:
            out.skipped.append(f"{where}: name must be 1-{MAX_NAME_LENGTH} characters")
            continue
        if name.casefold() in seen:
            out.skipped.append(f"{where}: duplicate of {seen[name.casefold()]!r} (names must be unique)")
            continue

        mode = (row.get("Mode") or "FM").strip().upper()
        if mode not in VALID_MODES:
            out.skipped.append(f"{where}: mode {mode!r} is not one of {', '.join(sorted(VALID_MODES))}")
            continue

        try:
            frequency = _hz(row.get("Frequency", ""), "Frequency")
        except ValueError as exc:
            out.skipped.append(f"{where}: {exc}")
            continue

        duplex = (row.get("Duplex") or "").strip()
        tx_frequency: int | None = None
        if duplex.lower() in DUPLEX_SIMPLEX:
            tx_frequency = None
        elif duplex in DUPLEX_SIGNS:
            try:
                offset = _hz(row.get("Offset", ""), "Offset")
            except ValueError as exc:
                out.skipped.append(f"{where}: {exc}")
                continue
            if offset == 0:
                out.skipped.append(f"{where}: duplex {duplex!r} with a zero offset — is it simplex?")
                continue
            tx_frequency = frequency + DUPLEX_SIGNS[duplex] * offset
        else:
            out.skipped.append(
                f"{where}: duplex {duplex!r} is not supported (only '', '-' and '+'; "
                f"'split' and 'off' need their own handling)"
            )
            continue

        tone_mode = (row.get("Tone") or "").strip()
        tx_tone = rx_tone = None
        try:
            if tone_mode == "":
                pass
            elif tone_mode == "Tone":
                tx_tone = _tone(row.get("rToneFreq", ""), "rToneFreq")
            elif tone_mode == "TSQL":
                # cToneFreq, NOT rToneFreq — see the module docstring.
                tx_tone = rx_tone = _tone(row.get("cToneFreq", ""), "cToneFreq")
            elif tone_mode == "Cross":
                cross = (row.get(CROSS_COLUMN) or "").strip() if has_cross_column else ""
                if has_cross_column and cross != SUPPORTED_CROSS:
                    out.skipped.append(
                        f"{where}: cross mode {cross!r} is not supported (only {SUPPORTED_CROSS})"
                    )
                    continue
                if not has_cross_column:
                    out.assumed.append(
                        f"{where}: Tone=Cross with no {CROSS_COLUMN!r} column — assumed "
                        f"{SUPPORTED_CROSS}. Check this row against the radio."
                    )
                tx_tone = _tone(row.get("rToneFreq", ""), "rToneFreq")
                rx_tone = _tone(row.get("cToneFreq", ""), "cToneFreq")
            else:
                out.skipped.append(
                    f"{where}: tone mode {tone_mode!r} is not supported (only '', Tone, TSQL, Cross; "
                    f"DTCS is not implemented and must not be read as 'no tone')"
                )
                continue
        except ValueError as exc:
            out.skipped.append(f"{where}: {exc}")
            continue

        seen[name.casefold()] = name
        preset: dict = {"name": name, "frequency": frequency}
        if tx_frequency is not None:
            preset["tx_frequency"] = tx_frequency
        if tx_tone is not None:
            preset["tx_tone"] = tx_tone
        if rx_tone is not None:
            preset["rx_tone"] = rx_tone
        preset["mode"] = mode
        comment = (row.get("Comment") or "").strip()
        if comment:
            preset["comment"] = comment
        out.presets.append(preset)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m radio_server.chirp",
        description="Import a CHIRP CSV channel export into a radio.toml [[presets]] list.",
    )
    parser.add_argument("csv", help="the CHIRP CSV export to read")
    parser.add_argument(
        "--into", metavar="RADIO_TOML",
        help="merge into this radio.toml (by preset name); without it, print the result and exit",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="drop the existing [[presets]] list instead of merging into it",
    )
    args = parser.parse_args(argv)

    try:
        text = Path(args.csv).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"cannot read {args.csv}: {exc}", file=sys.stderr)
        return 2
    try:
        result = parse_chirp_csv(text)
    except ChirpError as exc:
        print(f"{args.csv}: {exc}", file=sys.stderr)
        return 2

    for note in result.assumed:
        print(f"  assumed: {note}")
    for note in result.skipped:
        print(f"  skipped: {note}")
    print(
        f"{len(result.presets)} channel(s) read from {args.csv}"
        + (f", {len(result.skipped)} skipped" if result.skipped else "")
    )
    if not result.presets:
        print("nothing to import", file=sys.stderr)
        return 1

    if not args.into:
        for preset in result.presets:
            legs = f"{preset['frequency'] / 1e6:.4f}"
            if "tx_frequency" in preset:
                offset = (preset["tx_frequency"] - preset["frequency"]) / 1e6
                legs += f" {offset:+.3f}"
            tone = f" {preset['tx_tone']} Hz" if "tx_tone" in preset else ""
            print(f"  {preset['name']:<20} {legs}{tone}")
        print("\n(--into radio.toml to write them)")
        return 0

    # Validate the WHOLE merged list before touching the file. `resolve_presets` runs at app
    # construction, so one bad entry does not degrade the station — it stops it from starting, and
    # radio.toml is hand-edited with no version control behind it.
    target = Path(args.into)
    from .config import load_presets
    from .config.save import save_presets

    existing = [] if args.replace else (load_presets(target) if target.is_file() else None) or []
    by_name = {str(e.get("name", "")).casefold(): e for e in existing}
    merged = list(existing)
    for preset in result.presets:
        clean = {k: v for k, v in preset.items() if k != "comment"}
        key = clean["name"].casefold()
        if key in by_name:
            merged[merged.index(by_name[key])] = clean
        else:
            merged.append(clean)
    try:
        resolve_presets(merged)
    except RuntimeError as exc:
        print(f"refusing to write {target}: the merged preset list is invalid — {exc}", file=sys.stderr)
        print("nothing was written.", file=sys.stderr)
        return 1

    if target.is_file():
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"backed up {target} -> {backup}")
    # Write via a temp file + atomic replace: a crash mid-write must not leave a half-written
    # radio.toml, which is a station that will not start.
    scratch = target.with_suffix(target.suffix + ".tmp")
    if target.is_file():
        scratch.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    total = save_presets(result.presets, scratch, replace=args.replace)
    os.replace(scratch, target)
    print(f"wrote {total} preset(s) to {target}")
    print("restart the server to pick them up (presets are read at startup).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
