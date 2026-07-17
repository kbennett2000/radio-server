# Your own voice server, for about $2 a month

radio-server needs a meeting place on the internet — a **Mumble server**. Think of it as a
conference call that's always open: your station links into it, your friends' stations link into
it, and anyone can also join from a free Mumble app on their phone or computer. Whoever's in the
channel can talk; everyone else hears them.

You don't have to run one. But if you do, it's **your** club's frequency, in a way: your name on
the door, your channels, your rules (like "callsigns only"). And it's genuinely cheap and
genuinely easy — the whole thing is one rented mini-computer and one script.

## Do I need one?

Be honest with yourself here — most people can skip this page at first:

- **Just you?** No. radio-server comes pointed at the demo server, and there are busy public
  Mumble servers to explore. Come back if you ever want a quiet place of your own.
- **You and a few friends?** Maybe. Start on the demo or a public server; if you find yourselves
  meeting up regularly, $2 a month buys you a private room with your names on it.
- **A radio club?** Probably yes. A club server gives you a place for nets, a channel per
  interest group, and callsign-only usernames — with no repeater site, no coordination, and no
  digital-mode alphabet soup.

## What it costs and where it lives

You rent a small cloud computer (a "VPS") from a hosting company — the going rate is **about $2 a
month**, and the cheapest plan is honestly more than enough: Mumble is very light, and with
radios only one person talks at a time. [Hosting options and costs](hosting.md) compares a few
good providers and explains why the small plan is plenty.

## Setting it up

Four steps, and the script does the hard parts:

1. **Rent the box.** Pick a provider from [hosting.md](hosting.md), choose their smallest plan
   with **Ubuntu** as the operating system, and pick the datacenter nearest your members. They'll
   email you the address of your new machine and how to sign in.
2. **Copy the script to it.** From this folder, [`setup-mumble.sh`](setup-mumble.sh) is the whole
   installer. Copy it to your new machine (the provider's welcome email shows how to connect).
3. **Run it.** Type `sudo ./setup-mumble.sh` and answer a few plain questions: what to call your
   server, a join password, and whether usernames must be callsigns (for a club: yes). That's
   it — the script sets up everything, turns the server on, and prints what to tell your members.
   It's safe to run again any time you want to change an answer.
4. **Point everyone at it.** Add the server in radio-server's settings (name, address, password —
   give it a DTMF code and your handheld can link to it over the air), and have members install
   the free [Mumble app](https://www.mumble.info/) on their phones or computers and join with the
   same address and password.

## After it's up

Three one-time clicks make you the admin — do them right after setup:

1. Join from your Mumble app and **register** your name (right-click your name → Register).
2. Reconnect as `SuperUser` (the script printed that password), right-click the server name →
   Edit → Groups → `admin`, and add yourself.
3. Reconnect as yourself. You're the admin now — add channels by right-clicking the server name →
   Add (leave "Temporary" unchecked).

The [technical reference](technical-reference.md) walks through the same steps in more detail,
plus troubleshooting.

## The details, when you want them

- [Hosting options and costs](hosting.md) — providers, prices, and why the cheap plan is enough.
- [Technical reference](technical-reference.md) — everything the setup script does and why, all
  the settings, and troubleshooting.
- [`setup-mumble.sh`](setup-mumble.sh) — the installer itself.
- [`check-username.sh`](check-username.sh) — test names against your callsign policy before
  inviting people.
