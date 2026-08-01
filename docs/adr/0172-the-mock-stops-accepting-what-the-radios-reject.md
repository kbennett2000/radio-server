# ADR 0172 — The mock stops accepting what the radios reject

**Status:** Accepted · 2026-08-01 · closes the two findings
[ADR 0154](0154-two-controls-must-not-both-say-fm.md) carried and
[ADR 0160](0160-the-bench-answers-back.md) measured on the station (finding 13)

## Context

Two carried findings that are one defect, written down seventeen ADRs ago and both still open:

1. **`POST /mode` returns 500, not 422, on a value the backend rejects.** `app.py` caught only
   `UnsupportedCapability`; `/tone`, `/frequency`, `/split`, `/power` and `/modulation` all carry the
   identical three-line `except ValueError → 422` arm. Confirmed empirically on the station as ADR
   0160 finding 13: `{"mode":"AM"}` → **HTTP 500**, body `Internal Server Error`.
2. **`MockRadio.set_mode` accepts anything, and that is *why* nothing caught (1).** Every API and UI
   test runs against the mock, so `{"mode":"AM"}` was 200 in the suite and 500 on the bench.
   `MockRadio.set_power` validates for exactly this stated reason; `set_mode` never did. ADR 0154
   called it "a finding in its own right" and predicted **it will hide the next divergence too**.

The order of this cycle is the argument. The mock is fixed **first**, because the double's honesty is
the instrument that makes the route's defect visible; the route is fixed second, because a strict
mock with an unguarded route turns a wrong 200 into a crash. Neither half is shippable alone, which
is what makes them one reviewable unit rather than two bundled changes.

`set_mode` is **bandwidth** (`FM`/`NFM`); `set_modulation` is the **demodulator** (`FM`/`AM`). ADR
0150 split them and ADR 0154 relabelled the UI accordingly. `AM` was never a legal mode anywhere
except in the mock — which is precisely why it is the value worth testing with: it is the word a ham
reaches for, it sat in this control's own select until ADR 0154, and it is wrong in a way that reads
as right.

## The measurement that came before the design

The premise — that the mock's permissiveness is what hides the route — was checked rather than
asserted, by staging the two edits separately and running the suite between them.

| state | `POST /mode {"mode": "AM"}` |
|---|---|
| **master** | **200**, `status.mode == "AM"` — success, reporting a bandwidth no radio in this project can be in |
| **mock tightened, route untouched** | `ValueError: mode must be FM or NFM, got 'AM'` **escapes the handler** at `app.py:1610`; the same call under `raise_server_exceptions=False` is a literal **500 / `Internal Server Error`** |
| **both** | **422**, `detail: "mode must be FM or NFM, got 'AM'"` |

The middle row is the finding made reproducible: the 500 ADR 0160 measured on hardware now appears
in the suite, on a mock, with no radio attached. That is the whole claim of guardrail 6's
software-first rule, and it had never been true of this route.

**The same staging measured the collateral.** With the mock strict and the route still unguarded, the
full suite was **1 failed, 2282 passed, 5 skipped** — the single failure being the new route test.
See *Collateral* below; the number is zero and that is not the reassuring result it looks like.

## Decision

### 1. `MockRadio.set_mode` validates, and to the fleet **intersection**

`FM`/`NFM` after `.strip().upper()`, storing the canonical form, raising
`ValueError(f"mode must be FM or NFM, got {mode!r}")` — a message byte-identical to
`AiocBaofeng.set_mode`'s, so a test asserting the 422 text asserts something true on the bench.

The accepted set is deliberately **narrower than `uvk5` alone**, which also takes `WIDE`/`NARROW` via
`_MODE_ALIASES`. A test double must be a **lower bound** on what the fleet accepts, never a superset
of one backend: the API layer is backend-agnostic and never knows which radio it is wired to, so a
green test against the double must be true of *every* backend the route can reach. Accepting `WIDE`
would be this cycle's defect reintroduced one alias down. `WIDE`/`NARROW` appear nowhere else in the
repo — no test, config, preset, doc or UI option — and `resolve_presets` rejects them at load, so no
route or config path can produce one. The asymmetry is pinned by a test so it stays a decision.

`_require_cat` stays **first**, before any value check: an audio-only backend must answer "I cannot do
this at all", never "that word is wrong" — a 422 would send the operator to fix a value on a radio
with no such control (guardrail 3). A *good* value cannot tell the two orders apart, so the test that
pins it uses a bad one.

The tuple is spelled inline rather than imported from `presets.VALID_MODES`. That is the
`POWER_LEVELS`/`CTCSS_TONES` rule already stated in `presets.py`, and here it is not merely style:
`presets` imports `backends.base` on its first line and `backends/__init__` imports `.mock`, so the
reverse import is circular and fails whenever `presets` is the entry point. A test pins the two sets
equal instead, exactly as `POWER_LEVELS` is pinned to `PowerLevel`.

### 2. `POST /mode` maps `ValueError` to 422 — locally, not app-wide

The three-line arm copied from `/tone`, one route above. The alternatives were considered and
rejected:

| alternative | verdict |
|---|---|
| `@app.exception_handler(ValueError)` → 422, mirroring the existing app-wide `RadioUnavailable` → 503 | **Rejected.** See below — it is the tempting one. |
| `ModeBody.mode: Literal["FM","NFM"]` — pydantic 422s it for free, no route change | **Rejected.** Moves the vocabulary from the backend contract into the wire schema, so a backend that legitimately accepts more becomes unreachable; the body becomes pydantic's error shape rather than the `{"detail": "<sentence>"}` every other 422 uses; and it leaves `radio.set_mode` free to raise into every other caller — the same fix-at-the-wrong-layer mistake ADR 0154 made by trimming only the UI select. |
| Fix the route only, leave the mock | **Rejected.** Untestable without hardware, which is the whole prohibition in guardrail 6. |

The global handler deserves its reasons stated, because `RadioUnavailable`'s own docstring makes the
case for it — *"registered app-wide rather than per-route deliberately… the next route added should
not have to remember"* — and three routes in this file have indeed forgotten.

It is still wrong here, for four reasons, the last decisive:

- **`RadioUnavailable` is a domain exception this project defined; `ValueError` is Python's.** The
  app-wide handler is safe because nothing raises `RadioUnavailable` by accident, and every instance
  carries a sentence written for an operator. `ValueError` has no such guarantee — any stdlib call in
  any handler. A global arm converts "the server has a bug" into "the client sent something bad" for
  the whole app, permanently and silently, and leaks developer text like
  `invalid literal for int()` into a body every other 422 fills with an operator sentence.
- **It would mask 500s that are currently the correct answer.** The broadcast-FM `off` arm calls a
  no-argument method after `action` was already validated; `/tuning/persist` takes a pydantic `bool`.
  A 422 on either is uninterpretable — there is nothing in the request to fix. It would also split
  `PATCH /settings`'s documented **400** across two codes depending on which internal frame raised.
- **App-level handlers are HTTP-shaped**, and a `ValueError` inside a WebSocket handler has no
  `HTTPException` semantics.
- **It defeats the fail-first discipline this cycle is built on.** With a global arm, a future
  route's bad-value case is **green from birth** — 422 comes back whether or not the route or the
  backend validated anything. The precise defect this cycle exists to expose, a route that never
  looks at a value, becomes structurally untestable. **A global handler does not make the next route
  remember; it makes the next route's forgetting invisible.**

The apparent duplication is also less than it looks: the six existing arms differ in intent, each
naming which `ValueError`s its route expects. Collapsing them deletes six places where a route says
what it knows can go wrong.

### 3. The protocol contract names the vocabulary

`CatRadio.set_mode`'s docstring said only *"Set the operating mode, e.g. `"FM"` (CAT)"* — the valid
set specified by an "e.g.". That vagueness is the upstream of a mock that accepted `AM` while the
whole fleet refused it, so the contract now names both words, the case-insensitivity, that it is not
`set_modulation`, and that every implementation raises `ValueError` which `POST /mode` turns into a
422.

## Collateral — measured at zero, and that is the finding

**Not one existing test broke.** With the exact tightening applied and the route left unguarded, the
full suite was **1 failed, 2282 passed, 5 skipped**, the one failure being this cycle's own new route
test. Every indirect path was checked: only two production callers of `set_mode` exist in the repo
(`presets.apply_preset` and the route); `resolve_presets` upper-cases and rejects at load, so a preset
can never carry a bad mode; the controller, bridges, scan runner, station ID, D-STAR, DVAP, doctor and
chirp never call it; `scripts/` has one comment and no call; the `uvk5.mode` config reaches
`UvK5Radio` directly and never the mock. All four direct mock call sites pass `FM`/`NFM`.

The brief expected collateral and asked for it to be listed as findings. There is none to list, and
the absence is worth more than a list would have been:

1. **The permissiveness was never used.** Across 2276 tests and seventeen ADRs, nothing relied on it.
   A relaxation no caller depends on is not a design choice; it is an omission that never got read.
2. **Zero red is the shape of a blind spot, not a clean bill of health.** If tightening a double
   changes nothing, the double was never asked the question. The FM/NFM contract that
   `presets.VALID_MODES` publishes, that `api.md` already documented **as true**, that three real
   backends enforce, and that ADR 0154 trimmed the web select down to, had **no test standing behind
   it at the mock** — the one place every API and UI test passes through.
3. **It is what licenses the ordering.** Because the mock change is *provably* free, the route test
   failing in the intermediate state is unambiguous evidence about the **route** rather than about
   the tightening. That is a claim only measurement could support, which is why both runs are
   recorded above.

Stated plainly so no reader infers otherwise: **this cycle ships with no pre-existing test going
red.** Its red run is its own new tests. One standing workaround remains as direct evidence of the
same pattern one setter over — `class _Picky(MockRadio)` in `test_api.py` exists solely to reach
`/tone`'s 422, because the mock will not raise (finding 1).

## The rest of the map — where `MockRadio` is still more permissive than production

The brief asked for the audit even where nothing changes. A double that accepts more than production
is a bug factory; here is the remaining inventory, and why each is deferred rather than swept in.

| method | the mock accepts | the fleet | disposition |
|---|---|---|---|
| `set_mode` | anything | FM/NFM (uvk5 also WIDE/NARROW) | **fixed this cycle** |
| `set_tone` | any value, never coerced — negative, 9000.0, non-numeric | **three different answers**: uvk5 a range 67.0–254.1, kv4p an exact 38-tone table ±0.1 Hz, aioc an exact 50-entry tenths table (and it requires a frequency first) | **finding 1** — its own cycle |
| `set_channel` | any int, reported in `status().channel` | aioc has **no such method**; uvk5 and kv4p raise `UnsupportedCapability` | **finding 2** |
| `set_frequency` | any int — `0`, negative, off-raster, 9e18 | positive, in-band, on-raster; aioc refuses mid-TX, uvk5 refuses while keyed | **finding 4** |
| `set_split` | anything, including with no frequency set, crossband, 900 MHz offsets | the one case `base.py` explicitly says must be stricter; aioc and uvk5 refuse four ways | **finding 4** |
| `scan(on)` | any bool, always succeeds | aioc has no method; uvk5 and kv4p raise `NotImplementedError` | no action — pydantic already validates the bool, and the divergence is capability-shaped, not value-shaped |
| `capabilities()` | `FULL_CAPS` unconditionally | no real backend grants `SET_CHANNEL`, and broadcast-FM caps are *earned from a wire reply* | no action — advertising everything is the mock's purpose; see finding 2 |
| retune while transmitting | allowed | aioc `RuntimeError`, uvk5 `Uvk5KeyingError` | no action — a TX-state model, not a value check |
| `set_power`, `set_modulation`, `set_broadcast_fm` | validated (the last by building the real frame) | matched | already correct — the precedent this cycle followed |

Deferring these is not tidiness. `set_frequency`/`set_split` are the sharp case: a hardcoded band
limit on the mock would be **a claim about hardware the mock does not have**, which guardrail 1
forbids asserting from memory. `set_mode` had no such problem — FM/NFM is a *published vocabulary*,
not a measurement. And `set_tone` genuinely changes what `POST /tone` can express, so intersecting
three disagreeing backends is a decision, not a chore.

## Acceptance

- **Red run 1**, all seven new tests against unmodified master: **4 failed, 3 passed**. The three
  passes are pins, not counted as evidence — they are the happy path, the 501/422 precedence, and the
  `_require_cat`-before-validation ordering, each of which had to survive the change. The four
  failures, verbatim: `DID NOT RAISE ValueError` (the mock swallows `AM`), `AssertionError: assert
  'nfm' == 'NFM'` (no canonicalisation), **`assert 200 == 422`** (the route, invisible behind the
  mock), `DID NOT RAISE ValueError` (the vocabulary pin).
- **Red run 2**, mock tightened and route untouched: **1 failed, 2282 passed, 5 skipped**. The
  failure is `ValueError: mode must be FM or NFM, got 'AM'` escaping through `app.py:1610` —
  measured as a literal **500 / `Internal Server Error`** under `raise_server_exceptions=False`. No
  no-raise client was committed: the shipped assertion is 422, real under either client, and the flag
  would invite reuse that turns crashes into quiet 500 assertions.
- `uv run pytest` **2283 passed / 5 skipped** (baseline 2276/5). `npx vitest run` **14 files / 155
  tests**, unchanged — no UI change was needed and none was invented. ADR 0154 already trimmed the
  browser half of this finding; that a UI-only fix left the server free to 500 is the reason it
  survived.
- **Bench, on the deployed station** (`baofeng` backend, UV-K6 over the AIOC), the same probe run
  before and after the deploy:

  | probe | before (`0e1f9cf`) | after (`db30fb9`) |
  |---|---|---|
  | `POST /mode {"mode":"AM"}` | **HTTP 500**, `Internal Server Error` | **HTTP 422**, `{"detail":"mode must be FM or NFM, got 'AM'"}` |
  | `GET /status` across that call | unchanged (147.555, FM) | unchanged |
  | `POST /mode {"mode":"nfm"}`, station untuned | *(500 — same uncaught path)* | **HTTP 422**, `{"detail":"set a frequency before a split, tone or mode"}` |
  | `POST /mode {"mode":"nfm"}`, station tuned | — | **200**, canonicalised to `NFM` |

  ADR 0160 finding 13 is closed on the hardware that raised it. The status being unchanged across
  the refused call — on both sides — is what made this safe to run on a live station: the value never
  reaches the radio, in the broken shape or the fixed one.
- **The bench found a second instance of the same defect, on the same route** — see finding 1. The
  untuned-station row above is not the vocabulary error; it is `AiocBaofeng`'s *other* `ValueError`,
  and it was a 500 on master too.
- `acceptance.py`: **8 of 10 stages PASS, `split-minus` SKIP, `web` FAIL, exit 1** — the ADR 0171
  baseline exactly. `presets`, `rx`, `dtmf`, `auth`, `tx`, `split` and `services` all PASS, which is
  the real-RF proof that a route learning to refuse a bad value did not cost a good one: the `tx`
  stage keyed and the witness saw 9 carrier polls, RMS 11974, 1000 Hz at 0.963, CTCSS 0.024, `infx`
  leg ratio. The `web` FAIL was **re-run alone rather than inferred from the summary** (ADR 0171's
  lesson) and reproduces exactly one failing check, `kv4p GET /healthz → 404` — the witness, not the
  station, whose own `/healthz` is 200 with `radio serial reader: alive`.
- Station left on **147.555**, mode **FM**, modulation **FM**, simplex, no tone, `tx_ok: true`, tune
  persist **off**, broadcast FM **off**, not transmitting, unit `active`. The acceptance run leaves
  the station on 445.800 with persist on; both were restored deliberately and verified by a final
  `GET /status` rather than assumed.

## Consequences

- `POST /mode` now answers a client error with a client-error code, naming both words that work.
  ADR 0160 finding 13 is closed on the hardware that raised it.
- The mock refuses what the radios refuse for the third setter of three; `set_power`,
  `set_modulation` and `set_mode` now share one rule and one stated reason.
- `status().mode` is canonical upper case from the mock, matching `UvK5Radio` and what
  `PresetControl`'s active-channel highlight compares by exact string equality.
- The mock is now knowingly **stricter** than `uvk5` for two alias words. Recorded with its expiry
  condition: if a route or config path ever puts `WIDE` on the wire, this set grows and
  `presets.VALID_MODES` grows with it.
- The 422 vocabulary is in `api.md` for the first time — `/mode` previously had a body column reading
  `"<str>"` and no narrative at all, while its sibling `/modulation` had fifteen lines.

## Findings

1. **The route had a second 500, more reachable than the one that was carried, and only the bench
   found it.** On an **untuned** station — the state every host restart leaves behind —
   `POST /mode {"mode":"nfm"}` hits `AiocBaofeng._stage`'s *other* `ValueError`,
   `"set a frequency before a split, tone or mode"`, which was equally uncaught and equally a 500.
   It is now a 422 by the same arm. Two things follow. **The defect was never really about `AM`:**
   the vocabulary case is the one a human notices, but the route was blind to *every* `ValueError`
   its backend could raise, and the more likely path in practice needs no bad word at all — just a
   restart. And **the mock could not have found this one**, because `MockRadio.set_mode` has no
   ordering constraint to violate: it is a divergence of *sequencing*, not of vocabulary, so no
   amount of tightening the double's value check would have surfaced it. The mock and the bench each
   found something the other could not, which is the argument for running both rather than either.
2. **`MockRadio.set_tone` is the next one, and it is a real decision, not a chore.** It accepts any
   value where the three backends disagree three ways. Its fix would delete `class _Picky(MockRadio)`
   in `test_api.py`, which exists only because the mock will not raise — a standing workaround that
   is itself evidence of the pattern this cycle names.
3. **`POST /channel` has no `ValueError` arm, but the prior question is not the arm.** No real
   backend implements `SET_CHANNEL` at all — aioc has no method, uvk5 and kv4p raise
   `UnsupportedCapability` — while the mock accepts any int *and advertises the capability*. Adding a
   422 today would ship an unreachable branch. The real question is whether the mock should advertise
   `SET_CHANNEL`, and that is a capability decision.
4. **`POST /tuning/persist` and the broadcast-FM `off` arm also lack a `ValueError` catch, and should
   not grow one.** Recorded with the reason so a later reader does not "fix" them by symmetry with
   this cycle: both take a pydantic-validated body containing nothing a 422 could tell a client to
   change, and the broadcast-FM `off` path is the always-available way *out* of a bad state (ADR
   0164) — the worst place to relabel a server bug as the operator's fault.
5. **The mock's frequency and split setters stay permissive deliberately.** A hardcoded band limit
   would be a hardware claim the mock cannot make (guardrail 1). If it is ever wanted it must be a
   *configurable* band on the mock, never a constant — otherwise the double starts asserting from
   memory exactly what the guardrail forbids.
6. **`test_docs_contract.py` cannot catch a stale status-code table.** It checks that every REST
   path, WebSocket path and capability *string* appears in `api.md`; it says so itself — *"what it
   cannot prove is whether the prose is true."* `api.md` documented `/mode`'s values as `FM`/`NFM`
   while the server returned 500 for everything else, and no gate could see the gap. The 422 summary
   row edit in this cycle is a review item, not a tested one.
7. **The station's own `radio.toml` names the backend `baofeng` on a UV-K6.** Noted only because the
   capability surface under test here (`set_mode` present at all) comes from `baofeng.uvk5_tuner`,
   not from the backend name — guardrail 3's rule that the surface is never inferred from the name,
   observed rather than restated.

## Out of scope

The fork; the witness checkout; bounding `EventHub`'s queue (ADR 0171 finding 4 — it changes
event-drop semantics and is its own cycle); adding `modulation` to `GET /presets` (a response-field
change, and still ADR 0154's finding 3); tightening the other permissive mock setters (findings 1–4);
`POST /channel`'s missing arm (finding 2).
