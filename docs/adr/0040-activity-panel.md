# 0040 — The activity panel: channel activity in the control grid

Status: Accepted

## Context

`GET /activity/summary` (ADR 0039) serves the Tier-0 answer — *is this channel
actually dead?* — as a `ChannelActivity` rollup. Nothing rendered it yet, so the
answer was reachable only by `curl`.

It matters most for the person least able to run `curl`: a new ham who keys up at
2pm on a Saturday, hears nothing, and concludes the repeater is a graveyard —
when in fact it's busy at commute hours and on net night. The Tier-0 panel exists
to replace that wrong conclusion with the truth: "heard 14 times this week,
busiest mornings and Tuesdays, last heard 40 minutes ago."

This cycle adds **one card** to the existing SPA. It is deliberately *not* a
second app, route, build, mount, or shell — it follows the shape ADR 0037
established: everyday actions first, honest plain-language labeling, and empty
states rendered as a calm note rather than an error.

## Decision

Add `web/src/components/ActivityCard.jsx`, mounted in the control grid
(`ControlPanel.jsx`) **immediately after the Listen/Talk action cards** and before
Status. Actions lead (ADR 0037's rule); this card is read-only telemetry, so it
follows them. It is capability-independent — activity is summarized on any backend
— so it is always rendered, never capability-gated.

The card reuses existing styles only (`.card`, `.log-head` + `.link`, `.muted`,
`.notice`, `.error`) — **no CSS is added or changed**, so nothing else is
restyled or reordered.

### Plain language, not statistics

The card turns the raw `ChannelActivity` (`busy_count`, `total_airtime`,
`last_heard`, `by_hour[24]`, `by_weekday[7]`) into sentences:

> Heard 14 times this week. Busiest around 7-8 am and on Tuesdays. About 12
> minutes of activity. Last heard 40 minutes ago.

- "this week" reflects the default 7-day `activity.window`.
- Busiest hour is the argmax of `by_hour` rendered as a 12-hour range ("7-8 am");
  busiest weekday is the argmax of `by_weekday`.
- `last_heard` is rendered relative to the browser clock ("40 minutes ago").
- All formatting is done by small pure helpers at the top of the component.

No raw grid of 24 hourly and 7 daily counts is shown — that is the "statistics"
the prompt and ADR 0037 push against.

### Two honesty constraints, encoded

1. **No invented frequency.** `rx_open`/`rx_close` carry no frequency (the
   Baofeng has no CAT — the per-radio-not-per-frequency limit named in ADR 0036).
   So the card is titled "Channel activity" with the hint "Your radio's current
   channel — not a specific frequency." It never prints "146.940" or any
   frequency it does not have.

2. **Marginal, not joint.** `by_hour` and `by_weekday` are *separate marginal*
   distributions, not a joint hour×weekday grid. The data supports "busiest around
   7-8 am" and, independently, "busiest on Tuesdays" — but **not** "Tuesday 8pm,"
   which asserts a joint peak the summary can't know. The card states the two
   facts joined by "and," never as a single joint claim. (If a joint peak is ever
   wanted, the summarizer must emit a joint bucket first; the UI will not fake it.)

### A zeroed summary is not an error

An empty or missing ledger returns a zeroed `ChannelActivity` as a normal `200`
(ADR 0039). The card treats `busy_count === 0` as a state, not a failure:

- **When `audio.squelch` is `"off"`** — the single most likely reason a real user
  sees an empty panel — the card explains it in a `.notice`: activity is tracked
  from the software squelch, which is currently off; set `audio.squelch` to
  `"audio"` in Settings and restart to start logging. This is the honest,
  actionable truth instead of a bare "nothing," which would read as "dead channel"
  — exactly the wrong conclusion this panel exists to prevent.
- **When squelch is on** (`"audio"`/`"cat"`) — a plain muted "Nothing heard yet —
  the channel has been quiet," a legitimately quiet or freshly-installed channel.

`audio.squelch` is a `/settings` value (default `"off"`), not a field on the
route. `ControlPanel` already fetches `/settings` once at mount (for
`web.auto_listen`); that same fetch now also reads the effective squelch
(`value ?? default`) and passes it to the card as a prop — no second round-trip.
Because squelch is restart-to-apply, reading it once is correct; the card's
refresh re-pulls only the summary.

### Refresh on load and on demand — no polling

The summary fetch is a `useCallback load` run on mount and reused by a "refresh"
link in the card header (the `SettingsView` reload pattern). There is **no polling
loop** this cycle: a summary is a point-in-time read, and a live poll would add
load for a number that changes slowly. A `401` routes to the token gate (house
convention); any other fetch error shows an `.error` line.

## Out of scope

- No second build/mount/shell, no new route, no query params on the route, no
  Link/network work.
- No restyle or reorder of any other card.
- No polling loop.
- No joint hour×weekday rendering (the data is marginal).
- No frontend test runner: the SPA has none today, and adding vitest + config +
  deps is scope creep for one card. The pure format helpers are verified ad-hoc
  during implementation and by browser review (below).

## Consequences

- **Tier-0 is legible.** The "is this repeater dead?" answer is now visible to a
  non-technical operator in plain language, with the empty-panel trap
  (squelch off) explained rather than mistaken for a dead channel.
- **Verification is build + human browser check.** `uv run pytest` is untouched
  (no backend test loads the real SPA bundle) and `npm run build` must succeed. A
  headless cycle cannot drive a browser, so the prompt's "browser-verified against
  a live server" step is handed to the human reviewer, spelled out in the PR: with
  `audio.squelch="audio"` the card shows a real summary; with `"off"` and a zeroed
  ledger it shows the squelch reason, not a lie; refresh re-pulls.
- **The honesty guardrails hold.** No invented frequency, no joint claim from
  marginal data, empty ≠ error — each encoded in the component, not just intended.
- **Cross-references:** ADR 0037 (the SPA shape this conforms to), ADR 0039 (the
  route it renders), ADR 0036 (the per-radio-not-per-frequency limit behind the
  honest labeling).
