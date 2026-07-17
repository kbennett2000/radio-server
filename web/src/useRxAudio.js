// Live receive audio — the main-thread half of RX playback (ADR 0023).
//
// Owns an AudioContext + the `/audio/rx` binary WebSocket. Nothing is created until `listen()` is
// called from a user gesture (the Listen button): browsers start an AudioContext suspended, so
// autoplay is impossible without one — there is no point trying on load. `listen()` builds the audio
// graph (worklet → gain(mute) → destination), opens the socket, and streams decoded PCM into the
// worklet. `stop()` closes the socket (the server pump is demand-driven, so the last listener
// leaving makes it go idle) and tears the graph down.
//
// The socket mirrors `useEvents`: `?token=` auth, exponential-backoff reconnect, and a 1008 policy
// close (rejected token) bubbles to `onAuthError` instead of retrying. The first message is a JSON
// format header (ADR 0023); we note it but assume canonical 48k/s16le/mono regardless, so an older
// header-less server still plays. During a reconnect the graph stays up and the worklet underruns to
// silence — the same clean-gap path as a TX suspend.
//
// `forceMute` (ADR 0024) is an external override the caller drives while the LOCAL operator is
// transmitting: the server suspends RX during TX, but the ~500ms jitter buffer would still play its
// buffered tail — so we ramp the gain to 0 immediately so you never hear yourself gate in/out.

import { useCallback, useEffect, useRef, useState } from "react";

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 10000;
const WS_POLICY_VIOLATION = 1008; // bad/missing token — do not retry

// Playback volume (ADR 0050 follow-up): a per-browser gain applied before the output. The default
// sits a little below unity so a hot source (near-0 dBFS FM RX or Mumble voice) has headroom and the
// browser's output resample can't overshoot into DAC clipping. It does NOT un-clip audio already
// squared at the capture ADC — that's a capture-level fix (see docs/operating.md) — it only prevents
// playback-stage clipping and lets the operator tame a loud channel. Persisted; mute still wins (0).
const VOLUME_KEY = "radio.rxVolume";
const DEFAULT_VOLUME = 0.85;

function readVolume() {
  try {
    const v = Number.parseFloat(window.localStorage.getItem(VOLUME_KEY));
    return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : DEFAULT_VOLUME;
  } catch {
    return DEFAULT_VOLUME;
  }
}

function rxUrl(token, path) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}?token=${encodeURIComponent(token)}`;
}

// `conn` is one of: "idle" (not listening) | "connecting" | "open" | "reconnecting".
// `path` selects the RX source: the RF radio (default) or the Mumble channel (ADR 0050); the
// transport, 48 kHz format, mute, and reconnect are identical either way.
export function useRxAudio(token, { onAuthError, forceMute = false, path = "/audio/rx" } = {}) {
  const [listening, setListening] = useState(false);
  const [conn, setConn] = useState("idle");
  const [muted, setMuted] = useState(false);
  const [volume, setVolumeState] = useState(readVolume);

  // All mutable engine state lives here so the imperative callbacks don't re-render on every frame.
  const ref = useRef(null);
  if (ref.current === null) {
    ref.current = { disposed: true };
  }
  const mutedRef = useRef(false);
  const forceMuteRef = useRef(forceMute);
  const volumeRef = useRef(volume);
  const onAuthErrorRef = useRef(onAuthError);
  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  // Effective gain = 0 whenever muted OR force-muted (local TX); otherwise the playback `volume`.
  const applyGain = useCallback(() => {
    const s = ref.current;
    if (s.gain && s.ctx) {
      const target = mutedRef.current || forceMuteRef.current ? 0 : volumeRef.current;
      s.gain.gain.setValueAtTime(target, s.ctx.currentTime);
    }
  }, []);

  // Applied live so toggling `forceMute` mid-listen ramps the running graph immediately.
  useEffect(() => {
    forceMuteRef.current = forceMute;
    applyGain();
  }, [forceMute, applyGain]);

  const setVolume = useCallback(
    (v) => {
      const clamped = Math.min(1, Math.max(0, v));
      volumeRef.current = clamped;
      setVolumeState(clamped);
      applyGain();
      try {
        window.localStorage.setItem(VOLUME_KEY, String(clamped));
      } catch {
        /* storage unavailable — the volume still applies for this session */
      }
    },
    [applyGain],
  );

  const stop = useCallback(() => {
    const s = ref.current;
    s.disposed = true;
    if (s.retryTimer) {
      clearTimeout(s.retryTimer);
      s.retryTimer = null;
    }
    if (s.ws) {
      s.ws.onclose = null; // don't let the teardown close schedule a reconnect
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
    if (s.gain) {
      try {
        s.gain.disconnect();
      } catch {
        /* already gone */
      }
      s.gain = null;
    }
    if (s.ctx) {
      try {
        s.ctx.close();
      } catch {
        /* already closed */
      }
      s.ctx = null;
    }
    setListening(false);
    setConn("idle");
  }, []);

  const listen = useCallback(async () => {
    const s = ref.current;
    if (s.ctx || s.starting) return; // already listening or mid-setup
    s.starting = true;
    s.disposed = false;
    setListening(true);
    setConn("connecting");

    // --- audio graph (worklet -> gain(mute) -> speakers) ---
    try {
      const ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
      await ctx.resume(); // the gesture that lets audio actually play
      await ctx.audioWorklet.addModule(new URL("./rxWorklet.js", import.meta.url));
      if (s.disposed) {
        // Stopped while the module was loading.
        try {
          ctx.close();
        } catch {
          /* ignore */
        }
        s.starting = false;
        return;
      }
      const node = new AudioWorkletNode(ctx, "rx-player", {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      const gain = new GainNode(ctx, {
        gain: mutedRef.current || forceMuteRef.current ? 0 : volumeRef.current,
      });
      node.connect(gain).connect(ctx.destination);
      s.ctx = ctx;
      s.node = node;
      s.gain = gain;
    } catch (e) {
      // No worklet / no audio device / blocked context — fail clean back to idle.
      // eslint-disable-next-line no-console
      console.error("RX audio failed to start:", e);
      s.starting = false;
      stop();
      return;
    }
    s.starting = false;

    // --- websocket (with reconnect) ---
    let backoff = BACKOFF_START_MS;
    const connect = () => {
      if (s.disposed) return;
      const ws = new WebSocket(rxUrl(token, path));
      ws.binaryType = "arraybuffer";
      s.ws = ws;

      ws.onopen = () => {
        if (s.disposed) return;
        backoff = BACKOFF_START_MS;
        setConn("open");
      };

      ws.onmessage = (ev) => {
        if (s.disposed) return;
        if (typeof ev.data === "string") {
          // Format header (ADR 0023). We note it but play canonical regardless.
          try {
            s.format = JSON.parse(ev.data).format ?? null;
          } catch {
            /* ignore a malformed header — assume canonical */
          }
          return;
        }
        // Binary PCM frame: interleaved little-endian int16 mono @ 48k (browsers are LE).
        const i16 = new Int16Array(ev.data);
        if (i16.length === 0) return;
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) {
          f32[i] = i16[i] / 32768;
        }
        if (s.node) s.node.port.postMessage(f32, [f32.buffer]);
      };

      ws.onclose = (ev) => {
        if (s.disposed) return;
        if (ev.code === WS_POLICY_VIOLATION) {
          // Token rejected at the handshake — retrying can't fix that. Drop everything and re-gate.
          onAuthErrorRef.current?.();
          stop();
          return;
        }
        setConn("reconnecting"); // graph stays up; the worklet underruns to silence until frames return
        s.retryTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      };

      ws.onerror = () => {
        try {
          ws.close(); // let onclose drive reconnection
        } catch {
          /* already closing */
        }
      };
    };

    connect();
    // `path` MUST stay in the deps: it selects the RX endpoint (RF vs Mumble, ADR 0050), and the
    // socket URL is built from the closed-over value. Drop it and `listen` freezes to the mount-time
    // path, so switching to Mumble mode would keep opening `/audio/rx`.
  }, [token, path, stop]);

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      const next = !m;
      mutedRef.current = next;
      applyGain();
      return next;
    });
  }, [applyGain]);

  // Tear everything down on unmount (e.g. a re-auth drops back to the token gate).
  useEffect(() => stop, [stop]);

  return { listening, conn, muted, volume, setVolume, listen, stop, toggleMute };
}
