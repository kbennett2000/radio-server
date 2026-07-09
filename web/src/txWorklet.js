// Mic capture sink — the AudioWorklet half of TX (ADR 0024). The inverse of rxWorklet.js.
//
// rxWorklet is a source that renders buffered PCM to the output; this is a sink that forwards each
// captured input quantum to the main thread, where the resample-to-48k + Float32→Int16 conversion
// and the WebSocket send happen (mirroring how RX does the Int16→Float32 decode on the main thread).
// Capture has no render-thread timing need, so this stays dumb — no buffering, just forward frames.

class TxCapture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    // No input connected yet (or the source ended): nothing to forward, but keep the node alive.
    if (!input || !input[0]) return true;
    // Mono: take channel 0. The graph reuses this buffer each quantum, so post a COPY, not the view.
    this.port.postMessage(input[0].slice());
    return true;
  }
}

registerProcessor("tx-capture", TxCapture);
