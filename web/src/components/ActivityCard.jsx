// Channel-activity card (ADR 0040): the Tier-0 "is this channel actually dead?" answer, rendered in
// plain language for a non-technical operator. Reads GET /activity/summary (ADR 0039) and turns the
// ChannelActivity rollup into sentences — "Heard 14 times this week. Busiest around 7-8 am and on
// Tuesdays. Last heard 40 minutes ago." — never a grid of statistics.
//
// Honesty rules (from the prompt + ADR 0037):
//   - The records carry no frequency (no CAT on the Baofeng), so this is "your radio's channel," not
//     "146.940". We never invent a frequency.
//   - by_hour and by_weekday are MARGINAL distributions, not a joint grid, so we state two independent
//     facts ("busiest mornings AND busiest on Tuesdays"), never a joint "Tuesday 8pm".
//   - A zeroed summary is NOT an error. When the software squelch is off, that is the single most
//     likely reason the panel is empty — we say so, with the fix, instead of showing a bare "nothing".
//
// Refresh on load and on demand (a header button), no polling loop — mirrors SettingsView's `load`.

import { useCallback, useEffect, useState } from "react";
import { Unauthorized } from "../api.js";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

// Index of the largest bucket, or -1 when every bucket is zero (nothing to report).
function argmax(counts) {
  let best = -1;
  let bestVal = 0;
  for (let i = 0; i < counts.length; i++) {
    if (counts[i] > bestVal) {
      bestVal = counts[i];
      best = i;
    }
  }
  return best;
}

// A single clock hour in 12-hour form: 0 -> "12", 13 -> "1". Meridiem is added separately so a range
// that stays within one half of the day shows it once ("7-8 am") instead of twice.
function hour12(h) {
  const v = h % 12 === 0 ? 12 : h % 12;
  return String(v);
}
function meridiem(h) {
  return h < 12 ? "am" : "pm";
}

// The busiest hour as a range, e.g. "7-8 am" or "11 pm-12 am" (meridiem repeated only when it flips).
function hourRange(h) {
  const start = h;
  const end = (h + 1) % 24;
  if (meridiem(start) === meridiem(end)) {
    return `${hour12(start)}-${hour12(end)} ${meridiem(end)}`;
  }
  return `${hour12(start)} ${meridiem(start)}-${hour12(end)} ${meridiem(end)}`;
}

// A duration in seconds as an approachable phrase: "less than a minute", "about 12 minutes",
// "about 1.5 hours". Rounded on purpose — this is a plain-language signal, not a stopwatch.
function airtimePhrase(seconds) {
  if (seconds < 60) return "less than a minute";
  const minutes = seconds / 60;
  if (minutes < 60) {
    const m = Math.round(minutes);
    return `about ${m} minute${m === 1 ? "" : "s"}`;
  }
  const hours = minutes / 60;
  const h = Math.round(hours * 10) / 10;
  return `about ${h} hour${h === 1 ? "" : "s"}`;
}

// Relative time since a unix-epoch timestamp, from the browser clock: "just now", "40 minutes ago",
// "2 hours ago", "3 days ago".
function relativeSince(epochSeconds, nowSeconds) {
  const delta = Math.max(0, nowSeconds - epochSeconds);
  if (delta < 60) return "just now";
  const minutes = Math.floor(delta / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(delta / 3600);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(delta / 86400);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export default function ActivityCard({ client, squelch, onAuthError }) {
  const [summary, setSummary] = useState(null); // null = loading (tri-state, like ServicesView)
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSummary(await client.activitySummary());
    } catch (e) {
      if (e instanceof Unauthorized) return onAuthError?.();
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [client, onAuthError]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="card">
      <div className="log-head">
        <h2>Channel activity</h2>
        <button type="button" className="link" onClick={load} disabled={loading}>
          refresh
        </button>
      </div>
      <p className="muted services-hint">Your radio's current channel — not a specific frequency.</p>

      {error && <div className="error" role="alert">{error}</div>}
      {summary === null && !error && <div className="muted">Loading…</div>}
      {summary !== null && !error && <ActivityBody summary={summary} squelch={squelch} />}
    </div>
  );
}

function ActivityBody({ summary, squelch }) {
  const heard = summary.busy_count ?? 0;

  if (heard === 0) {
    // Not an error — an empty ledger. The most likely real cause is the software squelch being off.
    if (squelch === "off") {
      return (
        <div className="notice" role="status">
          Nothing heard yet. Activity is tracked from the software squelch, which is currently{" "}
          <strong>off</strong>. Set <code>audio.squelch</code> to “audio” in Settings and restart the
          server to start logging what the channel hears.
        </div>
      );
    }
    return <div className="muted">Nothing heard yet — the channel has been quiet.</div>;
  }

  const lead = heard === 1 ? "Heard once this week." : `Heard ${heard} times this week.`;

  const busyHour = argmax(summary.by_hour ?? []);
  const busyDay = argmax(summary.by_weekday ?? []);
  const parts = [];
  if (busyHour >= 0) parts.push(`around ${hourRange(busyHour)}`);
  if (busyDay >= 0) parts.push(`on ${WEEKDAYS[busyDay]}s`);
  const busiest = parts.length ? `Busiest ${parts.join(" and ")}.` : null;

  const airtime =
    summary.total_airtime > 0 ? `About ${airtimePhrase(summary.total_airtime)} of activity.` : null;

  const lastHeard =
    summary.last_heard != null
      ? `Last heard ${relativeSince(summary.last_heard, Date.now() / 1000)}.`
      : null;

  return (
    <>
      <p>{lead}</p>
      {busiest && <p className="muted">{busiest}</p>}
      {airtime && <p className="muted">{airtime}</p>}
      {lastHeard && <p className="muted">{lastHeard}</p>}
    </>
  );
}
