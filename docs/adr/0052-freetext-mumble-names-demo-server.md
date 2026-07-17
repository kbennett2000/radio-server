# 0052 — Free-text Mumble entry names, per-entry password, shipped demo server

Status: Accepted (amends 0042)

## Context

Three frictions in the multi-server link config (ADR 0042), surfaced by the same re-centering that
motivated ADR 0051:

1. **Names must be slugs** (`[a-z0-9_]{1,32}`). The constraint exists because the name is overloaded
   as four identifiers at once — secret name / env-var suffix (`RADIO_MUMBLE_PASSWORD_<NAME>`), URL
   path segment (`/settings/mumble-servers/{name}/password`), manager dict key, and TTS token. But
   the operator experiences it as "why can't I just call it `Radio Server Demo`?" — the web editor
   even auto-mangles what they type.
2. **Passwords live only in the secrets channel.** Right for private servers; wrong for the one case
   ADR 0042 didn't anticipate: a server whose join password is *deliberately public* (a gate code,
   not a secret). A shipped example can't be complete if its password can't be written down.
3. **Nothing works out of the box.** The example entries are commented out; a fresh install has no
   channel to key into. The project now wants the opposite: run the installer, key `10#`, and you're
   talking to the world on a public demo server.

## Decision

### 1. Display name + derived slug

`name` becomes free text (non-empty, ≤ 64 chars after strip). A `slug` is **always derived** from it
— lowercase, non-alphanumeric runs collapse to `_`, trimmed, capped at 32, empty fails loud — and the
slug takes over every identifier role: manager keys, engine link maps, catalog ids (`link:<slug>`),
secret name `mumble_password_<slug>` / env `RADIO_MUMBLE_PASSWORD_<SLUG>`, and the password
endpoint's path segment (the param is slugified on the way in, so both old slug URLs and display
names resolve). Uniqueness is enforced on slugs, naming both colliding entries.

Backward compatible by construction: a valid ADR-0042 slug slugifies to itself, so every existing
config keeps its secret names, URLs, and ledger ids. Display and TTS use the real name (still
speaking `_` as a space, so legacy slug names read naturally). The web editor drops its client-side
slugify and shows exactly what the operator typed. `slug` is serialized to the UI for keying but
never trusted on input — the resolver recomputes it (the settings editor round-trips whole entries,
so unknown-field rejection would otherwise break saves).

### 2. Optional plaintext `password`, secrets still win

`[[mumble.servers]]` entries gain an optional `password` field. Resolution order at connect (and in
the doctor): the secrets channel (`mumble_password_<slug>` / env) **overrides** the plaintext field;
the field covers the public-gate-code case. The secrets posture for private servers is unchanged and
remains the documented recommendation. Consequence honored throughout: `GET /settings/mumble-servers`
must return `password` so the editor's whole-list PUT doesn't erase it (unlike secrets, which stay
write-only with `password_set` presence flags).

### 3. A live demo entry ships in the example, and defaults renumber

`radio.toml.example` gains an **active** entry — name `Radio Server Demo`, host `104.168.125.41`,
port `64738`, `dtmf = "10"`, password `github.com/kbennett2000/radio-server` (public by design) —
emitted by the example generator, so the file stays byte-locked to `render_example()`. A fresh
config copied from the example can key `10#` immediately.

`mumble.disconnect_dtmf` default changes `"73"` → `"98"`, pairing with `99#` logout and the two-digit
shipped keypad (ADR 0051). A deployment that relied on the implicit `73` and has link entries changes
behavior on upgrade — called out in the PR; an explicit `disconnect_dtmf = "73"` keeps the old combo.

## Consequences

- Operators name servers in plain language everywhere (config, settings editor, TTS confirmations,
  link card); the slug becomes an implementation detail.
- The shipped keypad story completes: `01#` ID, `02#` time, `10#` demo link, `98#` link off, `99#`
  logout — no collisions (validated at startup as before).
- The demo host is a bare IP; if it moves, the example goes stale until regenerated. Accepted — the
  entry is operator-editable config, not code.
- All demo connections present as `<CALLSIGN> (radio-server)`; callsign-less bench setups share the
  default nick and a server may reject duplicates. Cosmetic; noted, not solved.
