// Live transmit audio — the main-thread half of TX mic capture (ADR 0024). The mirror of useRxAudio.
//
// Owns a mic stream + an AudioContext + the `/audio/tx` binary WebSocket. Nothing is created until
// `startTalk()` runs from a user gesture (the Talk button): getUserMedia needs a gesture and an
// AudioContext starts suspended. `startTalk()` requests the mic, builds the capture graph
// (source → tx-capture worklet), opens the socket, performs the format handshake, then streams
// resampled canonical PCM. `stopTalk()` closes the socket (the server drops PTT + frees the slot),
// releases the mic, and tears the graph down.
//
// The load-bearing conversion: the mic drives the context at its NATIVE rate (often 44.1k), but
// `/audio/tx` demands canonical 48k/s16le/mono and rejects anything else (1003). So we resample
// ctx.sampleRate → 48000 (streaming linear interpolation) and encode Float32 → Int16 LE on the main
// thread — the exact inverse of RX's decode. The context is created at its default rate (not forced
// to 48k) so the resampler is always the real path.
//
// Unlike RX, TX does NOT auto-reconnect: active keying must never silently resurrect after a drop —
// the operator presses Talk again. Close codes map to explicit UI states (1008 re-gate, 1013 busy,
// 1003 format error).

import { useCallback, useEffect, useRef, useState } from "react";

const WS_POLICY_VIOLATION = 1008; // bad/missing token — re-gate
const WS_TRY_AGAIN_LATER = 1013; // single-talker slot taken — "radio busy"
const WS_UNSUPPORTED_DATA = 1003; // bad format/frame — should never happen (we send canonical)
const DST_RATE = 48000;
const FRAME_SAMPLES = 960; // 20 ms @ 48k -> 1920-byte frames (even -> whole s16le samples)

function txUrl(token, path) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}?token=${encodeURIComponent(token)}`;
}

// The socket's one TEXT channel, folded into what the operator sees. Exported and pure so it can be
// exercised without a WebSocket, the way `reduceStatus` is in `useEvents.js`.
//
// Why text at all: a browser cannot read a WebSocket close code — every server-side close arrives as
// 1006 — so anything the operator needs to know has to be *sent* before the close. That was built
// for the single-talker `busy` case, and ADR 0161 gives a refused key-up the same treatment: until
// then a radio declining to transmit (demodulating AM, or deafened by its second receiver) reached
// the browser as nothing at all, and `onclose` reported "Transmit connection dropped."
//
// Returns `null` for anything unrecognised, which is deliberate: a later server may send frames this
// build has never heard of, and guessing at them is how a live over gets torn down by a message that
// meant nothing.
// A held-for duration an operator reads at a glance: "12s", "4m 20s", "4h 20m". Exported and pure.
export function formatHeld(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// The sentence a refused operator reads. THE OLD ONE WAS A LIE: "another operator is transmitting"
// is false during a leak — the slot is held by a socket that died and nobody is transmitting at all,
// which is exactly the case ADR 0167 left invisible and ADR 0170 makes reportable.
//
// The server sends the holder, the age and the threshold with the refusal (it cannot be fetched: a
// browser has no channel here but this message). Past `stale_after_s` the phrasing CHANGES rather
// than just adding a number, because "held for 4h 20m" only reads as wrong to someone who already
// knows what normal looks like.
export function describeBusy(msg) {
  const held = formatHeld(msg?.held_s);
  const holder = msg?.holder;
  // An older server, or a claim made without a label: fall back to the pre-ADR-0170 sentence rather
  // than inventing a holder. Saying "held by unknown" would be a second lie in place of the first.
  if (!held || !holder) return "Radio busy — another operator is transmitting.";
  const stale = msg?.stale_after_s;
  if (stale != null && msg.held_s > stale) {
    return (
      `Radio busy — the transmit slot has been held by ${holder} for ${held}, ` +
      "longer than any single transmission should last. Nobody may actually be transmitting."
    );
  }
  return `Radio busy — ${holder} has held the transmit slot for ${held}.`;
}

export function classifyTxMessage(msg) {
  if (msg?.status === "ready") return { status: "talking", ready: true };
  if (msg?.status === "busy") {
    return {
      status: "busy",
      rejected: "busy",
      error: describeBusy(msg),
    };
  }
  if (msg?.status === "refused") {
    return {
      status: "error",
      rejected: "refused",
      // VERBATIM. The backend wrote that sentence to send an operator to a specific place — the
      // Demodulation control, or the radio's own EXIT key — and a client that rewords it throws
      // away the diagnosis the whole interlock exists to produce. The fallback exists only so an
      // alert box is never empty; a refusal with no reason is not expected.
      error: msg.reason || "The radio refused the key-up.",
    };
  }
  return null;
}

// `status` is one of: "idle" | "requesting" | "talking" | "busy" | "denied" | "error".
// `path` selects the TX target: the RF radio (default) or the Mumble channel (ADR 0050); the mic
// capture, 48 kHz framing, and handshake are identical either way.
export function useTxAudio(token, { onAuthError, path = "/audio/tx" } = {}) {
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState(null);

  // Mutable engine state — never triggers a re-render on the per-frame path.
  const ref = useRef(null);
  if (ref.current === null) {
    ref.current = { disposed: true };
  }
  const onAuthErrorRef = useRef(onAuthError);
  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const stopTalk = useCallback(() => {
    const s = ref.current;
    s.disposed = true;
    if (s.ws) {
      s.ws.onclose = null; // teardown close must not trip the status handlers below
      try {
        s.ws.close();
      } catch {
        /* already closing */
      }
      s.ws = null;
    }
    if (s.node) {
      try {
        s.node.disconnect();
      } catch {
        /* already gone */
      }
      s.node = null;
    }
    if (s.source) {
      try {
        s.source.disconnect();
      } catch {
        /* already gone */
      }
      s.source = null;
    }
    if (s.stream) {
      s.stream.getTracks().forEach((t) => t.stop()); // release the mic (turns off the OS indicator)
      s.stream = null;
    }
    if (s.ctx) {
      try {
        s.ctx.close();
      } catch {
        /* already closed */
      }
      s.ctx = null;
    }
    s.ready = false;
    setStatus("idle");
    setError(null);
  }, []);

  const startTalk = useCallback(async () => {
    const s = ref.current;
    if (s.ctx || s.starting) return; // already talking or mid-setup
    s.starting = true;
    s.disposed = false;
    s.ready = false;
    s.rejected = null;
    s.pending = [];
    s.pos = 0;
    s.prev = null;
    setError(null);
    setStatus("requesting");

    // --- mic (gesture-gated permission) ---
    // On an insecure origin (a phone on http://<lan-ip>, not localhost) the browser doesn't expose
    // the mic at all — navigator.mediaDevices is undefined — so getUserMedia would throw a bare
    // TypeError that reads like a code bug. Name the real cause instead (ADR 0039).
    if (!window.isSecureContext || !navigator.mediaDevices) {
      s.starting = false;
      setStatus("denied");
      setError(
        "Transmit needs a secure connection (HTTPS). This page is on plain http — load it over " +
          "https:// to enable the microphone.",
      );
      return;
    }
    let stream;
    try {
      // Radio TX wants the raw mic, NOT browser call-processing: echoCancellation /
      // noiseSuppression / autoGainControl are tuned for video calls and can gate or pump
      // speech, making transmitted audio faint or choppy on the air. Disable all three so the
      // operator's voice reaches the radio unprocessed (ADR 0029 bring-up).
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
    } catch (e) {
      s.starting = false;
      setStatus("denied");
      setError(
        e && e.name === "NotAllowedError"
          ? "Microphone permission denied — allow mic access and try again."
          : `Could not access the microphone: ${e?.message ?? e}`,
      );
      return;
    }
    if (s.disposed) {
      stream.getTracks().forEach((t) => t.stop());
      s.starting = false;
      return;
    }
    s.stream = stream;

    // --- capture graph (mic source -> tx-capture worklet sink) ---
    try {
      const ctx = new AudioContext({ latencyHint: "interactive" }); // DEFAULT rate — resampler is real
      await ctx.resume();
      await ctx.audioWorklet.addModule(new URL("./txWorklet.js", import.meta.url));
      if (s.disposed) {
        try {
          ctx.close();
        } catch {
          /* ignore */
        }
        s.starting = false;
        return;
      }
      s.srcRate = ctx.sampleRate;
      const source = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, "tx-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
      });
      node.port.onmessage = (ev) => onCapturedFrame(s, ev.data);
      source.connect(node);
      s.ctx = ctx;
      s.source = source;
      s.node = node;
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("TX capture failed to start:", e);
      s.starting = false;
      setStatus("error");
      setError(`Audio capture failed: ${e?.message ?? e}`);
      stopTalk();
      return;
    }
    s.starting = false;

    // --- websocket: handshake then stream (no reconnect) ---
    const ws = new WebSocket(txUrl(token, path));
    ws.binaryType = "arraybuffer";
    s.ws = ws;

    ws.onopen = () => {
      if (s.disposed) return;
      // Declare canonical up front (the server 1003s anything else). We resample to match it.
      ws.send(JSON.stringify({ rate: DST_RATE, width: 2, channels: 1 }));
    };

    ws.onmessage = (ev) => {
      if (s.disposed) return;
      // The server's text messages: `ready` to stream, `busy` when the single-talker slot is taken,
      // and `refused` when the radio declined the key-up (ADR 0161). All three are sent explicitly
      // because a browser can't read a close code — it only ever sees 1006.
      let out = null;
      try {
        out = classifyTxMessage(JSON.parse(ev.data));
      } catch {
        /* ignore anything unexpected */
      }
      if (out === null) return;
      if (out.ready) s.ready = true;
      // `rejected` is what stops `onclose` overwriting this with a generic message a moment later —
      // the close is coming, and it must not bury the reason. It also means no retry, for both
      // causes: hammering a busy slot is rude, and hammering a refusal is a transmitter being asked
      // to do something the radio has already said it will not do.
      if (out.rejected) s.rejected = out.rejected;
      setStatus(out.status);
      if (out.error !== undefined) setError(out.error);
    };

    ws.onclose = (ev) => {
      if (s.disposed) return;
      if (s.rejected === "refused") {
        // The reason is already on screen and the close that follows it must not overwrite it. This
        // guard is the whole point of sending the message first: a refused key-up arrives AFTER the
        // ready ack, so without it the `s.ready` branch below would replace a sentence naming the
        // demodulator or the second receiver with "Transmit connection dropped." (ADR 0161).
        teardownKeepStatus(s);
        return;
      }
      if (s.rejected === "busy" || ev.code === WS_TRY_AGAIN_LATER) {
        // Busy was already surfaced from the message (or, as a fallback, the close code). No retry.
        if (s.rejected !== "busy") {
          setStatus("busy");
          setError("Radio busy — another operator is transmitting.");
        }
        teardownKeepStatus(s);
        return;
      }
      if (ev.code === WS_POLICY_VIOLATION) {
        onAuthErrorRef.current?.();
        stopTalk();
        return;
      }
      if (ev.code === WS_UNSUPPORTED_DATA) {
        setStatus("error");
        setError("Server rejected the audio format.");
      } else if (s.ready) {
        // Dropped mid-talk; don't auto-resurrect a keyed transmitter — the operator presses Talk again.
        setStatus("error");
        setError("Transmit connection dropped.");
      } else {
        // Closed before the ready ack and it wasn't busy — a rejected handshake shows as 1006 in a
        // browser, so we can't name the exact cause (e.g. a rotated token). Fail clearly, no retry.
        setStatus("error");
        setError("Could not start transmit — the radio may be unavailable.");
      }
      teardownKeepStatus(s);
    };

    ws.onerror = () => {
      try {
        ws.close(); // let onclose classify + tear down
      } catch {
        /* already closing */
      }
    };
    // `path` MUST stay in the deps: it selects the TX target (RF vs Mumble, ADR 0050). Drop it and
    // `startTalk` freezes to the mount-time path, so Mumble-mode Talk would key `/audio/tx` — the
    // radio — instead of sending to the channel.
  }, [token, path, stopTalk]);

  // Tear down on unmount (e.g. a re-auth drops back to the token gate).
  useEffect(() => stopTalk, [stopTalk]);

  const talking = status === "talking";
  return { status, talking, error, startTalk, stopTalk };
}

// Streaming linear resample (srcRate -> 48k) + Float32→Int16 LE + framed send. Runs per captured
// quantum. Carries one sample of history (`prev`) and a fractional read position (`pos`) across
// quanta so the resampled stream is continuous (no per-quantum clicks). Identity when srcRate == 48k.
function onCapturedFrame(s, chunk) {
  if (s.disposed || !s.ready || !s.ws || s.ws.readyState !== WebSocket.OPEN) return;
  const n = chunk.length;
  if (n === 0) return;
  if (s.prev === null) s.prev = chunk[0];
  const ratio = s.srcRate / DST_RATE; // source samples advanced per output sample
  const virtualAt = (i) => (i <= 0 ? s.prev : chunk[i - 1]); // index 0 == prev, k == chunk[k-1]
  let p = s.pos;
  while (p < n) {
    const i = Math.floor(p);
    const t = p - i;
    const a = virtualAt(i);
    const b = virtualAt(i + 1);
    let v = a + (b - a) * t;
    if (v > 1) v = 1;
    else if (v < -1) v = -1;
    s.pending.push(Math.round(v * 32767));
    p += ratio;
  }
  s.pos = p - n; // carry the fractional remainder into the next quantum
  s.prev = chunk[n - 1];

  while (s.pending.length >= FRAME_SAMPLES) {
    const frame = Int16Array.from(s.pending.splice(0, FRAME_SAMPLES));
    if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(frame.buffer);
  }
}

// Teardown that preserves the status/error already set (used by the onclose classifier). Mirrors
// stopTalk's cleanup minus the state resets.
function teardownKeepStatus(s) {
  s.disposed = true;
  if (s.node) {
    try {
      s.node.disconnect();
    } catch {
      /* gone */
    }
    s.node = null;
  }
  if (s.source) {
    try {
      s.source.disconnect();
    } catch {
      /* gone */
    }
    s.source = null;
  }
  if (s.stream) {
    s.stream.getTracks().forEach((t) => t.stop());
    s.stream = null;
  }
  if (s.ctx) {
    try {
      s.ctx.close();
    } catch {
      /* closed */
    }
    s.ctx = null;
  }
  s.ready = false;
}
