// Live status panel (ADR 0022, simplified in ADR 0037): the folded /events state, rendered as
// compact label/value rows for an everyday operator. The prominent On air / Receiving / Idle lamp
// lives in the radio-face header (ADR 0044 — see ControlPanel's StateLamp), and the frequency/mode
// readout is the face's LCD, so this card carries the remaining facts. The CAT fields
// (channel/tone/scan) only render when the backend advertised the matching capability, so an
// audio-only radio shows just Backend instead of a column of "—". `hasCap` defaults permissive so
// the panel still renders everything if a caller omits it.
//
// One row is the exception to "hide what the backend cannot do" (ADR 0139). Without `set_frequency`
// the face's LCD is gone too, so nothing anywhere on the screen says where this station transmits —
// and in Baofeng mode, where the radio's front panel holds the channel, that is the single most
// important fact about the next over. Hiding it reads as "no frequency involved"; it is stated
// instead, as a warning, because it is a thing the operator must go and check on the radio itself.

import { formatHeld } from "../useTxAudio.js";

function fmtHz(hz) {
  if (hz == null) return "—";
  return `${(hz / 1e6).toFixed(4)} MHz`;
}

//: The three talk slots, in the order an operator would look for them. `null` in the block means the
//: subsystem is not configured at all, which is a different fact from "free" and is not rendered.
const SLOT_LABELS = { tx: "RF", mumble: "Mumble", dstar: "D-STAR" };

// Talk-slot occupancy, one line (ADR 0170). Exported and pure so the wording is testable without a
// client or a socket, the way `classifyTxMessage` is.
//
// Before this row a stranded slot was invisible: `Transmitting` and `Receiving` above are PTT state
// and squelch state, and BOTH read false while a slot is stuck — so the card positively suggested an
// idle station. The load-bearing part is the age, not the flag: "held" is ambiguous between an
// operator mid-over and a socket that died in March.
export function describeSlots(slots) {
  if (!slots) return null;
  const held = Object.entries(SLOT_LABELS)
    .filter(([key]) => slots[key]?.held)
    .map(([key, label]) => {
      const slot = slots[key];
      const age = formatHeld(slot.held_s);
      const who = slot.holder ?? "an unlabelled claimant";
      return {
        text: age ? `${label}: ${who}, ${age}` : `${label}: ${who}`,
        stale: slot.stale_after_s != null && slot.held_s > slot.stale_after_s,
      };
    });
  if (held.length === 0) return { value: "all free", warn: false };
  return {
    value: held.map((h) => (h.stale ? `${h.text} — STUCK?` : h.text)).join(" · "),
    // A slot held longer than any legal over is the whole reason this row exists; it must not read
    // like the ordinary busy case beside it.
    warn: held.some((h) => h.stale),
  };
}

// Deliberately worded "demand"/"requested", never "listeners" or "active" (ADR 0170).
//
// MEASURED: `/audio/rx` parks on its queue and learns a client is gone only from the next send, so
// with a VAD squelch on a quiet channel a dropped listener stays counted — indefinitely, in the
// RST case. This number is therefore a count of REQUESTS for received audio and not of proven-live
// listeners, and no note in `api.md` travels with the value to the card that renders it. The label
// is what travels. (Same lesson as Bandwidth vs Demodulation: two numbers that read alike in one
// card get confused, and the fix was the name.)
export function describeRxDemand(rxDemand) {
  if (!rxDemand) return null;
  const n = rxDemand.requested ?? 0;
  const reader = rxDemand.reader_running ? "reader running" : "reader stopped";
  return `${n} requested · ${reader}`;
}

// `on` is the green good/active emphasis (session open, split armed). `warn` is its opposite and
// must not reuse it — a "power is uncharacterised" alarm painted the same colour as "session open"
// reads as reassurance (ADR 0134).
function Row({ label, value, on, warn }) {
  return (
    <div className="status-row">
      <span className="status-label">{label}</span>
      <span className={`status-value${on ? " on" : ""}${warn ? " warn" : ""}`}>{value}</span>
    </div>
  );
}

export default function StatusPanel({ state, hasCap = () => true, slots = null, rxDemand = null }) {
  const s = state || {};
  const scan = s.scan ? `${s.scan.phase}${s.scan.frequency ? ` @ ${fmtHz(s.scan.frequency)}` : ""}` : "—";
  const sessionOpen = s.session ? s.session.phase === "session_open" : s.sessionOpen;
  const occupancy = describeSlots(slots);
  const demand = describeRxDemand(rxDemand);
  return (
    <div className="card">
      <h2>Status</h2>
      <Row label="Backend" value={s.backend ?? "—"} />
      {!hasCap("set_frequency") && (
        <Row label="Transmits on" value="set on the radio — not readable from here" warn />
      )}
      {/* Only when a split is actually armed. The face's LCD shows the RX frequency, so without
          this row nothing anywhere tells the operator that PTT keys somewhere else (ADR 0133). */}
      {hasCap("set_split") && s.tx_frequency != null && (
        <Row label="Transmits on" value={fmtHz(s.tx_frequency)} on />
      )}
      {hasCap("set_channel") && <Row label="Channel" value={s.channel ?? "—"} />}
      {hasCap("set_tone") && <Row label="Tone" value={s.tone != null ? `${s.tone} Hz` : "—"} />}
      {/* The demodulator, and what it costs when it is not FM (ADR 0150/0154). The value carries the
          alarm rather than a separate row, which is the PA row's shape below — `tx_ok: false` means
          NON-FM, so the modulation is quoted rather than assumed to be AM (USB is reserved on this
          wire). Gated like the other CAT rows so an audio-only radio shows Backend and not a column
          of "—"; that gate can never hide the warning, because the only tuners that ever measure
          `tx_ok` are the ones that advertise `set_modulation`. The second term covers the case where
          a runtime 501 has withdrawn the capability while the refusal still stands. */}
      {(hasCap("set_modulation") || s.tx_ok === false) && (
        <Row
          label="Demodulation"
          value={
            s.tx_ok === false
              ? `${s.modulation ?? "not FM"} — transmit disabled`
              : (s.modulation ?? "—")
          }
          warn={s.tx_ok === false}
        />
      )}
      {hasCap("scan") && <Row label="Scan" value={scan} />}
      <Row label="OTA session" value={sessionOpen ? "open" : "—"} on={!!sessionOpen} />
      {/* Who holds the transmitter, and since when (ADR 0170). Absent on a server too old to report
          it, rather than rendered as "free" — an unanswered question must not look like a clear
          answer. */}
      {occupancy && <Row label="Talk slots" value={occupancy.value} warn={occupancy.warn} />}
      {/* Its own row, next to but NOT inside the talk-slot line: talk slots are self-reaping and
          this number is not, and one row holding both would lend this one their trustworthiness. */}
      {demand && <Row label="RX demand" value={demand} />}
      {/* What the last over actually radiated. Absent until the first key-up of the process, and
          absent on backends that cannot see their PA. The wrong-band case is the one worth reading:
          the bias byte is the other band's calibration and the output level is uncharacterised, so
          it is called out rather than shown as a number nobody can interpret (ADR 0134). */}
      {s.pa && (
        <Row
          label="PA (last over)"
          value={
            s.pa.band_matched
              ? `bias ${s.pa.bias} · ${fmtHz(s.pa.tx_frequency)}`
              : `bias ${s.pa.bias} — WRONG BAND, power uncharacterised`
          }
          warn={!s.pa.band_matched}
        />
      )}
    </div>
  );
}
