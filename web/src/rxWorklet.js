// RX PCM player — the AudioWorklet half of live receive audio (ADR 0023).
//
// Runs on the audio render thread. The main thread decodes each `/audio/rx` binary frame to Float32
// and hands it over via `port.postMessage`; this processor buffers a little (a jitter buffer) and
// feeds it to the output. Transport is postMessage, deliberately NOT SharedArrayBuffer — so the page
// needs no cross-origin-isolation (COOP/COEP) headers and same-origin static serving is untouched.
//
// Continuity over latency (the brief): playback waits until ~150 ms is buffered (priming), then
// drains. An underrun — a scripted RX silence, or the arbiter suspending RX during TX — outputs
// silence (zeros) and re-primes, so a gap is a clean pause, never a buzz or a crash. Latency is
// bounded: holding more than ~500 ms drops the oldest samples (mirrors the server hub's drop-oldest).

const SAMPLE_RATE = 48000; // canonical; the AudioContext is created at this rate, so PCM maps 1:1
const PRIME_SAMPLES = Math.round(0.15 * SAMPLE_RATE); // buffer this much before (re)starting playback
const MAX_SAMPLES = Math.round(0.5 * SAMPLE_RATE); // cap buffered latency; drop oldest beyond
const CAPACITY = SAMPLE_RATE; // 1 s ring, headroom over MAX

class RxPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(CAPACITY);
    this._read = 0;
    this._write = 0;
    this._available = 0;
    this._primed = false; // false until PRIME_SAMPLES buffered; back to false on underrun
    this.port.onmessage = (e) => this._enqueue(e.data);
  }

  _enqueue(chunk) {
    // `chunk` is a Float32Array of mono samples.
    for (let i = 0; i < chunk.length; i++) {
      this._buf[this._write] = chunk[i];
      this._write = (this._write + 1) % CAPACITY;
      if (this._available < CAPACITY) {
        this._available++;
      } else {
        // Ring genuinely full — advance read so we overwrite the oldest, never corrupt.
        this._read = (this._read + 1) % CAPACITY;
      }
    }
    // Bound latency: never hold more than MAX_SAMPLES; drop the oldest down to the cap.
    while (this._available > MAX_SAMPLES) {
      this._read = (this._read + 1) % CAPACITY;
      this._available--;
    }
    if (!this._primed && this._available >= PRIME_SAMPLES) {
      this._primed = true;
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;
    if (!this._primed) {
      out.fill(0); // still priming (or re-priming after an underrun): output silence
      return true;
    }
    for (let i = 0; i < out.length; i++) {
      if (this._available > 0) {
        out[i] = this._buf[this._read];
        this._read = (this._read + 1) % CAPACITY;
        this._available--;
      } else {
        out[i] = 0;
        this._primed = false; // underrun — re-prime before resuming so playback stays continuous
      }
    }
    return true;
  }
}

registerProcessor("rx-player", RxPlayer);
