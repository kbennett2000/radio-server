# 0133 — Repeater split/offset TX, and importing a CHIRP channel list

Status: Accepted

Supersedes ADR [0115](0115-channel-presets.md) Decision 1 ("simplex-only v1").

## Context

Presets have been simplex since ADR 0115, and that ADR named the follow-on precisely:

> Split/offset is recorded here as a follow-on arc that would touch the `CatRadio` interface itself
> (a new `set_split`/offset concept every backend must implement), **not** something to smuggle into
> a preset field now. — [0115](0115-channel-presets.md), Decision 1

This is that arc. Simplex presets serve monitoring a repeater's *output*; talking *through* a
repeater needs the radio to transmit offset from where it listens, with the right CTCSS on the way
up. The operator supplied a 37-entry CHIRP export of his local 2 m/70 cm repeaters and wants all of
it live on the station.

Two things make this more than a field on a dataclass. First, the uvk5 backend's `set_frequency`
**refuses to run while keyed** (`backends/uvk5/radio.py:517-521`, ADR 0132) — the write list it
issues ends with the RX reg-0x30 word, which would drop the carrier mid-over. So a split cannot be
"retune, then key"; the TX-leg tune has to happen *inside* the key path, and the un-key has to put
the receiver back. Second, this is the first value in the backend where **a validation-passing typo
radiates**: `set_frequency`'s range is deliberately wide (18 MHz–1.3 GHz) because a wrong RX
frequency is harmless.

## Decision 1 — `set_split(tx_hz)`, a separate capability-gated method

`CatRadio` gains `set_split(tx_hz: int | None)` and `Capability.SET_SPLIT`; `RadioStatus` gains
`tx_frequency`. `apply_preset` calls it exactly the way it already calls `set_tone`/`set_mode` —
only when the backend advertises the capability — so a backend that lacks split is never called
with a parameter it does not understand.

The alternative considered and **rejected** was overloading the existing call as
`set_frequency(hz, *, tx_hz=None)`, on the theory that tuning state is the pair `(rx, tx)` and
should be set atomically the way kv4p's `HostDesiredState` is (ADR 0063). It reads well and it is
wrong here: the repo is full of duck-typed `Radio` doubles with the one-argument signature
(`tests/test_presets.py`, `tests/test_duplex_arbiter.py`), and a keyword they do not accept turns a
`ValueError` path into a `TypeError` — a test asserting HTTP 422 starts getting 500. It also forces
a parameter onto every backend that will never implement split. ADR 0115 named the right shape three
cycles ago; follow it.

**`set_frequency` always clears the split.** Any tune is simplex until something re-arms it. This is
the fail-safe direction and it is deliberate: a stale TX leg surviving a retune would let an
unattended station ID (a timer, not an operator) key a repeater's uplink. Every caller that tunes —
`POST /frequency`, a `ScanEngine` hop, `apply_preset` on a simplex entry — therefore disarms split
as a side effect, which is stated in the docstring, covered by a test, and logged when it actually
discards an armed split.

## Decision 2 — `MockRadio` implements it; kv4p advertises nothing

`Capability.SET_SPLIT` joins `CAT_CAPS`, so `FULL_CAPS` grows and `MockRadio` advertises it — which
means `MockRadio` must **implement** it, not just claim it. `tests/test_capabilities.py` pins
`SHARED | CAT == FULL` and `len(FULL_CAPS) == len(Capability)`; a capability in the set that no
implementation backs is exactly the silent no-op guardrail 3 exists to prevent.

**kv4p does not advertise split this cycle, although the code would be two lines.** Its wire struct
already carries `freq_tx` and `freq_rx` as separate fields (`backends/kv4p/frames.py`) and
`set_frequency` deliberately writes the same value to both. The *code* is trivial; the *proof* is
not — witnessing a kv4p split needs a mirror-image RF stage with the K6 as the receiver, which is a
second bench arc. Advertising a transmit capability that has never keyed real RF on the frequency it
claims is the Part 97 hazard the project guardrails exist to prevent, and this project has already
been burned once this month by a capability that reported success at every layer while nothing
useful left the antenna (ADR 0132). Named follow-on.

## Decision 3 — `tx_frequency` is canonical; `offset` is derived output only

A preset stores the **absolute TX frequency in Hz**. `GET /presets` also reports
`offset = tx_frequency - frequency` because that is how hams read a repeater, but offset is never an
*input*.

Accepting both spellings was considered and rejected. `tests/test_presets.py` uses `{"offset": …}`
as *the* canonical example of an unknown field that must fail loud — making it known would break
that test and invert the module's stated "unknown fields are typos" discipline. The repo's existing
pattern for a derivable value is `slug` in `link/entries.py`: "Computed, never input." One input
spelling also removes three of the four validation branches an alias would need (mutual exclusion,
zero-offset, sign handling).

## Decision 4 — `tone` becomes `tx_tone`, fail-loud, no alias

The field has always *meant* the transmit tone — both backends implement `set_tone` as TX-only
CTCSS, and kv4p's own docstring says so ("repeater access (a TX tone) is the case that matters", ADR
0063). Once `rx_tone` exists beside it, a bare `tone` is genuinely ambiguous. A permissive alias
would be the opposite of this module's posture, so `tone` stays in `_KNOWN_FIELDS` **only** to raise
with the exact rewrite — the `_LEGACY_MUMBLE_KEYS` shape from `config/settings.py`. Migration cost on
the only deployed config is zero: its three presets carry no `tone` key.

## Decision 5 — `rx_tone` is stored, not honoured, and says so out loud

26 of the operator's 37 entries are `TSQL` with a real receive tone, so this is the common case, not
a corner. RX tone squelch is not implemented (RSSI squelch keeps gating receive), and storing a
field the radio ignores is exactly the silent-drop guardrail 3 forbids.

There is no capability to gate on, and inventing a capability string no enum member backs would
pollute a vocabulary the UI parses. So `split_preset_fields` appends a skip entry with an empty
capability and a stated `reason`. The web UI builds its notice from the field name alone, so it
renders "rx_tone not supported on this radio" with no UI change — which is true, of every radio.

## Decision 6 — key-up: tune the TX leg first, in the same frame as the TX enable

The key batch becomes, **only when a split is armed**:

```
(0x38, tx_lo), (0x39, tx_hi), (0x50, 0x3B20), *tone_pairs, (0x30, 0), (0x30, 0xC1FE)
```

TX frequency first, TX enable last, audio only after the read-back confirms. Simplex is unchanged
byte for byte.

**The safety argument is frame atomicity, and it must stay a comment in the code.** One
`_write_registers([...])` call is one `WriteRegisters` frame, and a corrupt frame is dropped
*whole* — the firmware validates CRC over the entire body (`tests/test_uvk5_transport.py`), and the
measured real-world failure on this bench is a whole lost frame, never a partial one
(`scripts/bench/uvk5_tx_regs.py`). So `(0x38/0x39)` and `(0x30, 0xC1FE)` can never land
independently: there is no state in which the transmitter is enabled while the synthesiser still
holds the RX frequency. Any future refactor that splits this batch into two `_write_registers` calls
reintroduces exactly that hazard, which is why the reason is written next to the code and not only
here.

`Dock_ForceTx` does not touch the synthesiser — it deliberately skips `BK4819_SetFrequency` because
the host owns REG-38/39 (ADR 0132) — so the TX leg survives the firmware's PA sequence.

## Decision 7 — key-down is two frames, not one

```
frame 1 (byte-identical to simplex):  (0x30, 0), (0x30, reg30), (0x33, rx33)
frame 2 (only when split was armed):  (0x38, rx_lo), (0x39, rx_hi), (0x33, rx33), (0x30, 0), (0x30, reg30)
```

The tempting single-frame version — `(0x30,0), (0x38), (0x39), (0x30,reg30), (0x33)` — splits the
`0 → reg30` re-latch *around* the frequency write. Every frequency write in this codebase descends
from the pinned client's `BK4819.cs` sequence (ADR 0112), which never writes 0x38/0x39 without the
clear-and-restore pair immediately following, and nothing in the repo establishes that a bare
frequency write moves the synthesiser without it. Frame 2 is literally `set_frequency`'s batch: the
one shape with a citation.

Two properties fall out. The simplex un-key tail is untouched **by construction**, so the exact-tail
assertions in `tests/test_uvk5_radio.py` and `tests/test_uvk5_tot.py` cannot be perturbed by this
change. And the retune lands strictly after `Dock_EndTx` has run, so the window in which the PA is
still ramping down while the synthesiser sits on the repeater's *output* is structurally zero —
regardless of how long `Dock_EndTx` blocks, which nothing in this repo measures.

## Decision 8 — read the retune back, because a dropped tune is invisible

From `scripts/bench/uvk5_tx_regs.py`, measured on this bench: *"a dropped tune is invisible: the API
returns 200, `/status` reports the requested frequency, and the radio sits wherever it was."* Under
a split, "wherever it was" is the repeater's **input**. So after frame 2 the backend reads 0x38/0x39
back once, compares, and on mismatch logs loudly and re-sends once. Non-raising throughout — key-off
must never raise — but never silent, which is the fault class ADR 0132 was entirely about.

## Decision 9 — `tx_hz` gets a guardrail `hz` does not need

`set_frequency` accepts 18 MHz–1.3 GHz because a wrong receive frequency is harmless. A transmit
frequency is not. `set_split` additionally requires:

- `abs(tx_hz - hz) <= 10 MHz` — covers every standard 2 m / 1.25 m / 70 cm offset with room to
  spare, and rejects a stray digit (`146.340` typed as `1463.40`) that would otherwise pass.
- both legs on the same side of the firmware's 280 MHz band split. A crossband split would make
  `_correct_tx_band` flip the reg-0x36 gain byte and the reg-0x33 LNA path within one key cycle — a
  path with zero bench evidence, and ADR 0132 is a long account of what happens when those two
  registers disagree with the carrier.

Both are relaxable later behind an explicit setting. Neither costs a real operator anything: the
widest offset in the operator's own 37-entry list is 5 MHz.

## Decision 10 — the CHIRP importer refuses to guess

`python -m radio_server.chirp` (the repo has no console scripts; entry points are
`python -m radio_server.doctor` / `.enroll`). Pure parse in `radio_server/chirp.py`, thin CLI.

Mapping, implemented exactly:

| CHIRP `Tone` | `tx_tone` | `rx_tone` |
|---|---|---|
| `Tone` | `rToneFreq` | — |
| `TSQL` | `cToneFreq` | `cToneFreq` |
| `Cross` (`Tone->Tone`) | `rToneFreq` | `cToneFreq` |

`Duplex` `-`/`+` with `Offset` in MHz gives `tx = rx ∓/± offset`; frequencies in the file are
repeater **outputs**, i.e. what we listen to.

**Getting TSQL backwards would be silent and total.** 26 of the 37 rows have
`rToneFreq = 88.5 ≠ cToneFreq`; using `rToneFreq` for TSQL — which is correct for `Tone` mode — would
transmit 88.5 Hz at repeaters expecting 100.0 / 103.5 / 107.2 / 123.0 / 141.3, and **none of them
would key**. Two rows have `rTone == cTone` and cannot distinguish the two mappings, so the
regression test pins a discriminating row.

Everything the parser does not understand is **skipped and printed**, never guessed: `Duplex` values
`split` / `off`, `Tone` values `DTCS` / `TSQL-R` / `DTCS-R`, unsupported `Mode`. A `DTCS` row read as
"no tone" would produce a preset that quietly will not key a repeater — the same class of silent
wrongness as the TSQL inversion.

**One assumption is unavoidable and is therefore printed.** A real CHIRP export carries a
`Cross Mode` column; the operator's 9-column paste does not, so `Tone->Tone` cannot be *known* for
the one `Cross` row. When the column is present the parser requires `Tone->Tone`; when it is absent
it assumes it and names the affected row on stdout. An assumption that announces itself is not an
assertion (guardrail 1).

**All frequency arithmetic is integer.** Each MHz field converts independently via
`int(Decimal(text).scaleb(6))`; offsets are subtracted as ints. Naive float arithmetic is not merely
untidy here — `(145.145 - 0.600) * 1e6` is `144545000.00000003`, and across the 2 m/70 cm bands
float subtraction lands 1 Hz low in **2160** cases, which `set_frequency` rejects as off-raster. That
is a hard 422 for exactly one repeater out of forty: the worst possible failure shape.

## Decision 11 — write config the way the file deserves

`save_presets` merges by casefolded name — replacing a matching entry, appending the rest — and
**re-emits the `[[presets]]` array from the merged list**, so the bytes it writes are a pure
function of that list.

Patching entries in place was tried first and abandoned, because it means inheriting tomlkit's
blank-line bookkeeping. Measured on tomlkit 0.15.0: an **appended** entry after the first gets a
leading `"\n"`, a **replaced** one gets `""`, and the leading blank line of a parsed entry is not
recoverable from `trivia.indent`. So a first import and a re-import of the same file differed by one
blank line per entry, three separate ways, each fix moving the discrepancy rather than removing it.
Idempotency is load-bearing here — this writes a config a station reads at startup, and "did that
import change anything?" has to be answerable by diffing — while entry formatting is a nicety.

What that costs, measured rather than assumed: the banner comment above the array, comments on a
`[[presets]]` header, inline comments on a key, and every other section of the file all **survive**.
A standalone comment *line* between two keys inside an entry is **lost** — it belongs to no value, so
nothing carries it. The `comment` key (which a CHIRP `Comment` column becomes) is re-emitted on every
write and always survives.

Casefolded matching is not a nicety: `resolve_presets` enforces case-insensitive name uniqueness, so
a case-sensitive merge would write a duplicate that **prevents the service from starting**. For the
same reason the importer runs `resolve_presets` over the merged list *before* writing, refuses and
exits non-zero if it fails, writes via temp file + `os.replace`, and leaves a `.bak` — `radio.toml`
is gitignored and hand-edited on the bench, with no VCS undo.

New entries **append**. The acceptance runner picks its retune target as "the first preset that is
not the current frequency"; appending keeps that on the bench simplex entry, where prepending would
park the bench on a live local repeater output mid-stage.

## Decision 12 — log the TX audio that un-keying throws away

Not strictly part of split, but found while working in `_key_off` and fixed here because it is three
lines in a method this cycle rewrites anyway.

`SoundCardTxPacer.stop()` clears its queue without draining. That is *correct* RF behaviour (ADR
0093: no long FM tail after the carrier drops) and it is **completely silent**. The consequence: a
caller that keys, hands audio to the *streaming* `transmit()` path — which enqueues and returns
immediately — and then un-keys, transmits **nothing at all**, and nothing anywhere says so. That
shape is what a bench script naturally reaches for, and one of them burned a large part of the
previous cycle producing confident conclusions from transmissions that never happened.

So `_key_off` now counts the bytes the pacer discards and WARNs when non-zero:
`un-keyed with 235200 PCM bytes (2.45 s) still queued — discarded`. One line, once, at the moment
the audio dies.

## Consequences

- Split is real on uvk5 and mock; kv4p reports it honestly as unsupported and says so per preset.
- A preset that a backend cannot fully honour still applies its RX leg and **reports what it
  skipped** — unchanged from ADR 0115, now covering the TX leg and the receive tone.
- The station carries the operator's full channel list, and the Channels card had to grow a filter
  and a scroll bound to survive 40 entries.
- Not proven this cycle, and stated as such: kv4p split; RX tone squelch; whether sub-audible CTCSS
  survives the kv4p receive path well enough to be measured over RF (see the bench section — the
  answer determines whether the acceptance stage hard-checks the tone or reports it).

## Verify on bench

- Read 0x38/0x39/0x33/0x36 idle, keyed, and after un-key on an armed split — the synthesiser must
  hold the TX leg for the whole over and return to the RX leg after.
- The transmitter must be **measurably stronger on the TX leg than the RX leg** with the witness
  moved between them. Absolute silence on the RX leg is not the test: the radios are inches apart
  and 600 kHz of near-field bleed is physics, not a defect.
