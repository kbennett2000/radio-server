# Server ops notes

Dated notes for the LAN server (`ubuntuserver`, `kb@192.168.1.62`) that hosts the radios and
dongles. Ops-only changes made directly on the box live here — things that are *not* in the repo's
deploy path and would otherwise be lost. Newest first.

> **Read the dates.** Every section below is a record of what was true on the day it was written, and
> older sections are *not* corrected in place — that would destroy the log's value. Where a section
> says "current truth", it means current *as of its own heading*. The block immediately below is the
> only one that claims to describe the box right now.

---

## Current state (2026-07-31)

**This block replaces the 2026-07-26 one below it, which was wrong in three places.** It is the
first entry in this file written from measurements taken on the box rather than from what a cycle
intended — see [ADR 0160](adr/0160-the-bench-answers-back.md).

| unit | port | backend | radio | frequency |
|---|---|---|---|---|
| `radio-server.service` | **8090** | **`baofeng`** | UV-K5 V3 on **F9** fork firmware, tuned by the server over the AIOC (`baofeng.uvk5_tuner = "hybrid"`) | **147.555** |
| `radio-server-kv4p.service` | **8091** | `kv4p` | kv4p SA818 **UHF** (`…CP2102N…`) — the witness | 445.800 |

Both `--user` units, `enabled` with `Linger=yes`, both HTTPS off the self-signed pair in
`/home/kb/applications/radio-server/tls/`.

- **The radio is on F9, flashed by hand by the operator.** *(This block said F8 until ADR 0164. The level was measured in ADR 0161 B1 — `flags=0x03` on `0x087A`, which only a Fusion build with `ENABLE_DOCK_FM_TX_INTERLOCK` answers — and again in ADR 0164, where every ON reply carried `blocks_tx: true`. Note that fork `main` is still F8 (`d086a23`): a build from `main` has `0x0879`/`0x087A` and no TX interlock, so it will key while playing broadcast FM.)* Confirmed over the AIOC on 2026-07-31:
  both `0x0878` (set-modulation, F7+) and `0x087A` (broadcast FM, F8-only) answer, and
  `GET /capabilities` carries `set_modulation` and `clear_broadcast_fm`. The dock session reports
  `F4HWN v5.7.0` — that is the Fusion *base* version and does **not** distinguish F6/F7/F8; the
  opcode answers do. **The "F6" in the block below, and `HANDOFF.md`'s "the radio is not flashed
  with F7", were both written by cycles that never ran on hardware and are false.**
- **The station was found on 147.555, not 445.800.** The block below claims 445.800; `radio.toml`
  `[uvk5] frequency` is `147555000` and `/status` agreed. ADR 0160 moved it to 445.800 for the
  keying stages and restored 147.555 afterwards.
- **D-STAR is NOT disabled.** `/status` reports `dstar.configured: true` with 27 076 RX frames and a
  live REF091 C activity list. The block below says it is disabled; that is stale. The crossband
  re-proof still has never passed — see [dstar-setup.md](dstar-setup.md) before touching it.
- The station runs **Baofeng mode**, not the `uvk5` dock backend — the radio's own firmware sets up
  TX, which is what made repeater work possible (ADR 0138/0139/0142). `[baofeng] uvk5_tune_persist`
  is `true` on this box.
- The witness is **UHF-only**, so nothing on 2 m has ever been verified end to end. A witness aimed
  at a station outside its band reads "no RF" whether or not the radio keyed — **that is not weak
  evidence, it is no evidence.** Any RF-absence test must move both radios into the same band and
  take a **positive control first**.
- **`/status.rssi` is `null` on this backend.** RSSI is a `uvk5`-dock field with no source in
  `aioc_baofeng.py`, so the 2026-07-25 advice below ("`curl /status | jq .rssi` is now the first
  thing to look at") **does not apply to the deployed mode**. There is no host-side signal-strength
  read here at all; measure received signal by audio RMS instead (`scripts/bench/rf_listen.py`).

### Keeping the deployment in step with master

ADR 0160 found the box on branch `transmit-power` @ `4ad0a87` — **12 ADRs behind master**, running
for five days, with none of the host code the cycle was sent to test. Nothing checks this. Before
any bench work:

```sh
cd /home/kb/applications/radio-server \
  && git fetch origin && git status -sb | head -1 && git log --oneline -1 \
  && git log --oneline origin/master -1 \
  && git rev-list --left-right --count HEAD...origin/master   # ahead <TAB> behind
```

**Read the two numbers, not the prose.** On a detached HEAD `git log --oneline -1` prints a commit
with no comparison and `git status -sb` prints `## HEAD (no branch)`, so neither reports drift at
all. `rev-list --left-right --count` prints `ahead<TAB>behind`, and `0<TAB>0` is the only healthy
answer.

> **ADR 0161 certified a guard that only worked in one direction, and ADR 0162 caught it.** The
> previous version of this snippet ended in `git merge-base --is-ancestor HEAD origin/master;
> echo "on-master=$?"`, tested against a checkout deliberately *ahead* of master, where it correctly
> printed `on-master=1`. It was never run against a checkout *behind* master — where it prints
> `on-master=0`, which reads as "we are on master" and is exactly wrong. That is not hypothetical:
> between the two cycles something ran `git reset --hard origin/HEAD`, whose symbolic ref was stale,
> and left the box **six commits behind** master reporting `on-master=0`. A guard tested in one
> direction is a guard tested for the case you happened to be in.

(Building this as an `acceptance.py` stage is still open: ADR 0160 finding 1, unmoved.)

`radio.toml` and `radio-secrets.toml` are untracked and gitignored, so a branch switch cannot clobber
them — verify with `git check-ignore -v radio.toml radio-secrets.toml` rather than assuming.

### The box is currently AHEAD of master on purpose (2026-08-01)

| | |
|---|---|
| deployed commit | **`1ba81c3`**, branch `adr-0165-token-out-of-the-journal` — **on both checkouts** |
| PR | **#222** ([ADR 0165](adr/0165-the-token-stops-appearing-in-the-journal.md)) |
| why | This branch is the only build that keeps the API token out of journald. On `origin/master` every WebSocket connect writes `?token=<the token>` in the clear — measured at **1068 lines in 24 h** on the station and **40** on the witness — and journal excerpts from this box routinely end up in ADRs, PRs and chat, in a **public** repo. |

**There are two checkouts on this box and they are not interchangeable.** `radio-server` (port 8090)
is the station; `radio-server-kv4p` (port 8091) is the measuring witness, with its own `radio.toml`
and its own `radio-secrets.toml`. Before ADR 0165 the witness had been left on `d779aca` for three
cycles and was leaking its own token — deploy **both**, or the second one keeps doing whatever the
first one just stopped doing.

*(The ADR 0163 entry this replaces said `357a487` / PR #220, the ADR 0162 one said `ef5f5c9` / #219,
and the ADR 0161 one said `8679bd5` / #218. All had stopped being true before anyone read them.
Check the two numbers against the box, do not trust this table.)*

**When PR #222 has merged, put BOTH checkouts back on the mainline:**

```sh
cd /home/kb/applications/radio-server \
  && git fetch origin \
  && git switch --detach origin/master \
  && ~/.local/bin/uv sync --extra hardware --extra tts --extra mumble \
  && (cd web && npm ci && npm run build) \
  && systemctl --user restart radio-server
```

```sh
cd /home/kb/applications/radio-server-kv4p \
  && git fetch origin \
  && git switch --detach origin/master \
  && ~/.local/bin/uv sync --extra hardware --extra tts --extra kv4p \
  && (cd web && npm ci && npm run build) \
  && systemctl --user restart radio-server-kv4p
```

**The two `uv sync` lines differ, and copying the first onto the witness breaks it.** `uv sync` is
exact — it uninstalls anything you do not name — and the witness is a kv4p node, so it needs
`--extra kv4p` (which composes the `opus` leaf). Sync it with the station's extras and `opuslib`
disappears; `POST /transmit` then answers **500** with `ModuleNotFoundError: No module named
'opuslib'`, and every RF stage of `acceptance.py` fails at once with no obvious connection to the
cause. ADR 0165 did exactly this and had to undo it. Note the unit's own `ExecStart` says
`--extra hardware --extra tts` with no `kv4p`: the codec survives a restart only because `uv run` is
*not* exact and leaves alone what a previous sync installed. That is worth fixing in the unit.

### Rotating the API token is a two-unit operation (ADR 0165)

`acceptance.py` authenticates to the station and the witness with a single `RADIO_API_TOKEN`, so the
two `radio-secrets.toml` files have always held the same value. `POST /settings/secrets/api-token/rotate`
writes one of them. Rotate, then copy the new value into the other file, then restart **both** units —
`restart_required: true` is not advice. Miss the second unit and every RF stage of `acceptance.py`
fails `401` while the station itself looks perfectly healthy.

Read the value with `grep api_token ~/applications/radio-server/radio-secrets.toml`. Do not paste it
into an ADR, a PR, a commit message or a chat window: that is the leak this whole ADR exists to
close, and re-creating it while reporting the fix would be its own small comedy.

### Never open the AIOC tty while the service is running (ADR 0163)

The port is **not** opened `exclusive`, so the kernel will happily let a second process onto
`/dev/serial/by-id/usb-AIOC_All-In-One-Cable_*-if04`. It does not fail loudly. What happens is:

```
ERROR radio_server.backends.uvk5.transport: uvk5: reader thread stopped on
SerialException('device reports readiness to read but returned no data
(device disconnected or multiple access on port?)')
```

**The reader thread stops and the transport never recovers.** The service keeps running, `/status`
keeps answering with a `broadcast_fm` block and `tx_ok: true`, and every dock read silently returns
nothing from then on. The only visible symptom in this configuration was the broadcast-FM cadence's
`deafened_unknown` climbing while `deafened_age_s` ran past a minute.

`systemctl --user stop radio-server` first, always — and **`systemctl --user restart radio-server` is
the recovery** if it has already happened. This is also why `broadcast_fm_on.py --on` cannot be used
to stage broadcast FM on a running station: reaching that state with the service up needs `F+0` at
the radio's front panel.

`uv` is **not** on the non-interactive `ssh` PATH — it lives at `~/.local/bin/uv`. A plain `uv sync`
over `ssh kb@… '…'` fails with `uv: command not found` and the rest of a `&&` chain silently does not
run, which is how a "deployed" checkout ends up running the previous environment.

### Observing the audio path in a state the boot assert exists to destroy

`AiocBaofeng.__init__` clears broadcast FM at every construction, and since ADR 0161 the key path
clears it again before every over. That is correct, and it makes one question unanswerable from the
deployed unit: *what does the RX pump do with broadcast-FM audio?* The only route to broadcast FM on
a **running** baofeng backend is the radio's own front panel.

The way round it, used for ADR 0161's item B4: stop the unit, set the receiver with
`scripts/bench/broadcast_fm_on.py --on <hz>`, then start a **temporary** radio-server on a spare port
whose config has **no `uvk5_tuner`**. No tuner means no dock traffic and no boot assert, so the
receiver survives into a running server, while the audio path — same AIOC card, same blocksize, same
squelch mode — is the deployed one. It needs `[tts] voice` and `[station] callsign` copied across or
the controller refuses to build.

The answer, for the record: **it relays.** `/audio/rx` carried RMS 3841.2 / 768 000 bytes in 8 s with
the BK1080 on, against **zero bytes** with it off on either side. A deaf station feeds a commercial
broadcast station to the browser and to both bridges.

### A sweep must turn tune persistence off FIRST

`[baofeng] uvk5_tune_persist = true` means **every** `POST /frequency` writes EEPROM and costs the
radio's six-second transmit lockout (`tuned … and stored it` in the journal). ADR 0160 burned 97 such
writes on an airband sweep before noticing. The runtime switch is `POST /tuning/persist {"on": false}`
— note the path, it is **not** `/tune_persist` — and it is runtime-only, so a restart restores the
configured value.

**And "runtime-only" defeats it in the place it is needed most: `acceptance.py` restarts the service
in its own first stage.** ADR 0161 turned persistence off, verified it (`(not stored — instant)` in
the journal), ran the suite, and still counted **seven** EEPROM writes — the `systemd` stage's
restart had silently restored the configured `true` before any stage tuned. To run the suite without
flash wear you have to edit `radio.toml` and restart, not use the runtime switch. Count the writes
afterwards rather than assuming:

```sh
journalctl --user -u radio-server --since "-90 min" --no-pager | grep -c "and stored it"
```

---

## 2026-07-25 (later) — the station moved to 2 m, and three things broke (ADR 0132)

**The operating frequency is now 147.555** (2 m simplex). `radio.toml` `[uvk5] frequency` changed
**445800000 → 147555000**; backup at `radio.toml.pre-0132-backup`. A **`2m Simplex 147.555`**
preset was added ahead of the two bench ones — and the **445.800 preset must stay**, because it is
the only frequency the kv4p can hear and `acceptance.py` uses it to put both radios in the same
band before the RF stages.

### What the bench proved

Register read-back over the dock, keyed, before and after the fix
([ADR 0132](adr/0132-dock-band-and-the-register-model.md)):

| on 147.555 | before | after |
|---|---|---|
| keyed PA gain byte (`0x36`) | `0xA2` UHF ✘ | **`0x88` VHF ✔** |
| keyed LNA path (`0x33`) | UHF ✘ | **VHF ✔** |
| LNA after un-key | UHF ✘ | **VHF ✔** |
| synth while keyed | 147 555 000 ✔ | 147 555 000 ✔ |

445.800 re-measured unchanged. **Receive over real RF on 2 m, measured on a live handheld key-up:**
`rssi 267` against a floor of 150 (**+117**), **squelch OPEN**, held ~12 s.

### The one that will bite again if forgotten

A radio left at **`reg 0x30 = 0`** has its receiver switched off — RSSI `157 → 0` — and that is
exactly where a *lost un-key* leaves it. The backend used to seed its RX word from a bare read at
connect and write that value back on every un-key and every retune, so it inherited the damage and
kept it for the life of the process: `rssi 0`, `busy false`, **zero bytes of audio**, indefinitely,
with every API call returning 200. Restarting only helped when the radio happened to be healthy at
that moment, which is why it looked intermittent.

Connecting now repairs it and says so:

```
WARNING uvk5: reg 0x30 read back 0x0000 at connect, which is not a receiving state
        (everything disabled). Seeding the stock RX word 0xbff1 and writing it, ...
```

**If that warning appears in the journal, the radio was found broken and was fixed** — worth
knowing, not worth alarm. If RX is ever dead again, `curl /status | jq .rssi` is now the first
thing to look at: `0` means the receiver is off, not that the channel is quiet.

### Measuring on a band the kv4p cannot hear

The kv4p is a single-module SA818-**UHF** board (400–480 MHz), and it is the witness every RF stage
in `acceptance.py` depends on. On 2 m there is no witness on this box at all.

```sh
# what a running station hears, any band — reports each signal live, so leave it open and key
RADIO_API_TOKEN=<token> .venv/bin/python scripts/bench/rf_listen.py --seconds 1200 --tone 1000

# the register-level TX proof, any band — needs the service STOPPED
systemctl --user stop radio-server
.venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit --frequency 147555000
.venv/bin/python scripts/bench/uvk5_tx_regs.py --tune-loop 25 --frequency 147555000  # no TX
systemctl --user start radio-server
```

`acceptance.py`'s RF stages now **SKIP with the reason** when the two radios are not on the same
channel (and a skipped run still exits non-zero). Before that they reported zero bytes / zero duty
/ zero tone — indistinguishable from a broken receiver, and it sent this session bisecting a
regression that did not exist.

**Idle RSSI floors, per band, off a running station: 107 on 445.800, 154–156 on 147.555.** Both
under the configured `squelch_threshold = 220`, and a real 2 m signal reads 267 — so one scalar
covers both bands and no per-band setting was added.

---

## 2026-07-25 — Stabilization: the bench is self-testing now (ADRs 0127/0128/0129)

**Current truth for this box.** Two `--user` units, both `enabled` with `Linger=yes` (so they start
at boot without a login), both HTTPS with the self-signed pair in
`/home/kb/applications/radio-server/tls/`:

| unit | port | backend | radio | frequency |
|---|---|---|---|---|
| `radio-server.service` | **8090** | `uvk5` | UV-K5 V3 over the AIOC dock (`AIOC_K6` card, `…AIOC…-if04`) | 445.800 |
| `radio-server-kv4p.service` | **8091** | `kv4p` | kv4p SA818 UHF (`…CP2102N…`) | 445.800 |

**Auth has two planes** (they are not interchangeable, and this trips people up):
REST wants `Authorization: Bearer <token>`; WebSockets want `?token=<token>` in the query string
(browsers cannot set handshake headers). Both read `api_token` from
`radio-server/radio-secrets.toml`. `totp_secret` **is** present on this box — the RF login path is
fully testable here, unlike the dev PC.

### Ops changes made directly on the box

- **Both unit files gained `SuccessExitStatus=143`.** A clean SIGTERM stop legitimately exits 143;
  without the declaration `systemctl stop` reported `Failed with result 'exit-code'`. The real
  20 s-then-SIGKILL hang is fixed in code (ADR 0127), not hidden here. `systemctl --user
  daemon-reload` after editing.
- **`radio.toml` gained two `[[presets]]`** ("Bench Simplex 445.800", "Bench Alt 446.000"). There
  were none, so `GET /presets` returned `[]` and the browser's Channels card was empty. The second
  entry exists so "apply a preset and watch the radio follow" is an observable change. A pre-change
  copy is at `radio.toml.pre-mission-backup`.
- **`radio.toml` gained a `"Bench Split"` preset** (RX 445.800 / TX 446.400 / `tx_tone = 100.0`) so
  the acceptance runner's `split` stage has something to prove itself on. It is **synthetic** — a
  bench frequency pair into a dummy load, deliberately not a real repeater. Keying a repeater's real
  uplink is the operator's business, never the runner's (ADR 0133). The stage skips loudly if the
  preset is missing rather than inventing one.
- **The operator's 37 CHIRP repeaters were imported** into `[[presets]]` via
  `python -m radio_server.chirp` (ADR 0133). A backup is written next to the file as
  `radio.toml.bak` on every import. Re-running the import is byte-identical, so it is safe to repeat.
  **Applying one of these arms the transmitter on a real repeater's input** — that is the point, but
  it means leaving the station parked on a bench simplex preset after any test session.
- **The `dtmf.py` stash was dropped** (`git stash drop`). It hardcoded
  `NATIVE_REVERSE_TWIST_DB 4.0 → 10.0`; `radio.toml` already sets `audio.dtmf_reverse_twist_db =
  10.0`, which is the supported way to do the same thing, and the hardcode would have broken
  `tests/test_native_dtmf.py:335`. The setting itself is **needed** — measured, see ADR 0129.
- `py-spy` installed via `uv tool install py-spy` (at `~/.local/bin/py-spy`) for the shutdown
  diagnosis. Attaching needs `sudo`.

### How to re-prove the station

```sh
cd /home/kb/applications/radio-server
RADIO_API_TOKEN=<token> .venv/bin/python scripts/bench/acceptance.py
```

Eight stages, no human, exit 0 only if all pass — it keys both radios itself. Run it after any
deploy touching audio, keying, or the controller. `--only <stage>` to narrow;
`--list` for the names. It **replaces the `/tmp` scripts**, which did not survive reboots.

**Last verified 2026-07-25: three consecutive runs PASS (exit 0, 0, 0), the first immediately after
a cold reboot.** A run takes a few minutes, most of it spoken announcements playing out in real
time. The `(xruns anywhere in run)` line is **information, not a verdict** — it includes the
restart the runner performs itself and the end of every keyed over, where nobody is reading the
capture ring by design. The verdict is `ALSA xruns while receiving`.

If the `tx` stage fails, escalate to the register-level check (needs the service stopped, and
always start it again):

```sh
systemctl --user stop radio-server
.venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit
systemctl --user start radio-server
```

### Standing facts worth not re-deriving

- **The UV-K5's dock firmware is flashed and working.** *(F5 when this was written; the radio has
  since moved to **F6**, which is what `setvfo`/`hybrid` tuning requires — ADR 0144.)* Keyed read-back shows `0x33 = 0x9028`
  (PA_ENABLE set) — radio-server never writes that bit, so the firmware is doing it. Chain B is
  closed; no flash is pending.
- **Dock TX radiates at PA bias 12** (`0x36 = 0x0CA2`). That is a low level. The lever for more is
  the radio's own OUTPUT_POWER setting, not a host register write (ADR 0128).
- Stopping the service is now cheap and clean (~0.5 s idle, ~5.5 s with WebSocket clients
  attached). There is no longer any reason to leave it stopped.

---

## 2026-07-24 — F4 Chain B (TX): software keys, physical TX doesn't — a firmware PA gap

Follow-up to the RX triage below. The F4 report included "browser TX not working" / "services not
announcing." **The software TX path works; the physical transmitter does not, because dock-mode
keying never engages the PA / antenna switch. Firmware fix, not radio-server code.**

### The HT was the unreliable instrument; the kv4p is the objective one

Every "no tone / no key-up" observation came through Kris's HT (which also produced the contradictory
RX captures). The bench's **kv4p (UHF SA818, service on 8091)** has a hardware carrier-detect:
`status().busy` is the SQ/COS pin (`backends/kv4p/radio.py:562`). The failing tests were on 147.555
(VHF); the kv4p is **UHF-only**, so `radio.toml` was reverted to **445.800** (the operating freq),
putting the UV-K5 in the kv4p's band, inches away.

### What the measurements showed

- **Software keys reliably:** browser PTT → `tx_key_up` (5.64 s); `_key_on`'s CONFIRM readback
  (`reg 0x30`→keyed, ADR 0112) passes. The backend keying is correct — NOT the fault.
- **Chip TX engages:** the *dummy-load* run's kv4p saw a modulated carrier (RMS to 7427) — low-level
  BK4819 RF, detectable in near-field only.
- **PA does NOT engage:** the *antenna* run, on the same confirmed **5.7 s** key
  (`/tmp/dual_tx_watch.py`: UV-K5 `transmitting=True`, kv4p carrier **False** throughout, 9 polls
  keyed-without-RF / 0 with) → no usable radiated power.
- **RX still works** (Kris hears himself in headphones with the antenna on) → the radio *is* in dock
  mode; the dark path is specifically the TX PA.

**Diagnosis:** the TX mirror of ADR 0120. Dock mode drives the BK4819 TX register but not the
MCU-side PA-enable + TX/RX antenna-switch GPIOs. RX got a firmware force-open
(`Dock_ForceRxAudioAlive`); TX has no equivalent. **Fix = a firmware `Dock_ForceTx`** in
`kbennett2000/uv-k1-k5v3-firmware-custom`. Radio-server's keying is proven correct.

### Reusable RF-loopback recipe

Verify UV-K5 TX in software without an HT, both radios on a **UHF** freq (kv4p band):
1. Set the UV-K5 to a UHF freq (`radio.toml` or `POST /frequency`); tune the kv4p to the same freq.
2. `scripts/bench/kv4p_carrier_watch.py <sec>` — kv4p `status().busy` = carrier detect (does RF radiate?).
3. `scripts/bench/kv4p_audio_probe.py <sec>` — kv4p `/audio/rx` per-window RMS + dominant Hz (is it modulated?).
4. `scripts/bench/dual_tx_watch.py <sec>` — correlates UV-K5 `transmitting` vs kv4p carrier (keyed WITH vs
   WITHOUT RF). Key via `doctor --tx-tone` (TTY CONFIRM) or browser PTT.

**Clean confirmation for the firmware cycle:** put the dummy load back and re-run — if the carrier
reappears with the dummy load but not the antenna, that pins it to near-field-only chip RF (no PA).
An RF power meter / SWR bridge on the antenna line settles it outright.

### Carry-forward for the firmware cycle (not radio-server code)

- **Key-test first-attempt flake:** the first `--key-test` after construction refused
  (`reg 0x30=0xbff1`, want `0xc1fe`); the retry passed (keyed 99 ms). A first-key settle race, likely
  the same GPIO-timing area as the PA-enable gap.
- VHF (147.555) TX behaviour is untested with an objective receiver (kv4p is UHF); the PA gap is
  expected to be band-independent, but unconfirmed on VHF.

### Frequency restored

`radio.toml` `[uvk5] frequency` reverted **147555000 → 445800000** — the F4 VHF pin is gone; the
deployment is back on its operating frequency. (The `radio.toml.bak-f4` backup can be removed.)

---

## 2026-07-24 — F4 RX triage: clipping knob, frequency pin, a doctor false-positive

Bench session that produced [ADR 0125](adr/0125-uvk5-v3-rx-pump-cat-gate-decouple.md) (the pump
CAT-gate decouple, a code fix, shipped as a PR). These are the **ops** findings from the same
session — level/config, not code.

### RX audio was clipping — the volume knob, not the host mixer

`doctor --rx-level` on idle noise read **peak 32752 / 32767 (−0.0 dBFS)** — the receiver audio was
slamming clip. The host capture mixer (`AIOC Audio In`) is already at 100 % / 0.00 dB (ADR 0124), so
the **radio's volume knob** is the only remaining level lever, and it had been turned too far up.
Clipping matters for DTMF: the harmonics a clipped waveform generates trip the native decoder's
2nd-harmonic / dominance gates, so `1234#` would not decode even though a carrier was plainly present.

**Fix (on the box):** turned the knob down to **peak 28032 (−1.4 dBFS)**, average ~8800 RMS. At that
level `doctor --dtmf` decoded **`1234#1234#`** cleanly — two perfect entries. **Leave the knob near
here** (idle-noise peak in the high-20k's, not pinned at 32767). If DTMF ever stops decoding, check
this first with `doctor --rx-level` before touching anything else.

Note this is only *half* of why DTMF failed on the server — the other half was the RX-pump
starvation fixed in code by ADR 0125. Both had to clear: the knob so `doctor` (which bypasses the
pump) decodes, the pump fix so the **live server** decodes.

### `radio.toml` frequency pinned to 147.555 MHz (was 445.800)

For the F4 bench work `[uvk5] frequency` was changed **445800000 → 147555000** to match the HTs on
the bench (both were on 147.550, then 147.555). Backup of the pre-change file is at
`~/applications/radio-server/radio.toml.bak-f4`.

**This is a bench setting, not the operating frequency.** When bench testing is done, restore the
intended operating frequency (445.800 or whatever the deployment should sit on) — the backend
re-applies `radio.toml`'s frequency on every construction, so the live value follows this key. The
web UI can also set it at runtime (`POST /frequency`), which is what the operating log showed during
this session.

### A `doctor --rx-capture` VERDICT(2) false-positive (filed, not fixed)

On a **pure-noise** capture (no DTMF present), `--rx-capture` emitted
`VERDICT (2): DTMF tones present but OFF-FREQUENCY … nudge kv4p.sample_rate_correction by ~×1.0236`
while every individual analysis window said "(no clean pair)". Two problems: it claimed tones on
noise, and it named **`kv4p.sample_rate_correction`** on a **uvk5** run (wrong backend's config key).
Minor doctor defect — the verdict logic over-reaches on noise and the remediation hint is
backend-blind. Worth a future doctor cycle; harmless (advice only), so not fixed here.

**Symptom.** `doctor --rx-noise` failed with `No input device matching 'AIOC_K6'` while the connect
probe passed ALL. The working theory was that the udev rule naming the card had not applied.

**It had.** The card was already renamed:

```
/sys/class/sound/card2/id -> AIOC_K6
/proc/asound/cards:  2 [AIOC_K6        ]: USB-Audio - All-In-One-Cable
```

The rules file had simply been written *after* the reboot it was tested against (boot ≈20:39, file
mtime 20:41), so the first look at it was stale.

**Actual root cause — a layer mismatch, not a udev fault.** A USB sound card has three names and
udev only owns the first:

| Layer | Value | Set by |
|---|---|---|
| ALSA card **id** | `AIOC_K6` | udev `ATTR{id}` |
| ALSA card **name** | `All-In-One-Cable` | USB product string — **not** settable by udev |
| PortAudio device name | `All-In-One-Cable: USB Audio (hw:2,0)` | derived from the card *name* |

`sounddevice` matches a string device against **PortAudio names**, which come from the card *name*,
never the card *id*. So `input_device = "AIOC_K6"` could never resolve — **no udev rule can fix
this**, because `ATTR{id}` is the only naming lever udev has and PortAudio does not read it. The
error text comes from the sounddevice library itself, not radio-server.

**Fix:** radio-server now resolves an ALSA card id to a PortAudio index itself
(`resolve_device` in `radio_server/backends/soundcard.py`) — see [ADR 0124](adr/0124-aioc-alsa-card-id-device-resolution.md).
`radio.toml` stays as-is; the code conforms to it.

### Final working rule — `/etc/udev/rules.d/85-aioc-names.rules`

```
# AIOC USB sound cards — stable per-cable ALSA card ids, keyed on the USB serial.
#
# Keyed on ATTRS{serial} (never card index / plug order) so a second AIOC cannot steal a name.
# KERNEL=="card*" scopes the match to the card node — ATTR{id} exists only there; without it
# udev attempts the write on every sound sub-device (controlC*, pcmC*D*).
#
# NOTE: this sets the ALSA card *id* (the aplay -l bracket field, and hw:CARD=AIOC_K6). It does
# NOT change the card *name* ("All-In-One-Cable", from the USB product string), which is what
# PortAudio/sounddevice reports. radio-server resolves the id itself — see ADR 0124.

SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{serial}=="da3441ac", ATTR{id}="AIOC_K6"

# AIOC #2 (UV-5R) — arriving 2026-07-25. Find its serial with:
#   ls -l /dev/serial/by-id/     ->  usb-AIOC_All-In-One-Cable_<SERIAL>-if04
# then paste <SERIAL> below and uncomment the line.
# SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{serial}=="<uv5r-aioc-serial>", ATTR{id}="AIOC_UV5R"
```

Apply with `sudo udevadm control --reload && sudo udevadm trigger -s sound`. Verify the rule
actually matches with `sudo udevadm test /sys/class/sound/card2` (look for the
`ATTR{id}="AIOC_K6"` line) — `aplay -l` alone won't tell you *why* a name stuck.

**Saturday (AIOC #2):** paste the new serial into the commented line, uncomment, reload+trigger,
then set that radio's config block to `input_device = "AIOC_UV5R"` / `output_device = "AIOC_UV5R"`.
No index chasing — both cables share the PortAudio name `All-In-One-Cable`, so the card id is the
only stable discriminator.

### Bench numbers (2026-07-24)

- `doctor --rx-noise` with `radio.toml`'s `AIOC_K6`, post-fix: **peak 5299 RMS (-15.8 dBFS),
  average 4123** — RX ALIVE.
- **A bare `arecord -D hw:CARD=AIOC_K6,DEV=0` reads floor (67 RMS) and that is *not* a fault.**
  The UV-K5 dock only un-mutes its receiver when radio-server runs the enter-HW-mode sequence
  (`REG_47`→FM, ADR 0120/0122). A raw ALSA capture skips that, so it always looks dead. Use
  `doctor --rx-noise` to test RX on this radio, never `arecord`.

### Two cosmetic warts noticed, deliberately not changed

- `systemctl --user stop radio-server` leaves the unit in state **`failed`** (`status=143` = SIGTERM).
  The app shuts down cleanly ("Application shutdown complete"); systemd flags it only because the
  unit lacks `SuccessExitStatus=143`. Adding that line to
  `~/.config/systemd/user/radio-server.service` would silence it.
- The deployed checkout `/home/kb/applications/radio-server` carries an **uncommitted local edit** to
  `radio_server/audio/dtmf.py`. Deploy drift — worth reconciling or reverting before the next pull.

### Operational reminders

- The AIOC sound card is **single-open**: stop `radio-server` before any capture test and start it
  straight after. There are three checkouts under `~/applications/` (`radio-server`,
  `-kv4p`, `-dstar`); the user service runs the plain `radio-server` one.
- To test an unmerged change without disturbing the deployed tree: `cp -a` the checkout to `/tmp`,
  overlay the changed files, and run with
  `cd /tmp/<copy> && uv run --project ~/applications/radio-server ... python -m radio_server.doctor`
  — cwd wins on `sys.path` and `radio.toml` is discovered from cwd, so the scratch tree's code and
  config are used against the deployed venv.
