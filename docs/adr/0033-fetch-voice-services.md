# 0033 — LAN-fetch voice services: quote (5#), battery (6#), bible (7#)

Status: Accepted

## Context

The weather (2#) and astronomy (3#) services established the pattern for a DTMF voice service backed by
a LAN HTTP endpoint: a pure `format_spoken_*` function, an injected `Fetcher` (`fetch_json(url)`), a
`base_url` config setting that *both* names the endpoint and gates registration, and graceful
degradation (a dead endpoint speaks an "unavailable" line rather than crashing the controller loop).

Three more read-only LAN services were requested, each against a different host on the operator's
network:

- **5# quote** — `GET /api/quotes/random` → `{author, text, tags}`.
- **6# battery** — `GET /api/data` → an object keyed by pack id, each `{label, soc, stale, ...}`.
- **7# bible** — Concord `GET /v1/random?translation=ESV` → `{translation, verse:{reference, text}}`.

They differ from weather/astro in one way that shapes the design: each lives on its **own** host:port,
not a shared `weather.base_url`. And the quote endpoint occasionally returns a paragraph-length quote,
which over the air is an uncomfortably long single transmission.

## Decision

Add three services in the weather/astro mold — one module each, a pure `format_spoken_*`, a
construction-time `(base_url, fetcher)` binding, and per-service registration guarded by its own
`<svc>.base_url` setting (empty = disabled, `coerce_str`, marked default `""`).

- **One shared `Fetcher`.** `build_controller` builds a single `UrllibFetcher` when *any* fetch-backed
  service is enabled (weather **or** quote **or** battery **or** bible), reusing `weather.timeout` as
  the common LAN fetch timeout. All these endpoints are on the same LAN with the same
  fail-fast requirement, so one timeout setting governs them all rather than proliferating four
  near-identical `*.timeout` keys. The fetcher stays injectable (tests pass `StubFetcher`).
- **Quote length — refetch until short.** `quote_service` fetches up to `MAX_TRIES` times and speaks
  the first quote whose text is ≤ `MAX_QUOTE_WORDS` (50) words; if every try is long, it truncates the
  last one to the cap + "…". A whole short quote is preferable to a truncated long one, and the cap
  keeps any single 5# over courteous on a shared repeater. Deterministic in tests: `StubFetcher`
  returns the same payload each call, so a long stub exercises the truncate branch in one pass and a
  short stub returns on the first fetch.
- **Bible translation is config** (`bible.translation`, default `"ESV"`, `coerce_str`). The API
  defaults to KJV with no param; the operator asked for ESV, and Concord serves many translations, so
  the choice is a setting rather than a hardcoded constant.
- **Battery** speaks every pack in payload order — `"{label}: {soc} percent"`, appending `" (stale)"`
  when the monitor flags a pack stale — so a dropped BLE link is audible, not silently omitted.

Each service degrades to its own spoken "unavailable" line on any `FetchError` / missing-field error,
exactly like weather/astro.

### Why not extend `ServiceContext`

The `Service` seam (`(Session, ServiceContext) -> AudioFrame`, context = clock + TTS only) is
unchanged. Like weather/astro, the URL and fetcher are bound at construction, so nothing new needs to
flow through the per-call context.

## Consequences

- Three new optional services, each dark until its `base_url` is set — a deployment without those LAN
  hosts is unaffected, and `/services` / the web panel list exactly what is wired.
- `weather.timeout` now governs all LAN fetches (renamed in spirit, not in key, to avoid a config break
  and setting sprawl); documented on the setting.
- Four new settings (`quote.base_url`, `battery.base_url`, `bible.base_url`, `bible.translation`); the
  canary count and `radio.toml.example` move accordingly.
- 5# transmissions are bounded to ~50 words regardless of the source quote, at the cost of up to
  `MAX_TRIES` fetches when the endpoint keeps returning long quotes (all inside the short LAN timeout).
