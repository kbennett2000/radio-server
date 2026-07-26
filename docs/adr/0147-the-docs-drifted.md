# 0147 — The docs drifted, and nothing was watching

Status: Accepted

Extends ADR [0058](0058-posix-install-script.md)'s docs↔code contract test to the rest of what is
mechanically checkable. Audits the documentation against ADRs 0142–0146 and 0086–0109.

## Context

The operator asked for a full docs audit. What came back was not a list of typos.

**Four documents still told the operator to set the frequency by hand on a Baofeng** — the exact
capability ADRs 0142–0146 were built to deliver, over the exact cable the docs said could not carry
it. `docs/install.md` made the claim about *the cable*, which is the part that was never true:

> There is no tuning control over this cable — you still set the frequency **by hand on the radio**.

`docs/hardware-bringup.md` went further and made three specific assertions — "advertises only the
shared caps", "the API returns 501 for any tuning call", "the web UI greys out the tuning controls" —
each individually false whenever `baofeng.uvk5_tuner` is set.

**18 ADRs (0097–0114) had no row in the ADR index**, in a project whose first convention is ADR-first.
The table jumped from 0081 straight to 0115. Other rows in that same table cite those numbers, and so
does `architecture.md` — a reader following the pointer found nothing. ADR 0139's row was
**structurally malformed**, missing its Status cell entirely.

**`deployment.md` told a deployer to proxy three WebSockets**, naming them. The app registers seven.
An nginx config built from that list leaves the browser's Mumble and D-STAR audio silently dead while
the rest of the UI looks fine.

And `using-it.md` promised *"you're never left with a dead station"* about a backend switch that
**SIGSEGVs the process** — recorded as open in ADRs 0140, 0141, 0142 and 0145, and reachable from the
web UI.

### The cause is structural, not carelessness

Every one of these had been true for many cycles, written by people who knew better at the time. The
mechanism is simple: **docs are updated by hand at the end of a cycle, and nothing fails when they
aren't.**

The two places this project already wired a lock did **not** drift — README ↔ `install.sh`
(`test_docs_install_command.py`, ADR 0058) and `radio.toml.example` ↔ `spec.py`
(`test_config.py`). That is the whole finding. The fix is not "be more careful."

## Decision

**Lock what a test can check; say plainly that it is not everything.**

`tests/test_docs_contract.py` adds seven checks: every ADR file has exactly one index row and every
row points at a file that exists; every index row has three columns; every relative markdown link in
the project's own docs resolves on disk; and every REST path, WebSocket path and `Capability` member
the app actually serves is named in `docs/api.md`. Routes come from `app.openapi()` over the same
`create_app(MockRadio(...))` seam the API tests already drive — no new fixture, no server bind.

**Anchor fragments are deliberately not checked.** GitHub's slugification of emoji and variation
selectors does not round-trip; it produced a false positive during the audit, and a check that cries
wolf gets deleted rather than fixed.

**What this cannot prove is the important half.** "There is no CAT" would have passed all seven
checks. A path being *mentioned* in `api.md` is not the same as being *described correctly* — four of
this cycle's worst findings were sentences that were false about endpoints already documented. The
locks catch absence, never wrongness. Review still has to do the rest, and a green suite must not be
read as more than it measured.

### Fail-first, and one honest miss

Run against the tree before any doc was touched, **five of the seven failed**: 18 unindexed ADRs, 1
malformed row, 1 broken link (`HANDOFF.md:1103`), 10 undocumented endpoints, 2 undocumented
WebSockets.

**The capability check passed, and I had predicted it would fail.** All eleven capability strings
already appear somewhere in `api.md` — the actual drift was in a *prose list* at one specific place
that named five of seven. A substring search cannot see that, and dressing it up as a lock that caught
something would misrepresent what it does. It is kept because it will catch the *next* capability
added without documentation, which is a real future failure; it caught nothing today.

### `status.power` on the `uvk5` backend

The audit surfaced a claim in `api.md` that `POST /power`'s 501 is the UV-5R case. It is not: the
`uvk5` dock backend does not advertise `set_power` either, and neither does `kv4p`. Only the mock and
`baofeng`-with-a-tuner do. **That is correct as built** — over the dock, power is a raw `0x36` PA-bias
write whose per-band calibration lives in flash the host cannot read (ADR 0128/0134), a different
mechanism with its own open questions. The docs were wrong, not the capability set.

### The crossband guide leads with the warning

The operator asked for the full D-STAR/DVAP operator guide, a feature area with **zero** narrative
documentation despite shipping two UI cards, eight endpoints and two WebSockets. It could not be
written as a setup page, because three facts dominate everything else about it:

- The reflector→RF crossband **stranded the transmitter keyed at least five times** across three bench
  sessions (ADRs 0090/0092/0093/0097/0099). `POST /ptt {"on": false}` returned `200` and
  `status.transmitting` read `false` while the carrier stayed up.
- **The joint dummy-load re-proof has never passed.** Attempt 1 stuck-keyed; attempt 2 put dead air on
  the air, stayed keyed, and hung the stop ~15 s. ADRs 0105–0108 then fixed four further measured
  defects, none of which has been through a dummy-load proof either.
- **There is no browser-only mode.** `dstar.operator_tx` was removed by ADR 0089 and nothing replaced
  it; `tx_to_rf` defaults to `True` and `_make_dstar_bridge` never overrides it. Setting
  `dstar.callsign` and clicking Connect arms reflector→RF keying. The link *is* the arming switch.

So `docs/dstar-setup.md` opens with the posture and the standing gate, and reaches configuration only
after them. It documents the gate; it does not open it. It also separates the **DVAPs**, which carry
no audio and key nothing, as the part an operator can safely use today.

## Consequences

- A cycle that adds an endpoint, a WebSocket or an ADR without documenting it now **fails the suite**.
  That is the point, and it is a cost paid by the cycle that creates the gap rather than by the
  operator who later trusts the doc.
- The ADR index is complete for the first time since 0096, and five ADRs whose conclusions were later
  overturned now say so in their Status line, following the convention already used at 0030/0045/0128.
  ADR bodies are historical record and were not rewritten.
- `server-notes.md` gains a dated "current state" block at the top and a note that older sections are
  records of their own date, not corrected in place — an ops log that gets retro-edited stops being
  evidence.
- Two `spec.py` description strings were wrong and render verbatim into `radio.toml.example`, which
  `configuration.md` sells as the complete reference. Fixed and regenerated; `test_config.py`'s
  byte-lock caught the stale file exactly as designed.

## Out of scope

- **The `POST /radio/select` SIGSEGV.** Documented as a hazard in `using-it.md`, `deployment.md` and
  `api.md`; **not fixed**. It is a teardown investigation that needs the bench and its own ADR. The
  operator chose this explicitly.
- **`create_app`'s D-STAR over-cap defaults to `0.0`** while `build_app` injects 60 s from settings, so
  any embedder that is not `build_app` gets no per-over ceiling. A real gap the audit surfaced;
  recorded here, not changed in a docs cycle.
- **Re-enabling the D-STAR crossband.** Still gated on the joint dummy-load re-proof from a cold-booted
  dongle.
- **Prose correctness in general.** See the Decision — no test in this cycle can check it.
