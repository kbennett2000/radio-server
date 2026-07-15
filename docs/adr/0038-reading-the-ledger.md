# 0038 — Reading the ledger: streaming `radio-server.jsonl` into the summarizer

Status: Accepted

## Context

ADR 0035 gave the station ledger durable `rx_open`/`rx_close` records; ADR 0036
gave `summarize_activity(records, ...)`, a leaf-pure transform that turns those
records into the Tier-0 "is this repeater dead?" answer. But `summarize_activity`
takes an **already-parsed iterable** of ledger dicts — deliberately, so it could
be specified and tested from literals. Nothing yet reads the on-disk ledger and
supplies those dicts.

This cycle builds **only** that reader: the seam between the append-only JSONL
file (`radio_server/eventlog/sink.py`, ADR 0018) and the pure summarizer. No API
route, no UI — those are later cycles. Building the reader in isolation keeps it
a small, load-bearing unit with a sharp contract.

The file it reads is what `JsonlSink` writes: one compact JSON object per line,
`flush()`ed per record, e.g.

```
{"ts":1783696840.62,"type":"rx_open"}
{"ts":1783696845.10,"type":"rx_close","duration":4.48}
```

## Decision

Add `radio_server/eventlog/reader.py`, a stdlib-only sibling of `sink.py`:

```python
def read_records(path: str | os.PathLike[str]) -> Iterator[dict]:
    ...  # a generator
```

It **streams** the ledger line by line and yields one parsed record dict at a
time. It **parses only** — it does not filter by record `type` or timestamp;
`summarize_activity` owns that. Runtime imports are stdlib only (`json`, `os`,
`collections.abc`, `typing`); path resolution is **not** re-implemented — the
caller passes a path, and `load_log_path(settings)` (already in `sink.py`) is the
existing resolver at the composition edge.

### Streams, never slurps

The ledger is append-only and unbounded — a year of squelch edges is a large
file. `read_records` is a generator over `for line in fh`, yielding one record at
a time. It never calls `readlines()` / `read()` / materialises the file. Wall
memory stays flat regardless of ledger size, and a caller that only needs the
first few records (or stops early) never pays for the rest.

### Torn / unparseable line → skipped, not raised

The writer may be mid-append, or may have crashed, leaving a truncated final
line. Any line that fails `json.loads` (`json.JSONDecodeError`) is **silently
skipped**. The ledger is *history*, not an input to validate — one bad line must
not sink the whole read. This mirrors the summarizer's own skip-don't-raise
stance on malformed records (ADR 0036): a read over untrusted history tolerates
corruption in individual lines.

### Non-dict JSON → skipped

A line that parses to a bare number, list, or string is structurally not a ledger
record. The contract is `Iterator[dict]`, so a parsed non-dict is skipped just
like an unparseable line. Note the distinction: an **unknown record type** — a
dict with a `type` the summarizer happens to ignore (`tx_key_up`, `session_open`,
an older schema) — **passes through** unchanged. Filtering by type is the
summarizer's job, not the reader's; the reader only guarantees "these are dicts
that parsed from the file."

### Missing file → empty iterator, does NOT raise

A fresh install has never transmitted or received — there is no ledger yet. That
is expected, not an error. `open` raising `FileNotFoundError` is caught and the
generator simply ends (yields nothing).

This is a **deliberate asymmetry with the sink** (ADR 0018). `JsonlSink` opens
its path at construction and **fails loud** if the path is unwritable: an
operating log that silently isn't being written is a bug worth crashing over.
The reader is the opposite: reading a not-yet-existing history is Tuesday.
Writing to nowhere is a mistake; reading from a history that hasn't started is
the normal first-run state.

### Concurrent writer → open, stream, close

The live server appends to the ledger while a summary reads it. `read_records`
takes no lock, does not `tail -f`, and does not retry: it opens once, streams the
lines present at that moment, and closes. A record appended *after* the read
begins is simply not seen this pass — a summary is a point-in-time snapshot, and
the next call will see the newer records. Append-only + line-buffered writes mean
the reader only ever sees whole, flushed lines or a torn tail (handled above); it
never sees a half-overwritten record, because the sink never overwrites.

## Known limit: O(all history) per summary (documented, not solved)

Every summary re-reads the **entire** ledger from the top. Records older than the
summary's window are parsed and then discarded by `summarize_activity` — work
done and thrown away on every call. At today's size (the real `radio-server.jsonl`
is ~9 KB) this is free. At a year of RX edges it will not be: the read cost grows
with total history, not with the window of interest.

The fixes are all deferred and **out of scope here**: reverse-seek from EOF
(stop once records fall before the window), a time index / sidecar, and log
rotation. None belong in a reader whose one job is "stream the file honestly."
Naming the limit is the deliverable; solving it is a later cycle once the ledger
is actually large.

## Consequences

- **The ledger becomes readable end to end.** `summarize_activity(read_records(
  path), now=..., tz=...)` is now a complete path from the on-disk file to a
  `ChannelActivity` — the data Tier-0 needs, ready for an API/UI cycle to expose.
- **`eventlog/` stays a leaf.** `reader.py` imports only stdlib; it adds no
  dependency on any other `radio_server` layer and no runtime import of
  `Settings`. `read_records` is re-exported from `radio_server.eventlog`.
- **The sink is untouched.** Reading and writing stay separate modules with
  opposite failure stances (fail-loud write, tolerant read), each justified by
  what a wrong path means for that direction.
- **Deferred, on purpose (out of scope here):** any API route or UI over the
  summary; rotation, indexing, tailing, or caching the ledger; the O(all
  history) fix above.
- **Cross-references:** ADR 0018 (the event log / sink and its fail-loud write),
  ADR 0035 (the RX records this reads), ADR 0036 (the summarizer this feeds and
  its matching skip-don't-raise stance).
