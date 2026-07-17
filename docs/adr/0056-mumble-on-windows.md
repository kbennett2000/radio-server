# 0056 — Mumble link installable and connectable on native Windows

Status: Accepted

## Context

ADR 0055 landed the last software piece for a native Windows install (DTMF decodes with no
`multimon-ng`). Two blockers remain before the Mumble link works on a clean Windows box with no git
and no hand-installed system libraries — both are **packaging**, not code:

1. **The `pymumble` pin needs git.** The `mumble` extra pinned
   `pymumble @ git+https://github.com/azlux/pymumble@a560e601…` (the branch head that fixes the 3.12
   `ssl.wrap_socket` removal — see the pin's own comment in `pyproject.toml`). Verified empirically
   this cycle (guardrail 1): with git off `PATH`, `uv sync --extra mumble` fails with *"Git executable
   not found. Ensure that Git is installed and available."* — uv shells out to a system git for `git+`
   sources; it does not vendor one. But `scripts/install.ps1` already treats git-absent as the
   **expected** Windows state — it zip-downloads the repo itself when git is missing. So the
   installer's own target user cannot install the extra the installer depends on.

2. **libopus.** pymumble's transitive dependency `opuslib` (3.0.1) is a pure-Python ctypes wrapper: at
   import time it runs `ctypes.util.find_library('opus')` and, on failure, raises. A stock Windows box
   has no `opus.dll`, so the extra installs but `import pymumble_py3` blows up.

Constraints found in the tree:

- **No plain-`opuslib` binary wheel exists for our stack.** Investigated (guardrail 1): the maintained
  bundled-libopus packages — `opuslib-next-bundled` (ships libopus in win/mac/linux wheels) and PyOgg —
  expose *different import names* (`opuslib_next`, `pyogg`). Our pinned pymumble does `import opuslib`
  internally; it will never call a differently-named binding. So no drop-in package removes
  `libopus0`/`brew opus`, and libopus stays a Windows-only vendor job (mac/linux keep their one-line
  system installs).
- **`find_library` ignores `os.add_dll_directory` on Windows.** It walks `os.environ['PATH']` looking
  for `opus`/`opus.dll` and does not consult directories registered via `add_dll_directory`
  ([cpython#111104]). So making opuslib resolve a vendored DLL means putting its directory on `PATH`,
  not (only) registering it.
- **opuslib 3.0.1 raises a bare `Exception`** (`"Could not find Opus library. Make sure it is
  installed."`), not `OSError`, when the library is absent. The current `doctor --link` and
  `PyMumbleClient._pm()` catch only `OSError` / `(ImportError, OSError)` — so today the common
  missing-libopus case escapes as a raw traceback instead of the friendly hint.
- **pymumble builds without git metadata.** Its `setup.py` reads the version from
  `pymumble_py3/constants.py` (string-parse), not setuptools_scm — so a GitHub archive tarball (no
  `.git`) builds byte-identically to the git checkout at the same commit.

## Decision

**Blocker 1 — swap the `git+` pin for a GitHub archive tarball at the same SHA.**
`pymumble @ https://github.com/azlux/pymumble/archive/a560e601….tar.gz`. uv fetches a direct URL
sdist with its own HTTP client — no git binary. The commit is unchanged, so the 3.12 SSL fix (the
reason for the SHA) is preserved; the pin's existing WHY-the-SHA comment stays, with a line added for
why the tarball form. `uv.lock` is regenerated (the source becomes the tarball URL + a content hash).

**Blocker 2 — vendor `opus.dll` (amd64) and load it explicitly.**
`radio_server/_vendor/win-amd64/opus.dll` (self-contained, BSD; license bundled alongside). A new
`radio_server/link/_opus.py` exposes `ensure_opus_loadable()`, called **before** `import pymumble_py3`
in both seams (`PyMumbleClient._pm()` and `doctor._link()`):

- **On Windows amd64** it prepends the vendored directory to `os.environ['PATH']` (so opuslib's
  `find_library('opus')` resolves it — the load-bearing step) and also calls `os.add_dll_directory`
  for dependent-DLL safety. Idempotent.
- **Off Windows** it is a no-op — mac/linux use the system libopus.
- **On Windows arm64** there is no vendored binary; it says so and lets the import fail into the
  friendly per-platform hint. arm64 is explicitly unsupported this cycle, not silently broken.

It returns a short reason string, so the path is **explicit and debuggable** (doctor prints it) rather
than import-order luck.

**Doctor — per-platform, accurate, and reachable libopus hint.**
A shared `opus_install_hint()` (in `_opus.py`, one source for a future docs cycle) keys on
`platform.system()`: macOS → *install Homebrew, then `brew install opus`*; Linux → *`sudo apt install
libopus0`*; Windows → the DLL ships with the extra, so a failure means an unsupported arch or a broken
bundle (no dead-end "install libopus"). Because opuslib raises a **bare `Exception`** when the library
is missing, `doctor._link()` and `_pm()` broaden their second catch from `OSError` to `Exception` so
the hint is actually reached; `ImportError` still maps to the "install the mumble extra" message. The
broadening is safe: at that point the only realistic non-import failure of `import pymumble_py3` is the
opus load.

### Alternative considered — a bundled-libopus PyPI wheel (`opuslib-next-bundled` / PyOgg)

Strictly the best answer *if one fit* — a normal dependency uv resolves, deleting `libopus0`/`brew
opus` on all three platforms. Neither fits: both expose a different import name than the plain
`opuslib` our pinned pymumble imports, so pymumble would never load their bundled binary. Adopting one
would mean vendoring/patching pymumble to import it — out of scope, and heavier than shipping one DLL.

### Alternative considered — `os.add_dll_directory` instead of a `PATH` prepend

The intuitive Windows DLL-directory API, but `ctypes.util.find_library` — which opuslib uses — does
**not** consult it; it only walks `PATH` ([cpython#111104]). Registering the dir without the `PATH`
prepend would leave `find_library('opus')` returning `None`. We do both, but `PATH` is what makes it
resolve.

### Alternative considered — keep the `git+` pin and require git on Windows

Rejected: it contradicts `install.ps1`, which already assumes git is absent and works around it. The
target operator is non-technical (ADR 0053); "install git first" is exactly the friction the installer
removes.

## Consequences

- **A clean Windows amd64 box installs and connects with no manual steps:** git-less `uv sync --extra
  mumble` succeeds (tarball), and `doctor --link` loads the vendored `opus.dll` and connects. mac/linux
  are unchanged.
- **KNOWN RISK — GitHub archive tarballs are generated on demand and have not always been byte-stable**,
  so the hash locked in `uv.lock` can in principle break out from under us. If that ever bites, the
  fix is to **vendor pymumble** — it is pure Python, upstream's last release was 2021, and we are
  already pinned to a branch commit that will never ship as a release. Named here as the fallback;
  **not done now** (it enlarges the tree and there is no failure yet to justify it).
- **Windows arm64 is unsupported** (no vendored binary); the doctor hint says so rather than emitting a
  confusing "install libopus".
- **libopus stays in `docs/install.md`.** Because no package covers the plain-`opuslib` binding, the
  `libopus0`/`brew opus` lines cannot leave the docs — that rewrite (and dropping the Windows-via-WSL2
  framing now that native Windows works) is a separate cycle, deliberately out of scope here.
- **Verify on hardware:** the acceptance is a real Windows box — git-less `uv sync --extra mumble`
  succeeds; `import pymumble_py3` succeeds after `ensure_opus_loadable()`; `doctor --link` connects to
  the demo entry and reports pass. Empirical, like the DTMF bench items (ADR 0038/0054). The vendored
  DLL's provenance and sha256 are recorded in `radio_server/_vendor/README.md`.
- **No installer changes** (defaulting `--extra mumble` is the next cycle, unblocked by this one) and
  **no mac/linux vendoring** (no free cross-platform package was found).

[cpython#111104]: https://github.com/python/cpython/issues/111104
