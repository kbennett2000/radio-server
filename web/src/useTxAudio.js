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

function txUrl(token) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/audio/tx?token=${encodeURIComponent(token)}`;
}

// `status` is one of: "idle" | "requesting" | "talking" | "busy" | "denied" | "error".
export function useTxAudio(token, { onAuthError } = {}) {
  const [status, setStatus] = useState("idle");
  const [level, setLevel] = useState(0); // 0..1 peak of the OUTGOING mic audio
  const [error, setError] = useState(null);

  // Mutable engine state — never triggers a re-render on the per-frame path.
  const ref = useRef(null);
  if (ref.current === null) {
    ref.current = { disposed: true, levelTarget: 0, levelDisplay: 0 };
  }
  const onAuthErrorRef = useRef(onAuthError);
  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const stopTalk = useCallback(() => {
    const s = ref.current;
    s.disposed = true;
    if (s.raf) {
      cancelAnimationFrame(s.raf);
      s.raf = null;
    }
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
    s.levelTarget = 0;
    s.levelDisplay = 0;
    setLevel(0);
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

    // --- level meter (peak of outgoing audio, attack/decay on rAF) ---
    const tick = () => {
      if (s.disposed) return;
      const target = s.levelTarget;
      s.levelDisplay = target > s.levelDisplay ? target : s.levelDisplay * 0.85;
      s.levelTarget = 0;
      setLevel(s.levelDisplay < 0.001 ? 0 : s.levelDisplay);
      s.raf = requestAnimationFrame(tick);
    };
    s.raf = requestAnimationFrame(tick);

    // --- websocket: handshake then stream (no reconnect) ---
    const ws = new WebSocket(txUrl(token));
    ws.binaryType = "arraybuffer";
    s.ws = ws;

    ws.onopen = () => {
      if (s.disposed) return;
      // Declare canonical up front (the server 1003s anything else). We resample to match it.
      ws.send(JSON.stringify({ rate: DST_RATE, width: 2, channels: 1 }));
    };

    ws.onmessage = (ev) => {
      if (s.disposed) return;
      // The server's one text message is the handshake result: `ready` to stream, or `busy` when the
      // single-talker slot is taken (it's sent explicitly because a browser can't read the pre-accept
      // 1013 close code — it only sees 1006).
      try {
        const msg = JSON.parse(ev.data);
        if (msg.status === "ready") {
          s.ready = true;
          setStatus("talking");
        } else if (msg.status === "busy") {
          s.rejected = "busy";
          setStatus("busy");
          setError("Radio busy — another operator is transmitting."); // do NOT retry-hammer
        }
      } catch {
        /* ignore anything unexpected */
      }
    };

    ws.onclose = (ev) => {
      if (s.disposed) return;
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
  }, [token, stopTalk]);

  // Tear down on unmount (e.g. a re-auth drops back to the token gate).
  useEffect(() => stopTalk, [stopTalk]);

  const talking = status === "talking";
  return { status, talking, level, error, startTalk, stopTalk };
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
  let peak = 0;
  while (p < n) {
    const i = Math.floor(p);
    const t = p - i;
    const a = virtualAt(i);
    const b = virtualAt(i + 1);
    let v = a + (b - a) * t;
    if (v > 1) v = 1;
    else if (v < -1) v = -1;
    s.pending.push(Math.round(v * 32767));
    const abs = v < 0 ? -v : v;
    if (abs > peak) peak = abs;
    p += ratio;
  }
  s.pos = p - n; // carry the fractional remainder into the next quantum
  s.prev = chunk[n - 1];
  if (peak > s.levelTarget) s.levelTarget = peak;

  while (s.pending.length >= FRAME_SAMPLES) {
    const frame = Int16Array.from(s.pending.splice(0, FRAME_SAMPLES));
    if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(frame.buffer);
  }
}

// Teardown that preserves the status/error already set (used by the onclose classifier). Mirrors
// stopTalk's cleanup minus the state resets.
function teardownKeepStatus(s) {
  s.disposed = true;
  if (s.raf) {
    cancelAnimationFrame(s.raf);
    s.raf = null;
  }
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
