# 0067 — extras taxonomy: a node installs what it needs and nothing else

Status: Accepted

## Context

`radio-server`'s optional dependencies were grouped by the *first* consumer that needed each package,
not by what a given node actually installs:

- `hardware = ["pyserial>=3.5", "sounddevice>=0.4"]` — pyserial (a serial control line) bundled with
  sounddevice (a USB sound card).
- `mumble = [pymumble, opuslib-next-bundled]` — the Opus stack rode the Mumble extra, and `opuslib`
  itself (the ctypes binding both the Mumble link and the kv4p codec import) was named **nowhere**: it
  arrived only transitively via `pymumble`.

The kv4p HT backend (ADR 0063–0066) needs exactly two things: a **serial** transport (pyserial) and the
**Opus** codec (opuslib + a loadable libopus). It has no sound card and nothing to do with Mumble. Under
the old grouping a kv4p node had to run `uv sync --extra hardware --extra mumble` to get pyserial +
libopus — and that dragged in `sounddevice`, the system `libportaudio2`, and `pymumble`, none of which
it ever calls. Worse, `uv sync` is **exact**: it uninstalls anything not named on the invocation, so
naming the wrong set silently removes the right one (the same exactness that bit the Mumble link in
ADR 0051/0057).

## Decision

Factor the leaves and compose the backends from them (PEP 621 self-referencing extras, which uv
resolves — verified this cycle on uv 0.11.17, see below).

| extra | contents | for |
| --- | --- | --- |
| `serial` | `pyserial>=3.5` | leaf — serial control line (AIOC PTT; kv4p USB transport) |
| `soundcard` | `sounddevice>=0.4` (+ system `libportaudio2`) | leaf — AIOC USB sound card |
| `opus` | `opuslib>=3.0.1` + `opuslib-next-bundled` (env-marked carrier) | leaf — the Opus codec stack |
| `tts` | `piper-tts`, `onnxruntime` | leaf — Piper neural TTS (unchanged) |
| `hardware` | `serial` + `soundcard` | AIOC/Baofeng backend |
| `kv4p` | `serial` + `opus` | kv4p HT backend |
| `mumble` | `opus` + `pymumble` (git-pinned tarball) | Mumble/Murmur voice link |

Two points of care:

1. **`opuslib>=3.0.1` is now named explicitly** in the `opus` leaf. It used to arrive only transitively
   through `pymumble`, so a kv4p-only sync would have gotten the carrier's libopus but no binding.
   `pymumble` still pins `opuslib==3.0.1`, so the workspace lock stays at 3.0.1 — naming it here cannot
   drift the version.
2. **The `opuslib-next-bundled` env-marker gating from ADR 0057 is moved into `opus` intact** — not
   re-derived. It restricts the carrier wheel to the five tags that publish one (win-amd64, mac-x86_64,
   mac-arm64, linux-x86_64, linux-aarch64); a no-wheel tag omits it and falls back to the system-lib
   hint instead of hard-failing `uv sync` on an sdist build.

### Compatibility — `hardware` and `mumble` closures are unchanged

- `hardware` = `serial + soundcard` = **pyserial + sounddevice** — the same two packages as before.
- `mumble` = `opus + pymumble` = **pymumble + opuslib + carrier** — the same closure as before (opuslib
  was already there transitively; it is now explicit).

So `update-radio-server.sh` and both installers, which run `--extra hardware --extra tts --extra mumble`
(or `--extra mumble`), resolve to the **identical** package set. Kris's deployed Ubuntu box keeps
working exactly; the installer commands are left untouched on purpose (see Consequences).

### The install hint follows the extra

The shared `opus_install_hint()` (`radio_server/link/_opus.py`) hardcoded `uv sync --extra mumble`, and
the kv4p codec (`backends/kv4p/audio.py::_load_opus`) reused it — so a kv4p node with a missing libopus
was told to install the *Mumble* extra. `opus_install_hint(*, extra="mumble", …)` now takes the caller's
extra; the kv4p codec passes `extra="kv4p"`, the Mumble link keeps the default. Same shared `opus` leaf,
correct remediation for each caller.

## Consequences

- **A kv4p node needs no system library at all.** `uv sync --extra kv4p` installs pyserial + opuslib +
  the bundled libopus carrier — no sounddevice, no `libportaudio2`, no `pymumble`, no Homebrew step.
  libopus loads from the carrier wheel on its five tags and falls back to the system-lib hint elsewhere.
  **Verified concretely this cycle** (see below), because it is the claim the docs cycle will publish.
- **Self-referencing extras resolve under uv** (0.11.17): `uv lock` flattened `radio-server[serial]` /
  `radio-server[opus]` into the leaf packages with the env markers intact, with **no version drift**.
  The fallback (duplicating the leaf lists inline) was therefore not needed.
- **Deferred to the next (docs) cycle, recorded here so it is not lost:**
  - The installers (`scripts/install.sh` `--with-hardware`, `scripts/install.ps1`) have no kv4p install
    path yet — `--with-hardware` is the AIOC path and would drag a sound card onto a kv4p node. Adding a
    kv4p install path is a user-facing UX decision for the docs cycle.
  - When that path is added, the installers' "earn the banner" gate (`check_mumble_importable()`,
    ADR 0057) is **wrong for a kv4p install that has no Mumble** — it must become conditional on whether
    the mumble extra was actually requested. It stays correct as-is for today's mumble-default installer.
  - User-doc prose (`docs/install.md`, `getting-started.md`, `deployment.md`, `configuration.md`,
    `hardware-bringup.md`) still says `--extra hardware --extra mumble`; the facts for updating it are in
    `docs/HANDOFF.md`.

## Verification (this cycle, no hardware)

- `uv sync --extra kv4p` in a clean throwaway env installed **pyserial 3.5, opuslib 3.0.1,
  opuslib-next-bundled 0.1.1** and nothing else radio-specific; `sounddevice` and `pymumble_py3` were
  confirmed absent. `radio_server.backends.kv4p.audio._load_opus()` loaded libopus from the carrier
  (`opuslib_next/_native/libopus.so`, **not** a system lib) and an Opus encode→decode round-tripped
  (3-byte packet → 1920-byte / 960-sample PCM frame).
- `uv sync --extra hardware` in a clean env installed pyserial + sounddevice (+ cffi), no opus/pymumble —
  closure identical to before the split.
- `uv lock` reported no version changes (only the extras were regrouped).
