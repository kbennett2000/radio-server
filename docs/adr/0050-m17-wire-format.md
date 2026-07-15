# 0050 — The M17 wire format: base-40 callsigns, reflector control packets, and the stream frame

Status: Accepted

## Context

This is the second cycle of the M17 backend arc. Cycle 54 (ADR 0049) built the Codec2 seam —
the *audio payload* that M17 carries. This cycle builds the *wire format* that carries it: the
base-40 callsign address encoding, the mrefd reflector control packets, and the M17 stream frame
with its embedded Link Setup Frame (LSF). Like ADR 0041's protocol cycle, this cycle is **pure**:
stdlib-only byte manipulation, no socket, no `Link` backend, no lifecycle. The UDP client and the
`M17Link` binding that consumes these functions are separate later cycles. The wire format lands
alone so it can be proven byte-exact against the spec before any socket exists to obscure a
framing bug.

Unlike the hardware facts elsewhere in this project, the M17 protocol is **published**, so there
is no excuse to work from memory (guardrail 1). Every byte layout in the implementation was read
this cycle from primary sources and encoded as a test:

- The **M17 specification** (`M17-Project/M17_spec`, the `M17_spec.tex` document) — the base-40
  alphabet and its encoder/decoder, the LSF field table, and the CRC-16 definition together with
  its published test vectors.
- **mrefd** (`n7tae/mrefd`) — its `Packet-Description.md` for the reflector control-packet set,
  and its `packet.cpp` for the exact byte offsets of the 54-byte stream frame.

**Licensing.** The M17 specification document and mrefd are both GPL-licensed. This project is
MIT. That is fine here for the same reason it is fine to speak HTTP without linking a GPL server:
we implement the protocol *from* its published description and exchange its bytes on the wire — we
do **not** link mrefd, and we do **not** copy specification text or code into the repository. What
crosses the boundary is the idea (a field is six bytes here, big-endian), never the expression.
The prose in this repo, including this ADR, is our own.

## Decision

- **A pure `radio_server/link/m17/` subpackage, stdlib-only.** It sits beside the untouched
  `base.py` / `mock.py` / `factory.py`. It imports nothing from `radio_server` at all — the
  payload it frames is opaque `bytes`, so it does not even need the `..audio` frame types. This
  keeps it a clean leaf: byte-in, byte-out, no I/O, no sockets. A test AST-parses the subpackage
  and asserts no module imports `socket` or anything from `radio_server`, encoding "pure,
  stdlib-only, no socket" as a checked invariant rather than a promise.

- **Base-40 callsign encode/decode, exact round-trip.** A callsign of up to nine characters from
  the 40-symbol alphabet (space, `A`–`Z`, `0`–`9`, `-`, `/`, `.`) encodes to a 48-bit value,
  emitted big-endian as six bytes; decoding inverts it. The reserved all-zero value is the empty
  callsign; the standard range `0x000000000001`–`0xEE6B27FFFFFF` is base-40 decodable; the
  extended range above it (through `0xFFFFFFFFFFFE`) and the `0xFFFFFFFFFFFF` BROADCAST value are
  not decodable to a base-40 string, so `decode_callsign` returns `None` for them and exposes
  `EMPTY` / `BROADCAST` as named constants for callers that need to recognise those addresses.

- **Fail loud on an unencodable callsign — a deliberate divergence from the spec's reference
  encoder.** The specification's example C encoder is permissive: it silently maps any character
  outside the alphabet to space and folds lowercase to uppercase. That suits a decoder that must
  never crash on hostile RF, but it is the wrong default for *our* encoder, which is called with
  a locally-configured station callsign. Coercing `"W1@X"` into `"W1 X"` would put a silently
  wrong identity on the air. So `encode_callsign` **raises** `CallsignError` on any character
  outside the alphabet and on a length over nine, per this project's fail-loud house style
  (guardrail 3) and the cycle's explicit instruction. This is the one place we intentionally do
  not mirror the reference code, and it is recorded here so a later cycle does not "fix" it back
  into silent coercion.

- **The reflector control set: CONN, ACKN, NACK, DISC, PING, PONG, and LSTN.** Each is a
  four-byte ASCII magic optionally followed by a six-byte sender callsign and, for the link
  requests, a one-byte module letter. We implement the client-side forms (the 4/10/11-byte
  packets); the 37-byte reflector-to-reflector interlink forms are a reflector's concern, not a
  client's, and are out of scope. **LSTN is not optional.** It is the protocol-level listen-only
  request — the same shape as CONN but with a more permissive callsign — and it is exactly what
  makes a zero-credential "listen before you talk" tier possible (the `LISTEN_ONLY` capability of
  ADR 0041). It is built and parsed here as a first-class packet, not left for later.

- **The 54-byte stream frame, including the LSF.** The `M17 ` frame is magic (4) · StreamID (2) ·
  DST (6) · SRC (6) · TYPE (2) · META (14) · frame number (2) · payload (16) · CRC (2). The
  middle 28 bytes (DST · SRC · TYPE · META) are the LSF contents; the frame carries a single CRC
  over its first 52 bytes rather than the LSF's own trailing CRC. The frame number's top bit
  marks the last frame of a transmission; its low fifteen bits are the running index. The
  16-byte payload — two Codec2 3200 frames — is treated as **opaque bytes** this cycle; wiring it
  to the ADR-0049 codec is the `M17Link` cycle's job.

- **The CRC is the spec's non-standard CRC-16.** Polynomial `0x5935`, initial value `0xFFFF`,
  most-significant-bit-first, with neither input nor output reflected. It lives in its own module
  and is checked against the four published test vectors, so a subtle bit-order error surfaces in
  a unit test rather than as a reflector silently dropping every frame.

- **Malformed input: parse returns `None`, build/encode raises — split by trust, applied
  consistently.** A reflector is an untrusted network peer, and a truncated or hostile datagram
  must never reach the keying path as a half-parsed object or an exception mid-decode. So every
  parse function (`parse_control`, `parse_stream`) returns `None` on *any* malformation — wrong
  length, unknown magic, or a failed CRC — and the caller simply drops it. Conversely, the build
  and encode functions operate on *local* input, where bad data is a programming error, not an
  attack; they raise by name (`CallsignError`, or a `ValueError` naming the offending field) so
  the mistake is loud. The rule is one sentence: untrusted inbound is dropped to `None`,
  local outbound fails loud.

- **The LSF source callsign is the talker, surfaced here.** M17 has no directory to ask "who is
  transmitting" — the answer rides *inside every stream frame* as the LSF SRC address (ADR 0041's
  reason M17 needs no directory). `parse_stream` decodes that address and exposes it as
  `StreamFrame.src`. This parser is the single point where "who is talking right now" becomes
  available; a later `M17Link` will map `StreamFrame.src` → `Station(callsign=...)` →
  `LinkStatus.talker`. Pinning it here is what makes the no-directory design cost so little: the
  identity was in the stream all along, and this is where we read it out.

## Consequences

- The M17 framing is proven byte-exact — against the spec's golden callsign vectors, its four CRC
  test vectors, and mrefd's own field offsets — before any socket exists. The UDP client that
  follows is ordinary datagram plumbing over a known-good codec of packets.
- No new dependency and no test skip-gate: the subpackage is stdlib-only, so its tests run
  unconditionally in the default suite. This is unlike the Codec2 tests (ADR 0049), which gate on
  a system library; and unlike Codec2, the wire format is *exact*, so the tests assert exact bytes
  rather than lossy geometry.
- The talker seam is open but not wired: `StreamFrame.src` exists and is tested, but nothing yet
  feeds `LinkStatus.talker` — that is the backend cycle. `base.py` / `mock.py` / `factory.py` are
  untouched.
- **Scope limits, deliberate:** client-side control packets only (no 37-byte interlink forms);
  the stream payload is opaque (no Codec2 wiring); no sockets, no `Link` backend, no UI. The
  M17/mrefd UDP client, binding these builders/parsers and the ADR-0049 codec into a real `Link`
  behind `create_link`, is the next cycle of the arc.
