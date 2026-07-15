# 0036 — Channel-activity summary: turning RX ledger edges into a busy-ness answer

Status: Accepted

## Context

The first product feature — "is this repeater actually dead?" — has, since ADR 0035, a durable
signal to answer it: paired `rx_open`/`rx_close` records in the station ledger
(`radio_server/eventlog/log.py`). But the ledger is raw history — one line per squelch edge — and
nothing turns it into an answer. Tier 0 needs the obvious rollups: how often the channel is busy,
*when* during the day and week, and when it was *last heard*.

This cycle builds **only** the pure transform that computes that answer. It is deliberately the
smallest load-bearing unit: no file reading, no API, no UI. The JSONL reader is the next cycle;
routing/UI are the ones after. Building the transform in isolation lets it be specified and tested
entirely from literals, with the same determinism the rest of the tree gets from an injected clock.

The record schema it consumes (verified against `eventlog/log.py`, ADR 0035):

- `rx_open`  → `{"ts": <float epoch>, "type": "rx_open"}`
- `rx_close` → `{"ts": <float epoch>, "type": "rx_close", "duration": <float|None>}`

`ts` is a unix epoch float — the same seam as `Clock = Callable[[], float]`.

## Decision

Add `radio_server/eventlog/summary.py`, a **leaf-pure** sibling of `log.py`/`sink.py`: stdlib only,
no import of any other `radio_server` layer, no `Settings`, no config, no disk, no wall clock. It
exposes one function and one frozen result:

```python
def summarize_activity(
    records,            # Iterable[dict] — already-parsed ledger dicts; this cycle reads no file
    *,
    now: float,         # injected unix ts, the Clock seam
    tz: ZoneInfo,       # injected timezone, the time_service seam
    window: timedelta = DEFAULT_WINDOW,          # default 7 days
    min_duration: float = MIN_DURATION_DEFAULT,  # seconds; marked verify-on-hardware
) -> ChannelActivity


@dataclass(frozen=True)
class ChannelActivity:
    busy_count: int                 # paired events surviving both filters
    total_airtime: float            # sum of event durations, seconds
    last_heard: float | None        # most-recent surviving event's open ts, or None
    by_hour: tuple[int, ...]        # length 24, index = LOCAL hour-of-day; value = event COUNT
    by_weekday: tuple[int, ...]     # length 7, index = LOCAL weekday (Mon=0); value = event COUNT
```

`now` and `tz` are **injected**, exactly like `format_spoken_time(now, tz)` in
`services/time_service.py`. The summarizer never reads a clock or a config file; a caller at the
composition edge resolves `now` (`time.time`) and `tz` (`load_timezone(settings)`) and passes them
in. This is what makes the whole function exercisable from literals with a fake `now`.

### Pairing: one `rx_open` + its `rx_close` = one busy event

The mapper walks records **in iteration order** — the ledger is append-only chronological, and the
next cycle's reader preserves that order — and mirrors `EventLog`'s own paired-edge state machine:
a single `pending_open_ts`.

- `rx_open` → set `pending_open_ts = ts`. A prior pending open is **overwritten** and thus
  **skipped** — this is the crash / still-busy case (an open whose close never arrived, or a second
  open before the first closed). We skip it; we do not guess a close time.
- `rx_close` → if a pending open exists, emit event `(open_ts, close_ts)` and clear the pending
  open. Otherwise **skip** it — an unpaired close (no open seen) contributes nothing.
- Any pending open left at the end of the stream is **skipped**.

An event's duration is `close_ts - open_ts`, computed from the pair — it does **not** rely on the
`rx_close` record's own `duration` field being present. Bucketing, `last_heard`, and window
filtering all key off the event's **open** timestamp: the moment activity started, i.e. when the
channel was heard.

### `min_duration`: a squelch crackle is not a QSO

A 0.3 s squelch crackle is not real activity. Events whose duration is below `min_duration` are
excluded from every output (count, airtime, and both bucket sets). The default,
`MIN_DURATION_DEFAULT = 1.0` second, is **marked "verify on hardware"** (guardrail 1): the
crackle-vs-transmission cutoff is a bench fact, not a known one, and the real value is tuned once
audio is flowing. It lives as a defaulted parameter with a verify note, never a hardcoded truth.

### `window`: recent history only

Events whose open timestamp is older than `now - window` are excluded. The default,
`DEFAULT_WINDOW = timedelta(days=7)`, answers "is it dead *lately*" rather than reporting ancient
history from an append-only file that never forgets. Pairing runs **before** the window filter, so
an open just inside the cutoff still pairs with a close just after `now`; the window then judges the
finished event by its open time.

### Bucketing is by LOCAL time

Buckets are indexed by local wall-clock via the injected `tz`:
`datetime.fromtimestamp(open_ts, tz)` → `.hour` (0–23) and `.weekday()` (Monday = 0 … Sunday = 6).
`fromtimestamp` with a `ZoneInfo` is DST-correct by construction, so "busiest at 7am" means 7am
**where the station is**, in both standard and daylight time — a 7am-local event in January (EST)
and one in July (EDT) both land in `by_hour[7]` despite different UTC offsets.

Each bucket holds an **event count**, not airtime: the buckets answer *when* activity happens
(distribution across the day/week); `total_airtime` is the separate how-much signal.

### Malformed / unknown records are skipped, not raised

The ledger is append-only history and may hold records written by older schema versions, plus every
other record type (`tx_key_up`, `session_open`, `scan`, …). The summarizer reads only `rx_open`/
`rx_close` and silently skips anything that is not a dict, lacks a numeric `ts`, or carries any
other `type`. It never raises on input — a summary is a read over untrusted history, and one bad
line must not sink the whole rollup.

### Empty input → a zeroed summary

Empty (or entirely-skipped) input returns a zeroed `ChannelActivity` —
`busy_count=0`, `total_airtime=0.0`, `last_heard=None`, `by_hour=(0,)*24`, `by_weekday=(0,)*7` — not
an error. "No activity" is a valid, expected answer to "is this repeater dead?", not an exceptional
one.

## Known limit: per-radio, not per-frequency (documented, not solved)

`rx_open`/`rx_close` carry **no frequency**. `AiocBaofeng` has no CAT, so the ledger records
activity on "the channel the radio was parked on," not "146.940 MHz." This summary is therefore
**per-radio, not per-frequency**: it answers "how busy is whatever this radio is sitting on," and it
cannot attribute activity to a specific frequency or split a multi-frequency scan. Per-frequency
labeling and a multi-frequency activity sweep both wait on the TM-V71A CAT backend (which can report
the tuned frequency) and are explicitly out of scope here.

## Consequences

- **The raw RX ledger becomes an answer.** A future reader (next cycle) can hand a parsed
  `rx_open`/`rx_close` stream to `summarize_activity` and get busy count, airtime, last-heard, and
  local-time distributions — the data Tier 0 needs.
- **`eventlog/` stays a leaf.** `summary.py` imports only stdlib (`dataclasses`, `datetime`,
  `zoneinfo`, `collections.abc`); it adds no dependency on any other `radio_server` layer and no
  runtime import cycle. `ChannelActivity`, `summarize_activity`, and the two marked-default
  constants are re-exported from `radio_server.eventlog`.
- **Deferred, on purpose (out of scope here):** reading `radio-server.jsonl` from disk (the
  next cycle); any API route or UI over the summary; any per-frequency attribution (waits on the
  V71A backend); any config plumbing — `now`, `tz`, `window`, and `min_duration` are all parameters.
- **Cross-references:** ADR 0015 (activity detection / squelch modes — the source of the edges),
  ADR 0018 (the event log), ADR 0029 (the AIOC/Baofeng backend — why there is no frequency),
  ADR 0035 (the RX records this consumes).
