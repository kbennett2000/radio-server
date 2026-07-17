# Vendored native libraries

Binaries bundled so a specific platform can run a feature without a manual system-library install.
Loaded explicitly by an in-tree shim — never by import-order luck. See ADR 0056.

## `win-amd64/opus.dll` — the Opus codec for the Mumble link on native Windows

**Why it's here.** The `mumble` extra's `opuslib` (3.0.1) is a ctypes wrapper that does
`ctypes.util.find_library('opus')` at import time. A stock Windows box has no `opus.dll`, so pymumble
can't import. Vendoring the DLL — named `opus.dll` so `find_library('opus')` resolves it — plus the
`radio_server/link/_opus.py` loader (which prepends this directory to `PATH` before importing pymumble)
makes the link work with no manual library install. On macOS/Linux nothing here is used; those keep
their one-line system installs (`brew install opus` / `apt install libopus0`).

**What it is.** The Opus reference codec (libopus, BSD 3-Clause — see `win-amd64/opus.LICENSE`).
PE32+ x86-64 DLL, 80 `opus_*` exports (all four opuslib calls — `opus_encoder_create`, `opus_encode`,
`opus_decoder_create`, `opus_decode` — present and verified).

- **sha256:** `d553adca7b939b9bf2eb9daa85f68c3c41154b97acb61238342fa8c387df05cc`
- **size:** 463360 bytes
- **provenance:** extracted verbatim from the BSD-licensed PyPI wheel
  `opuslib_next_bundled-0.1.1-py3-none-win_amd64.whl` (`opuslib_next/_native/opus.dll`), which bundles
  an `xiph/opus` build. Renaming was unnecessary — the wheel already ships it as `opus.dll`.
- **runtime dependency:** `VCRUNTIME140.dll` + the Universal CRT (`api-ms-win-crt-*`). Both are present
  on any box that can run CPython for Windows (Python itself links the same MSVC/UCRT runtime), so no
  extra redistributable is required.

**Arch coverage.** amd64 only. Windows **arm64** has no vendored binary; `ensure_opus_loadable()`
reports that explicitly and the import fails into the per-platform hint rather than a mystery.

**Updating.** Replace the file, then update the sha256/size/provenance above and re-verify the export
list (`objdump -x opus.dll | grep -oE 'opus_[a-z_]+'`).
