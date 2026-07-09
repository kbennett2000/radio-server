# 0003 — Session / auth state machine

Status: Accepted

## Context

Every DTMF-driven service must be gated (CLAUDE.md guardrail 4). The transport is RF
in the clear, so any code is overheard and can be replayed within its validity window.
Auth here is therefore *gated* access, not *secure* access: the load-bearing property
is enforced single-use, not secrecy. Cycle 2 builds this layer against `MockRadio`-era
plumbing but with no audio yet — the state machine is fed decoded digit strings so it
is unit-tested in isolation with an injected clock (no real sleeps, no hardware).

This ADR records the state machine, the single-use burn strategy, and clock injection.

## Decision

- **Two states.** `SessionState` is `UNAUTHENTICATED` or `AUTHENTICATED`. Transitions:
  - `UNAUTHENTICATED --valid, unused code--> AUTHENTICATED`
  - `AUTHENTICATED --inactivity > timeout--> UNAUTHENTICATED`
  - Invalid/replayed code in `UNAUTHENTICATED` stays put (rejected).
  A `Session` is a small mutable dataclass (`state`, `last_activity`); the `AuthGate`
  holds no per-caller state, so one gate serves many sessions.

- **`on_dtmf(digits, session, now=None)` is the single entry point.** Order within a
  call: (1) if authenticated and idle past the timeout, drop to `UNAUTHENTICATED`
  *before* stamping — so the current digits become a fresh auth attempt, never a
  command on a stale session; (2) stamp `last_activity = now`; (3) route: unauthenticated
  → `verify_and_burn` → `ACCEPTED`/`REJECTED`; authenticated → dispatch hook → `COMMAND`.
  It returns an `Outcome(kind, detail)`; turning that into CW/voice feedback is a later
  cycle's job.

- **TOTP with `valid_window=1`, verified by hand to learn the matched step.** pyotp's
  `verify` returns only a bool, but single-use burn needs to know *which* time-step a
  code matched. `verify_and_burn` iterates offsets `(-1, 0, +1)`, regenerates the code
  for each step with `totp.at(step * interval)`, and compares with
  `hmac.compare_digest`. Equivalent to `valid_window=1`, but it yields the step index.

- **Single-use burn keyed by `(code, time_step)`.** On the first match the pair is
  added to a `consumed` set and `True` is returned; any later presentation of the same
  code re-derives the same step, finds the key present, and returns `False` — even while
  the code is still inside its window. The set is pruned every call: a code for step `s`
  can only verify while `now_step ∈ {s-1, s, s+1}`, so entries with `step < now_step - 1`
  are dropped. The set stays bounded to the few steps still live (test asserts ≤ 3).

- **Clock injection everywhere.** `Clock = Callable[[], float]` (unix seconds), default
  `time.time`, injected into both the verifier (time-step math) and the gate (inactivity).
  One clock, one source of truth. Tests use a `FakeClock` advanced explicitly — no sleeps.

- **Secret from env, never hardcoded.** `load_totp_secret` reads `RADIO_TOTP_SECRET`
  and raises `RuntimeError` when unset (fail loud, not open/closed by accident). Tests
  inject the secret directly. `TotpVerifier.provisioning_uri` emits the `otpauth://`
  enrollment URI — the exact payload a QR encodes; image/terminal QR rendering is a thin
  optional wrapper deferred to avoid an image dependency this cycle.

- **Command dispatch is stubbed.** The gate takes an injectable `dispatch(digits,
  session)` hook; the default returns a loud "not wired (cycle 3)" marker rather than
  silently pretending to run a command. Cycle 3 replaces it with real service dispatch,
  where auth strength is matched per service (guardrail 4).

## Consequences

- The replay-rejection path has an explicit test at both the verifier and the gate
  (a second caller replaying an overheard code is refused and stays unauthenticated).
- Because time is injected, window edges, expiry, and timeout are deterministic and
  fast — the whole auth suite runs with no real time.
- The gate is transport- and audio-agnostic: the future DTMF-decode cycle only has to
  produce digit strings and feed `on_dtmf`; no auth logic changes.
- `valid_window=1` widens the accept window to ~±30 s (needed for OTA latency) without
  widening the *replay* surface, since every accepted code is immediately burned.
- Single-use state is in-memory per process. A future multi-process/persistence need
  (or a restart mid-window) would require sharing or persisting the `consumed` set;
  out of scope now and noted for when it matters.
