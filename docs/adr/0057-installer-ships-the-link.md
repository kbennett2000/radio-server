# 0057 — The one-line installer ships the Mumble link on all three platforms

Status: Accepted

## Context

The README's headline promise is: run one command on a clean box, open the control panel, click
Connect on the demo entry, talk to the world. Today it fails everywhere. Both installers
(`scripts/install.sh`, `scripts/install.ps1`) run a bare `uv sync` — no extras — so `pymumble` is
never installed; the `mumble` extra lives only behind `install.sh --with-hardware` (and not at all in
`install.ps1`). Then both print **"All set."** The browser Mumble path — the headline feature, which
needs no radio — is unreachable, and the installer says success anyway. An installer that lies is how
the current wall got built.

ADR 0056 built `ensure_opus_loadable()` and vendored a Windows `opus.dll` so `opuslib`'s import-time
`find_library('opus')` resolves. 0056 asked whether `opuslib-next-bundled` / PyOgg were a drop-in for
the `opuslib` pymumble imports and answered no (different import name). But that was the wrong question
once the shim existed. The right one:

**Do those wheels carry a usable libopus binary we can point the shim at?** Re-investigated this cycle
by pulling the full wheel tag matrix (the fact that decides it) and testing the shim end-to-end.

`opuslib-next-bundled` 0.1.1 tag matrix:

| wheel tag | carrier binary | covers |
|---|---|---|
| `win_amd64` | `opus.dll` | Windows x64 |
| `macosx_11_0_arm64` | `libopus.dylib` | Apple Silicon (every Mac since 2020) |
| `macosx_10_9_x86_64` | `libopus.dylib` | Intel Mac |
| `manylinux2014_aarch64` | `libopus.so` (confirmed ARM aarch64) | Raspberry Pi / arm64 Linux |
| `manylinux2014_x86_64` | `libopus.so` (deps: only libm/pthread/libc) | x86_64 Linux |
| *(sdist only — no wheel)* | — | win-arm64, 32-bit, musl/Alpine |

Wheels exist for every platform a radio box actually runs on — crucially **linux aarch64 (a Pi, the
obvious box next to a radio) and macOS arm64 (every recent Mac)**. Verified end-to-end on Linux:
patching `ctypes.util.find_library('opus')` to return the wheel's `libopus.so`, then importing the
**plain `opuslib`** pymumble uses, loads libopus from the wheel (`opuslib.api.libopus._name` = the
wheel path).

Two facts make the carrier robust, not fragile:

- **PyPI wheels are immutable and lock-hashed** — none of the on-demand-tarball byte-instability that
  ADR 0056 flagged for the pymumble git archive.
- **The win_amd64 `opus.dll` is byte-identical to the DLL 0056 vendored — proven, not asserted.** Both
  SHA256 = `d553adca7b939b9bf2eb9daa85f68c3c41154b97acb61238342fa8c387df05cc`, recomputed independently
  from the live wheel and the committed file (0056 extracted its DLL from this very wheel). So retiring
  the vendored DLL swaps nothing — it is the same bytes, sourced as a proper dependency.

## Decision

**Adopt `opuslib-next-bundled` as a libopus *binary carrier* (never imported for its bindings), on all
target platforms, and retire the vendored DLL.** Ship the `mumble` extra by default. Earn "All set."

- **One carrier code path in `ensure_opus_loadable()`.** Locate `opuslib_next/_native/{libopus.so |
  libopus.dylib | opus.dll}` via `importlib.util.find_spec("opuslib_next")` (no bindings import), then
  patch `ctypes.util.find_library` so `'opus'` returns that path and every other name delegates to the
  original. Uniform across win/mac/linux — a full path satisfies `find_library` on Windows too — so
  0056's Windows `PATH`-prepend / `_VENDOR_*` machinery is deleted.

- **The `find_library` patch is a bounded hack, and it is the only option.** On Linux/macOS
  `find_library` shells out to `ldconfig`/`gcc`/`ld` and will not see a wheel's private directory, and
  `LD_LIBRARY_PATH` cannot be set usefully after process start. Patching the one function that exists to
  answer exactly this question — while delegating every non-`opus` lookup unchanged — is the narrowest
  intervention. It must run before `opuslib` is imported (it does, in every entrypoint), since opuslib
  caches the lookup at import.

- **Carrier gated by an environment marker to exactly the five wheel tags.** No-wheel tags (win-arm64,
  32-bit) omit the carrier and degrade to the system-lib hint, rather than hard-failing `uv sync
  --extra mumble` on an sdist build with no toolchain. Per-platform disposition:
  - **carrier wheel:** win-amd64, macOS x86_64 + arm64, linux glibc x86_64 + aarch64.
  - **system libopus (via `opus_install_hint`):** win-arm64, 32-bit — no wheel.
  - **residual edge:** musl/Alpine reports the same `sys_platform`/`platform_machine` as glibc, so the
    marker cannot exclude it; there it would attempt the sdist. Named as a non-target (Raspberry Pi OS
    is glibc; the README targets desktop mac/win/linux + Pi).

- **The `mumble` extra is installed by default, not behind `--with-hardware`.** The browser Mumble
  client needs no radio and is the headline feature. `--with-hardware` keeps installing hardware + tts +
  mumble (each sync names every extra it wants — the single-sync rule).

- **"All set." is earned.** Before the success banner, each installer runs `python -m
  radio_server.link._opus`, which calls `ensure_opus_loadable()` and imports `pymumble_py3` — local,
  fast, and exercising the exact shim that was silently broken. On failure the banner does not claim the
  voice link works and prints the per-platform hint.

- **`docs/getting-started.md` Step 2 (`uv sync`) gains `--extra mumble`** so the hand path installs what
  the one-liner installs — a one-token correctness fix, not the docs cycle.

### Alternative considered — per-platform system-lib prompts in the installer

The fallback if no wheel carried the binary: `install.sh` prompts (via the existing `/dev/tty` `ask()`)
for `sudo apt install libopus0` / `brew install opus` / the Homebrew install line. Rejected as the
primary path because the carrier is verified and collapses three platform stories into one dependency.
Kept only as the escape hatch if the carrier ever proves fragile.

### Alternative considered — keep the vendored DLL for Windows, carrier only for linux/mac

Rejected: the carrier's win_amd64 binary is byte-identical to the vendored DLL (two matching SHA256s),
so keeping both means two load mechanisms for zero difference in the shipped bytes.

## Consequences

- **The headline command now installs the voice link on every target platform**, and the banner tells
  the truth when it doesn't. `pymumble` + libopus arrive with a default `uv sync --extra mumble`.
- **One dependency, one code path, one license.** The vendored `radio_server/_vendor/` tree (opus.dll,
  its bundled license, README) is deleted; `opuslib-next-bundled` carries the binary and its BSD license
  as a normal dependency.
- **Risk: all target platforms now lean on one young package (0.1.x, 2026).** Mitigated by a
  version+hash pin (immutable wheels — a lockfile install is reproducible). If it is ever yanked, the
  fallback is to re-vendor (the win DLL is in git history at ADR 0056; linux/mac vendor the same way) or
  the per-platform system-lib prompts above.
- **Deferred, recorded so scope stays small:** `docs/install.md` still lists `libopus0` / `brew opus` /
  the vendored-DLL story and routes Windows through WSL2. With opus now a dependency (and multimon
  already optional since ADR 0055), that extras table can collapse to PortAudio + a voice — but the
  rewrite is the next cycle, gated on hardware verification, not on this.
- **Verify on hardware:** a real Windows amd64 box (git-less `uv sync --extra mumble` → `python -m
  radio_server.link._opus` OK → `doctor --link` passes) and macOS arm64 (CI cannot run it; the
  mechanism is identical to the verified Linux path — flagged unverified).
