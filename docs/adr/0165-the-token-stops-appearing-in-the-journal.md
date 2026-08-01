# ADR 0165 — The API token stops appearing in the journal

**Status:** Accepted · 2026-08-01 · closes [ADR 0164](0164-the-on-path.md) **finding 2** and
[ADR 0161](0161-the-host-asks-the-radio.md) **finding 8** (open four cycles); extends
[ADR 0025](0025-config-system.md)'s secrets split from the plane a secret is *stored* on to the
plane it is *used* on

## Context

Seven WebSocket routes authenticate with `?token=` in the query string. That is not a preference: a
browser's `WebSocket` constructor cannot set an `Authorization` header, and this server's REST plane
uses one (`web/src/api.js:47`). uvicorn's `get_path_with_query_string`
(`uvicorn/protocols/utils.py:58-62`) appends the query string verbatim, and its WebSocket protocol
logs the result at INFO on every accept (`websockets_sansio_impl.py:377`). So the LAN bearer token
went into journald once per socket connect.

**Measured on the live station, 24 hours before this cycle deployed:**

| unit | raw `token=` lines |
|---|---|
| `radio-server` | **1068** — 1051 `/audio/rx`, 14 `/events`, 2 `/audio/mumble/rx`, 1 `/audio/tx` |
| `radio-server-kv4p` (the witness) | **40** |

The `/audio/rx` rate is `web/src/useRxAudio.js` reconnecting, roughly once every 82 seconds. This
change does not reduce that churn; it makes the churn harmless. Nobody should read it as permission
to stop caring about the reconnect rate.

### Severity, checked rather than assumed

The brief said the token was in ADR 0164 and `HANDOFF.md`, committed and pushed. **It is not.**
`docs/adr/0164-the-on-path.md:359` and `docs/HANDOFF.md:63` both read `token=<the token>` — a
placeholder — and grepping the working tree for the live value returns zero files. An ADR that
records a leak that is not there is as much a defect as one that misses a leak, so it is written
down in the direction the evidence goes.

**What is true is worse, and predates this arc.** Both repositories are **public**
(`gh repo view --json visibility` on `kbennett2000/radio-server` and
`kbennett2000/uv-k1-k5v3-firmware-custom`). The live token was committed in
`scratchpad/kv4p_audio_probe.py`, `scratchpad/kv4p_carrier_watch.py` and `scratchpad/dual_tx_watch.py`
(`9e9742e`, `1dea8d7`) and removed by `2e60c6b` when those watchers moved into `scripts/bench/` and
switched to the environment. Deleted from the tree, still reachable by SHA on GitHub. Guardrail 4's
"gated access, not secure access" is an argument about the LAN plane; it does not reach a credential
in a public repository.

**The repo already learned this rule once**, and wrote it down —
`scripts/bench/dual_tx_watch.py:25-26`: *"Credentials/endpoints come from the environment so this
works on any bench and no token is baked into the repo."* It was learned for scripts. It was never
generalized to the log plane, because nobody looks at a log line and sees a checked-in secret.

ADR 0025 kept secrets off the settings surface so that *"the settings surface (file + future REST +
future UI) can never accidentally expose or overwrite them"*, and `config/secrets.py:5-7` states it
more broadly still: *"never render, log, round-trip, or overwrite a secret."* That promise held
everywhere the secret is **stored**. The leak was on the plane it is **used** on — a URL — which no
part of that argument covered.

## Decision

### 1. Redact at the `LogRecord` factory, not at handlers and not at call sites

New `radio_server/logsafe.py`. `logging.setLogRecordFactory` wraps the previous factory and rewrites
credential-shaped text at record creation, upstream of every handler, formatter and filter.

The alternative — a `logging.Filter` on the root handler — was rejected on a fact, not a preference.
uvicorn's `dictConfig` gives the `uvicorn` logger `propagate: False` and gives `uvicorn.error` no
handlers of its own (`uvicorn/config.py:104-110`), so those records reach uvicorn's own stderr
handler and **never reach root**. A root-handler filter would have been blind to 100% of the 1108
measured lines while looking like a fix. And a rule applied at each call site cannot work at all
here: none of the call sites are ours.

Cost, measured: **1.30 µs on a hit, 2.32 µs on a miss** per string, and only for records that already
passed `isEnabledFor`.

### 2. The name layer is primary; the value layer is a backstop

**By name** — `token=`, `api_token=`, `Authorization: Bearer …`. The names come from `KNOWN_SECRETS`
and `MUMBLE_PASSWORD_PREFIX`, so adding a secret to `config/secrets.py` extends the redaction with
nobody remembering to come back here. It works before any secret is known, it survives rotation, and
it redacts a **wrong** token — uvicorn logs the 403 it writes for a rejected socket
(`websockets_sansio_impl.py:400`), and a mistyped credential is still somebody's credential.

**By value** — `register_secret_values` catches a live secret in a shape no `name=value` pattern can
span, such as the `websockets` library logging a header's name and its value as two separate
arguments. `Secrets.items()` was added so the redactor asks what the file holds rather than
enumerating names it knows about; a redactor with its own list is exactly the rule-someone-must-remember
this module exists to abolish.

Two deliberate limits. `fixed_code` is excluded **by name**, because `config/secrets.py:38-40` already
says a static 6-digit code is low-security by nature and registering it would blank every matching
frequency and byte count in the journal. And there is a **12-character floor**, because a short
secret is likely to be a substring of ordinary log text.

**The floor was not hypothetical.** The station's API token was **9 characters**, so it fell below it,
and the arming line said so on the first boot after deploy:

```
INFO: radio_server.logsafe: log redaction armed by value for: dvap_remote_password,
  mumble_password_mumble_demo, mumble_password_mumble_demo_test_channel, totp_secret;
  redacted by name only: api_token (under 12 chars)
```

That line is the design telling the operator where its own coverage stops. It names secrets, never
values, and it names the skipped ones too — a gap an operator can read beats a gap they have to
infer.

### 3. Never rewrite `msg` while there are args

This is the rule the whole implementation is shaped around, and it is not obvious.

Rewriting `record.msg` when `record.args` is non-empty can delete a `%s` and break arity. `logging`
answers a broken format by calling `Handler.handleError`, which prints `Message: …` **and
`Arguments: ('<the raw secret>',)`** straight to stderr. A redaction written the obvious way is a
*guaranteed* cleartext dump of the exact value it was protecting.

So `msg` is rewritten only when `args` is falsy, and otherwise only the `str` elements of `args` are
touched — arity and element types preserved, which is what keeps `uvicorn.logging.AccessFormatter`'s
5-tuple unpack and its `int(status_code)` working (`uvicorn/logging.py:99-106`). Containers are
rebuilt, never mutated: a dict argument is the caller's live object, and redacting it in place would
corrupt caller state rather than just the log line.

That rule leaves one shape uncovered, and the fail-first tests found it: `logger.info("api_token=%s",
value)` puts the name in the format string and the value in the args, so the string rules see a
specifier and a bare token respectively and neither fires. `_credential_slots` walks the format
specifiers in order and blanks the argument slot a credential name introduces — by position for
`%s`, by key for `%(name)s`, counting the `*` width forms that eat an extra argument.

### 4. What this deliberately does not cover

A partial guarantee stated as a total one is worse than no guarantee, so the module docstring names
its own blind spots: `extra=` fields (applied to `record.__dict__` *after* the factory returns),
`exc_text` (computed at format time), `workers=`/`reload=` children (which never run `main()`), and
`print()` — `enroll.py` deliberately prints a fresh TOTP secret to a terminal, which is the feature.
None has a live vector today. If one acquires one, the successor is a post-format **Formatter**
layer, which sees all four at once at the cost of owning uvicorn's `log_config`. Named trigger, not
built on a hypothetical.

### 5. `acceptance.py`: `RESULT: INCOMPLETE`, exit 3

`PASS`/0, `FAIL`/1, **`INCOMPLETE`/3**. Exit 2 was already "the run never began" (no
`RADIO_API_TOKEN`, unknown stage name). Every caller testing `rc != 0` keeps today's safety; a caller
can now tell "a check failed" from "a check never ran".

**Consumers audited before changing an exit code, and there are none beyond a human.** No
`.github/workflows` exists. No crontab entry and no systemd timer on the bench box. Every
`from acceptance import …` in `scripts/bench/` — `keyup_reliability`, `cold_key`, `aioc_ptt_gate0`,
`fm_tx_interlock`, `repeater_evidence`, `repeater_openup`, `deviation_probe`, `tune_follows_preset`,
`fm_cadence` — imports helpers (`api`, `rms`, `tone_power`, `RADIO_BASE`, `KV4P_BASE`, `BENCH_TX_HZ`,
`TOKEN`); none calls `main()` or reads its exit code. The single `rc == 1` in `scripts/bench/`
(`tune_follows_preset.py:527`) is its own `require_unanimous`.

Also fixed while there: `stage_verdict` now reads `ok` before `skipped`, so a stage that failed a
check and *then* skipped displays FAIL rather than SKIP. Unreachable today — every `skip()` call site
returns immediately — which is precisely why the display and the tally disagreeing would go
unnoticed. `overall_verdict` is a pure function of the stage list, so the banner finally has test
coverage that needs no radio, no station and no token.

## Fail-first

Two new test files, red before implementation. The first run was an `ImportError` for a module that
did not exist yet; a stub with the full surface and no behaviour replaced it so the red would be
**behavioural** rather than a collection error (ADR 0162's lesson, applied even though this one would
not have cascaded).

Red: **25 failed, 2174 passed, 5 skipped** — the two new files alone 25 failed / 5 passed. The five
that passed red are the guard cases that pin the *absence* of over-redaction (`fixed_code` untouched,
a short secret not value-registered, non-`str` args intact), and they must stay green in both
directions.

The integration leg is a real `uvicorn.Server` on a real ephemeral port, running uvicorn's own
`LOGGING_CONFIG` with the handler streams redirected to a buffer, connected by the real
`websockets.sync.client`, asserting on what the handlers actually emitted — **0.42 s**. A hand-rolled
handshake was rejected deliberately: uvicorn logs *nothing* when `ServerProtocol.accept()` refuses a
malformed upgrade (`websockets_sansio_impl.py:181-190`), so "the token is not in the output" would
have passed because the event never happened. Each of the three integration tests asserts the line
was logged *before* asserting the token is absent, for the same reason.

## Bench

Every measurement below is HTTP or journal. **The AIOC tty was not opened at any point** — ADR 0163
killed the dock link exactly that way while `/status` reported healthy throughout.

### B1-B3 — deploy, then the leak, both legs

Deployed `1ba81c3`, restarted, `/status` 200, both units active.

| | raw `token=` | redacted |
|---|---|---|
| 24 h before (`radio-server`) | **1068** | 0 |
| 24 h before (witness) | **40** | 0 |
| after, incl. a full acceptance run | **0** | 14 |

Both legs on the wire, in one window:

```
"WebSocket /audio/rx?token=<redacted>" [accepted]
"WebSocket /events?token=<redacted>" [accepted]
"WebSocket /audio/rx?token=<redacted>" 403      <- a deliberately wrong token
```

The witness's first "0 raw" was **vacuous** — nothing had connected a socket to it — so a socket was
connected to port 8091 specifically to make the observation real, and it produced a redacted line of
its own.

### B4 — the rotation, and why the order is forced

Deploy → verify → rotate, never the other way round: rotating first means the replacement starts
leaking on the next socket connect, so rotation without deployment is worse than no rotation.

`POST /settings/secrets/api-token/rotate` with no body, so `rotate()` generated the value rather than
accepting one: **200**, `restart_required: true`, 9 characters → 43, url-safe charset, file still
`0600`. After the restart the old value returned **401**, the new one **200**, and a socket connected
and logged a redacted line. **The value is not recorded here, in `HANDOFF.md`, in the PR, or in any
commit message** — reporting it the way the old one was reported would re-create the leak in the same
breath.

Rotation also closed the value-layer gap from decision 2, and the arming line is the evidence:

```
before: … redacted by name only: api_token (under 12 chars)
after:  log redaction armed by value for: api_token, dvap_remote_password, …
```

**The rotation was subsequently reverted by the operator.** Both `radio-secrets.toml` files were
written back to the previous 9-character value at 02:38:58 (witness) and 02:39:18 (station) — writes
this cycle did not make. The consequence is a split state that a reader should not have to work out:
both units started at 02:35 and still hold the rotated value in memory, so the value on disk returns
**401** on both ports and the next restart of either unit puts the old token back into service. The
old value remains in the public git history; that is what rotation was for, and it is again open.

### B5 — the banner, on the real station

```
summary
  systemd PASS · web PASS · presets PASS · rx PASS · dtmf PASS
  auth PASS · tx PASS · split PASS · split-minus SKIP · services PASS

  1 stage(s) could not be attempted: split-minus

RESULT: INCOMPLETE          exit 3
```

**9 of 9 attempted PASS.** The first run in five cycles that does not print `RESULT: FAIL` on a clean
bench. `split-minus` still SKIPs for the missing `Bench Split Minus` preset — the banner fix covers
every future skip, including the RF stages skipping whenever the two radios are off-channel, but the
preset itself is a separate item and is carried, not done.

### B6 — restored

**147.555**, `tune_persist` off, broadcast FM off, `rescues` 0, not transmitting, both units active,
`radio.toml` byte-identical at `ead78a44…` on the station and `f9be6bb5…` on the witness.

### What the bench found that pytest could not

1. **The witness instance was three cycles behind and also leaking.** `radio-server-kv4p` is a
   separate checkout with its own config and its own secrets file, running `d779aca` (PR #218, ADR
   0161) and contributing 40 lines a day. Fixing only the station would have been the drip-feed
   failure this repo keeps closing. It is now on the same commit.
2. **One rotation, two processes.** `acceptance.py` authenticates to both stations with a single
   `RADIO_API_TOKEN`, so the two instances have always shared one value. Rotating one broke that
   invariant and every RF stage failed `401` until the witness was given the same value and
   restarted. Rotation is a two-unit operation on this box.
3. **The station's documented deploy command breaks the witness.** `uv sync` is exact, and the
   witness is a kv4p node: syncing it with the station's `--extra hardware --extra tts --extra mumble`
   removes `opuslib` (the `opus` leaf, composed by `kv4p`), and `POST /transmit` then answers 500 with
   `ModuleNotFoundError: No module named 'opuslib'`. This cycle caused it and fixed it; it survived
   until now only because `uv run` is *not* exact and never removed what a past sync had installed.
   `docs/server-notes.md` now carries the witness's own command.
4. **`tune_persist` does not survive a restart, and never has.** `aioc_baofeng.py:109` says so in
   as many words — *"A boot value, not the setting: `set_tune_persist` moves it at runtime and does
   not write config"* — and `radio.toml` has said `uvk5_tune_persist = true` throughout, in a file
   byte-identical to the one ADR 0164 hashed while recording "tune_persist off". Every restore note
   in this arc has been describing a state that dies at the next restart. Re-applied here, and the
   config still says `true`.
5. **A diagnostic nobody can read is not a diagnostic.** `logging.basicConfig` sat just above
   `uvicorn.run`, so everything logged during startup — including the arming report this cycle
   added — was emitted before root had a handler and went nowhere. Found by running the real
   entrypoint rather than by any test; the test that now pins it was written afterwards.

## Consequences, and what was deliberately not done

- **journald output from this station is safe to paste.** That is the actual acceptance criterion,
  and it is what makes the rest of this project's habits — pasting journal excerpts into ADRs, PRs
  and chat — safe rather than lucky.
- **Auth behaviour is unchanged.** The token still travels in the query string, it is still validated
  the same way, and no route moved. This is a logging change. The query string remains visible in
  browser history, DevTools' Network panel, and to any page script via `ws.url`; a subprotocol
  handshake is the real fix for that and is not this cycle's.
- **Git history was not rewritten.** Out of scope, and rotation is what actually closes a leaked
  credential — a rewrite would not reach a clone, a fork, or a cached view.
- **Carried:** the `Bench Split Minus` preset (fixes one skip; the banner fix covers the rest), and
  the witness unit's `ExecStart` naming extras that exclude `kv4p`, which leaves its codec dependent
  on `uv run` not being exact.

## Verification

`uv run pytest` — **2199 passed, 5 skipped** (baseline 2169/5). `npx vitest run` — **13 files, 131
tests**, unchanged: no web change was expected or made, and it is reported as the regression check it
is. Red before implementation: **25 failed, 2174 passed, 5 skipped**, all behavioural after the
module stub replaced the collection error.
