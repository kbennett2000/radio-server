# 0066 — kv4p HT: connect on a running board, re-founded on shipped firmware

Status: Accepted

## Context

`Kv4pTransport.connect()` timed out against a running board. Two prior cycles diagnosed it as a
**sequence gate** (ADR 0062) and then **edge-triggered status reports** (ADR 0064). Both were inherited
from the wrong firmware pin — `e9935bd…`, an **unreleased** commit +44 ahead of the shipped release. This
cycle re-derives the behaviour from the **shipped** source (v2.0.0.1, `3f0e809…`), read verbatim, and
finds the old model is fiction and the old probe is a data-loss bug.

### What shipped firmware actually does (read verbatim this cycle)

```c
void handleCommands(RcvCommand command, uint8_t *params, size_t param_len) {
  switch (command) {
    case COMMAND_HOST_DESIRED_STATE:
      if (param_len == sizeof(HostDesiredState)) {   // the ONLY gate; a wrong length is dropped silently
        memcpy(&desiredState, params, sizeof(HostDesiredState));   // whole-struct overwrite
        reconcileDesiredState();
      }
      break;
  }
}
```

- **No sessions, no sequence gate, no flag mask.** `ProtocolSession`, `HOST_STATE_SESSION_FLAG_MASK`,
  and the "lower sequence is silently ignored" hazard are all `e9935bd`-only.
- `reconcileDesiredState()` applies config to `appliedState` **only if `RADIO_CONFIG_VALID`**, then calls
  `savePersistedRadioStateIfChanged()` **unconditionally**, then `markDeviceStateDirty()`.
- `deviceStateFlags()` returns the **whole** `desiredState.flags` word (`| phys_ptt|tx_active|squelched`);
  `currentDeviceState()` reports **`appliedState`** freq/bw/ctcss and `appliedSequence = desiredState.sequence`.
- `sendCurrentDeviceState()` / `deviceStateLoop()` early-return unless `ENABLE_STATUS_REPORTS` is set;
  `deviceStateLoop` sends **on-dirty and periodically** (`EVERY_N_MILLISECONDS(DEVICE_STATE_REPORT_INTERVAL_MS)`).
- `loadPersistedRadioState()` seeds `desiredState` from NVS at boot with the operator's freq/ctcss/bw/memory
  **and** the persistable flag bits (`HIGH_POWER`/`RSSI_ENABLED`/`FILTER_*`/`TX_ALLOWED`); reports start **off**.

### Consequence 1 — the real cause is a dropped probe, not edge-triggering

Because reports fire periodically *and* on-dirty, and `deviceStateFlags()` echoes `ENABLE_STATUS_REPORTS`,
a single 22-byte probe should already elicit a report on a running board. When it doesn't, the frame is
simply **not landing** — the `param_len == 22` gate fails silently (a probe lost to a reset-on-open race
or a dropped write draws no error). The fix is to **retransmit** the probe, not to invent new report
semantics.

### Consequence 2 — a permanent data-loss bug (confirmed)

Any host frame memcpy's over `desiredState` and `savePersistedRadioStateIfChanged()` then persists it
**unconditionally**. The old `connect()` (and `close()`) sent a **neutral zeros** state — so every probe
and every shutdown **permanently wrote freq `0.0` and `tx_allowed=false` to NVS**, zeroing the operator's
stored frequency and closing the TX gate. `doctor` called this "read-only." It was not.

## Decision

### 1. Passive-first, retransmitting, config-preserving `connect()`

1. **Passive.** Listen (no write) for `DEFAULT_PASSIVE_WINDOW` (a marked default that must exceed
   `DEVICE_STATE_REPORT_INTERVAL_MS`; verify-on-source/bench — guardrail 1). A board already streaming
   reports (a server reconnect, or the app attached) is fully visible: **sync the sequence counter to the
   reported `appliedSequence` and return — zero writes.**
2. **Elicit.** Otherwise send an elicit `HostDesiredState` (`ENABLE_STATUS_REPORTS` on, `RADIO_CONFIG_VALID`
   **off** so it never retunes and `appliedState` keeps the real freq), **retransmitting** every
   `_ELICIT_RETRANSMIT_INTERVAL` until the device echoes the flag or `timeout`.
3. **Restore.** Rewrite the tuning the elicit read back (`freq/ctcss/bw/memory`, sourced from the device's
   reported `appliedState`) with safe flag defaults — `RADIO_CONFIG_VALID | HIGH_POWER | RSSI_ENABLED`
   (the firmware's own boot defaults), **`TX_ALLOWED` left CLEARED** and filters cleared — undoing the
   elicit's zero-clobber of the stored frequency.

`_session_acknowledged()` stays the success test: shipped `deviceStateFlags()` copies the whole flags
word, so a state carrying `ENABLE_STATUS_REPORTS` proves a host frame applied; a boot HELLO's embedded
state (flag clear) never completes the handshake.

### 2. Neither `close()` nor the backend's first reconcile clobbers NVS

The close-time PTT-off reconcile echoes the device's **last known state** with `PTT_REQUESTED` cleared and
the device-only bits stripped (`_ptt_off_echo`), reproducing the device's own desired state minus PTT — so
`persistedRadioStateMatchesDesired()` holds and no zeros are persisted. If no state was ever seen, nothing
was keyed and close sends nothing.

The **`Kv4pHt` backend's initial reconcile** had the same flaw: it sent `freq_rx = 0.0` with
`RADIO_CONFIG_VALID` off (so it never retuned — but shipped persists unconditionally, zeroing the stored
frequency). It now **seeds the desired-state model's tuning from the `DeviceState` `connect()` returned**,
so the first reconcile carries the board's real frequency until the server sets its own via
`set_frequency()`. This is what makes `doctor --backend kv4p --rx-level` (which builds the backend)
non-destructive, not just the bare connect probe.

### 3. The model is re-founded on shipped behaviour

`_session_flags` (a masked-per-frame "session" model) → `_link_flags`: flags kept asserted for the life of
the connection because shipped memcpy's the whole word, so dropping `ENABLE_STATUS_REPORTS` on a later
frame would turn reports *off*. The transport docstring and ADR 0062 Decision 1 are corrected: no sequence
gate, no mask, whole-struct memcpy, unconditional persist, periodic+dirty reports. `frames.py`'s
`HOST_STATE_*_MASK` constants are re-labelled a **host-side grouping**, not a firmware-enforced mask.

## Consequences

- **`connect()` succeeds on a not-just-reset board** without clobbering its stored config, and a board
  already reporting is touched zero times. `doctor --backend kv4p --rx-level` can finally report live RX.
- **Firmware limitation, recorded not worked around.** Shipped firmware exposes **no read-before-write**:
  any host frame overwrites `desiredState` wholesale, and the operator's persistable *flag* bits
  (`TX_ALLOWED`/`HIGH_POWER`/filters) are never reported before they're overwritten. So on a **reports-off**
  board the *tuning* is recoverable (from `appliedState` in the reply) but those *flag bits* are not —
  `connect()` restores the frequency and applies safe flag defaults (TX stays off). There is also a
  sub-second window during the elicit where NVS holds zeros before the restore lands; a crash there leaves
  the stored frequency zeroed (the operator re-tunes to recover). Both are firmware limitations, not
  choices we can engineer away without firmware support.
- **`doctor`'s wording is now true:** "does not key; preserves the board's tuned frequency/CTCSS; resets
  TX-allow/filter flags to safe defaults." The `--rx-level` audio read remains genuinely read-only.
- **Tests.** `FirmwareFakeSerial` is re-founded on shipped acceptance (whole-struct memcpy, whole-flags
  echo, conditional retune, unconditional persist) with a modeled `persisted` view; new regressions cover
  the passive zero-write path, the elicit-then-restore that preserves the stored frequency (and leaves
  `TX_ALLOWED` safely off), and `close()` not clobbering NVS.
- **Deferred (next cycles, unchanged):** the extras taxonomy (a kv4p-only libopus extra so a node needs no
  `--extra mumble`) and the user docs.
