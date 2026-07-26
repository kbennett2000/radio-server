"""The docs<->code contract: the parts of the documentation a test can actually check (ADR 0147).

A full audit found that four documents still told the operator to set the frequency by hand on a
Baofeng — the exact capability ADRs 0142-0146 were built to deliver — that 18 ADRs had no row in the
ADR index, and that `deployment.md` named three WebSockets where the app registers seven. Every one of
those had been true for many cycles. The common cause is not carelessness: docs are updated by hand at
the end of a cycle and **nothing fails when they aren't**.

The two places this project already wired a lock did not drift: README <-> `install.sh`
(`test_docs_install_command.py`, ADR 0058) and `radio.toml.example` <-> `spec.py`
(`test_config.py`). So this file locks the rest of what is mechanically checkable.

What it can prove: an ADR file has an index row, a link points at a file that exists, an endpoint the
app serves is named in the API reference. What it cannot prove is whether the *prose* is true — that
"no CAT" sentence would have passed every check here. Those need review, and the ADR says so rather
than letting a green suite imply more than it measured.

Anchor fragments (`file.md#heading`) are deliberately NOT checked: GitHub's slugification of emoji and
variation selectors does not round-trip, it produced a false positive during the audit, and a check
that cries wolf gets deleted rather than fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

from radio_server.api import create_app
from radio_server.backends import Capability, MockRadio

_ROOT = Path(__file__).resolve().parent.parent  # tests/ is one level below the repo root
_ADR_DIR = _ROOT / "docs" / "adr"
_ADR_INDEX = _ADR_DIR / "README.md"
_API_MD = _ROOT / "docs" / "api.md"

#: Every markdown file that is documentation *of this project* — the ones a reader is sent to.
#: Vendored trees (`.venv`, `node_modules`, `web/dist`) are somebody else's docs.
_DOC_GLOBS = ("README.md", "AGENTS.md", "CLAUDE.md", "web/README.md")

#: FastAPI's own routes. They are real, but they are not part of the contract this project documents.
_FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

_ADR_ROW = re.compile(r"^\|\s*\[(\d{4})\]\(([^)]+)\)")
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+?)\s*(?:\"[^\"]*\")?\)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _doc_files() -> list[Path]:
    """Every project-authored markdown file, deduped and sorted."""
    found = {_ROOT / name for name in _DOC_GLOBS}
    found.update((_ROOT / "docs").rglob("*.md"))
    return sorted(p for p in found if p.is_file())


def _strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans so their contents can't look like links."""
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            kept.append("")
            continue
        kept.append("" if in_fence else re.sub(r"`[^`]*`", " ", line))
    return "\n".join(kept)


def _links(path: Path) -> list[tuple[int, str]]:
    """Every relative link target in `path` as (line number, target). Skips URLs and bare anchors."""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(_strip_code(path.read_text()).splitlines(), start=1):
        for target in _MD_LINK.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#", "<")):
                continue
            out.append((lineno, target))
    return out


def _adr_files() -> set[str]:
    """The filename of every ADR on disk (the index itself is not an ADR)."""
    return {p.name for p in _ADR_DIR.glob("*.md") if p.name != "README.md"}


def _index_rows() -> list[tuple[int, str, str, str]]:
    """Every ADR row in the index as (line number, number, link target, raw line)."""
    rows = []
    for lineno, line in enumerate(_ADR_INDEX.read_text().splitlines(), start=1):
        match = _ADR_ROW.match(line)
        if match:
            rows.append((lineno, match.group(1), match.group(2), line))
    return rows


# --- the ADR index is the project's decision map; a missing row makes an ADR unfindable ---------

def test_every_adr_file_has_exactly_one_index_row():
    # Keyed on the *filename*, not the number: `0001` is deliberately used twice (a process ADR and
    # the first technical one), and both are indexed. Numbers can't distinguish them; files can.
    targets = [target for _, _, target, _ in _index_rows()]
    missing = sorted(_adr_files() - set(targets))
    assert not missing, (
        f"{len(missing)} ADR(s) have no row in docs/adr/README.md and are unfindable from the "
        f"index: {missing}"
    )
    duplicated = sorted({t for t in targets if targets.count(t) > 1})
    assert not duplicated, f"indexed more than once in docs/adr/README.md: {duplicated}"


def test_every_index_row_points_at_an_adr_that_exists():
    on_disk = _adr_files()
    dangling = [
        f"docs/adr/README.md:{lineno} -> {target}"
        for lineno, _, target, _ in _index_rows()
        if target not in on_disk
    ]
    assert not dangling, f"index rows pointing at files that do not exist: {dangling}"


def test_every_index_row_has_three_columns():
    # ADR 0139's row shipped with its Status cell missing, so it rendered as an empty column on
    # GitHub. Cells may contain an escaped `\|` (ADR 0058's row quotes `curl … \| sh`), so split on
    # unescaped pipes only.
    bad = []
    for lineno, number, _, line in _index_rows():
        cells = [c for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) != 3:
            bad.append(f"docs/adr/README.md:{lineno} (ADR {number}) has {len(cells)} columns, not 3")
    assert not bad, "malformed ADR index rows: " + "; ".join(bad)


# --- a link that 404s is a doc that lies about where its own evidence lives ---------------------

def test_every_internal_markdown_link_resolves():
    broken = []
    for doc in _doc_files():
        for lineno, target in _links(doc):
            resolved = (doc.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                broken.append(f"{doc.relative_to(_ROOT)}:{lineno} -> {target}")
    assert not broken, f"{len(broken)} internal link(s) point at nothing:\n  " + "\n  ".join(broken)


# --- the API reference must name every endpoint the app actually serves -------------------------

def _app_paths() -> tuple[set[str], set[str]]:
    """(REST paths, WebSocket paths) the app serves, from the same DI seam the API tests drive."""
    app = create_app(MockRadio(supports_cat=True), api_token="docs-contract")
    rest = set(app.openapi()["paths"]) - _FRAMEWORK_PATHS
    sockets = {
        route.path
        for route in app.routes
        if type(route).__name__ == "APIWebSocketRoute" and hasattr(route, "path")
    }
    return rest, sockets


def test_every_rest_path_appears_in_api_md():
    # The literal path, so `/dstar/link` can't be "covered" by a paragraph about D-STAR in general.
    rest, _ = _app_paths()
    doc = _API_MD.read_text()
    missing = sorted(p for p in rest if p not in doc)
    assert not missing, (
        f"{len(missing)} endpoint(s) the app serves are not named anywhere in docs/api.md — "
        f"document them or the operator cannot discover them: {missing}"
    )


def test_every_websocket_path_appears_in_api_md():
    _, sockets = _app_paths()
    doc = _API_MD.read_text()
    missing = sorted(p for p in sockets if p not in doc)
    assert not missing, f"WebSocket(s) missing from docs/api.md: {missing}"
    # deployment.md told operators to proxy three of these when there were seven, which silently
    # breaks browser audio behind nginx. Whatever the count is, the reference must carry all of them.
    assert len(sockets) >= 7, f"expected at least the 7 known sockets, found {sorted(sockets)}"


def test_every_capability_appears_in_api_md():
    # Capability strings are the vocabulary of every 501 body, so a new one that never reaches the
    # reference leaves clients unable to interpret a refusal they will receive.
    doc = _API_MD.read_text()
    missing = sorted(str(c) for c in Capability if str(c) not in doc)
    assert not missing, f"capabilities missing from docs/api.md: {missing}"
