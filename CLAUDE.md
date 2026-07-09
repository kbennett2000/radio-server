# CLAUDE.md

<!-- Everything above the PROJECT CONTEXT marker is inherited from project-template.
     Do not edit per-project. Project-specific content is appended below the marker
     by the factory generator from the new-project issue. -->

## How work runs here

- Work is executed one cycle at a time by a headless `claude -p` run — no persistent session, and no human watching the run.
- Each cycle starts fresh. Current state lives in `HANDOFF.md`, the ADRs under `docs/adr/`, and this file — not in remembered conversation. Read them at the start of every cycle.
- End each cycle by updating `HANDOFF.md` so the next cycle can pick up cleanly.

## The cycle contract

**Never pause or wait for a human.** No one is watching the terminal. You must never end by printing a question and stopping — a question that isn't recorded on the issue is lost. Every cycle ends in exactly one of the two terminal states below, then exits.

**Do the work. Don't ask permission.** When files change, you ALWAYS — without asking, every time:
1. Work on a branch, never `master`/`main`.
2. Commit and push.
3. Open a PR for human review/merge.

Committing, pushing, and opening a PR are never optional and never require confirmation. A human reviews and merges the PR; you do not close the issue.

**Decide, don't stall.** If something is uncertain but you can proceed, make the reasonable choice and note it in the PR description. "Should I also do X?" is not a blocker — do the obvious thing or note it and move on. Non-blocking uncertainty never stops a cycle.

**Stopping early is rare and only for true blockers.** Stop only when you are missing information you genuinely cannot proceed without. Stopping means: record the blocker on the issue (the `needs-input` state below) and exit. This is recording, not asking — you never wait for a reply. A destructive or unwalkbackable action (force push, history rewrite, deleting branches/data) counts as a blocker: do not do it; record it and stop.

## End of cycle — always update the issue

You are given the instruction issue number for this cycle (e.g. #1). Before you exit, run exactly one case:

- **Completed** (files changed, PR opened):
  - `gh issue comment <N> --body "PR: <pr-url>"`
  - `gh issue edit <N> --add-label cycle-summary --remove-label instructions`
- **Blocked** (missing info you cannot proceed without):
  - `gh issue comment <N> --body "<the blocker, stated clearly>"`
  - `gh issue edit <N> --add-label needs-input --remove-label instructions`

Every cycle ends in one of these two states, then stops. Never close the issue.

## Conventions

- ADR-first: significant decisions get an ADR in `docs/adr/` before implementation.
- Keep changes small and reviewable.

<!-- ===== PROJECT CONTEXT (appended per repo — do not add content above this line) ===== -->

## Project context

### What this is

radio-server controls a ham radio over one HTTP/WebSocket API on a LAN and exposes DTMF-authenticated voice services (e.g. announce the time). Two radios, two operating modes, one shared API:

- **TM-V71A mode** — full control. Audio + PTT via a SignaLink USB on the radio's DATA jack (6-pin mini-DIN); frequency/channel/tone/mode via CAT (Hamlib rigctld, TM-D710 backend) on the PC/COM jack (8-pin mini-DIN).
- **Baofeng mode** — TX/RX only. Audio + PTT via an NA6D AIOC cable on a UV-5R. No CAT: frequency is set by hand on the radio.

### Core architecture: one `Radio` protocol, swappable backends

Shared surface, both backends implement: `transmit(audio)`, `receive()`, `ptt(on)`, `status()`.
TM-V71A-only (CAT): `set_frequency`, `set_channel`, `set_tone`, `set_mode`, `scan`.

Everything above the radio layer — DTMF decode, TOTP auth, sessions, service dispatch, TTS — operates on sound-card audio and is backend-agnostic. It calls only `receive()` and `transmit()`, so every service works identically in both modes.

The PTT mechanism is the ONLY real divergence between backends:
- SignaLink (V71): audio-triggered. `transmit()` plays audio; the box self-keys PTT off it.
- AIOC (Baofeng): explicit. `transmit()` asserts the serial RTS line (pyserial), plays audio, drops RTS.

### Build strategy: software-first behind a mock backend

Hardware is in transit. Build and unit-test the entire stack against a `MockRadio` that implements the `Radio` protocol (records TX audio, serves canned RX, fakes `status()`/busy). The real backends (`SignaLinkV71`, `AiocBaofeng`) are the LAST phase, brought up with hardware in hand. No feature should require real hardware to be testable.

### Stack

Python, FastAPI (REST + WebSocket), pytest, uv for packaging. External tools: Hamlib `rigctld`/`rigctl` for CAT (shell out or bindings); `multimon-ng -a DTMF` for DTMF decode; `pyotp` for TOTP; sounddevice/ALSA for audio I/O; a local TTS engine (piper is a good CPU-friendly default). `710.sh` (AG7GN/kenwood) is the reference implementation for TM-V71A CAT.

### Test command

`uv run pytest`

### Suggested layout (evolve via ADR)

`radio_server/{backends, audio, auth, services, scan, api}/` plus `tests/` and `docs/adr/`.

### Project-specific guardrails

1. **Verify hardware facts empirically — never assert them from memory.** The exact Hamlib rig model number, `rigctl` serial speed, `multimon-ng` flags, and the AIOC's default PTT line (RTS vs DTR) are judge-on-the-chip facts. Keep them as config with a marked default and a "verify against hardware" note; do not hardcode a guessed value as confirmed.

2. **The one wiring rule, encoded in the design:** PTT is keyed via the DATA port (SignaLink) or the AIOC serial line — NEVER via a CAT `TX` command. Keying over CAT transmits the radio's mic audio and ignores app audio. CAT is for tuning only. The V71 backend must not expose a CAT-keyed TX path.

3. **Capability split at the API.** In Baofeng mode the CAT methods do not exist. Expose a `capabilities()` call; the API returns a clear "unsupported in this mode" (or hides the endpoint) rather than silently no-op'ing. The web UI greys out tuning controls in Baofeng mode.

4. **Auth is gated access, not secure access.** Everything is in the clear over RF. TOTP still helps: validate with `valid_window=1` (~±30s for over-the-air latency) and ENFORCE single-use — burn a consumed code so it cannot be replayed inside its window. Keep sessions short (inactivity timeout). Match auth strength to what a service can do; guard anything that keys TX harder than "announce the time."

5. **Part 97:** every transmission the server makes is the licensee's station. Build automatic station ID (CW or voice) on a <=10-minute interval and at session end. This is required controller behavior, not an optional feature.

6. **Cycles:** ADR-first. Smallest reviewable, load-bearing unit per cycle. Software/mock cycles first; hardware bring-up is a separate empirical phase whose acceptance is "plug it in, it keys up clean."
