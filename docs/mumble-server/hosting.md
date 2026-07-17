# Hosting the Mumble Server

Part of [running your own Mumble server](README.md) — this page picks the machine it lives on.

Any of these will run a Mumble server for radio-server without breaking a sweat.
Prices as of July 2026; budget VPS pricing and stock move constantly, so treat
them as a starting point rather than a quote.

| Provider | Price | Specs | Locations | Notes |
|---|---|---|---|---|
| **RackNerd** 1GB KVM | $21.99/yr (**~$1.83/mo**) | 1 vCPU, 1GB RAM, 20GB SSD, 3TB transfer | Dallas, Chicago, San Jose, Seattle, LA | Promo rate holds on renewal. Best latency spread for US operators. |
| **BuyVM** SLICE 512 | $24/yr (**$2.00/mo**) | 1 core, 512MB RAM, 10GB SSD, **unmetered** | Las Vegas, New York, Luxembourg | Unmetered transfer is the draw. Chronically out of stock. |
| **InterServer** 1 Slice | **$3.00/mo** | 1 core, 2GB RAM, 40GB SSD, 2TB transfer | US | Most headroom, least value for this workload. |

Pick on **datacenter latency, not specs** — every plan here is overkill, and the
only number an operator will notice is round-trip time. Choose whichever
provider has a POP nearest your operators.

## Why the cheapest tier is enough

Mumble's server does not mix or transcode audio. It forwards Opus packets from
the talker to everyone else, so CPU is a function of packet count rather than
codec work. Measured on `mumble-server 1.5.517` (Ubuntu 24.04), an idle server
holds ~30 MB RSS. BuyVM's 512MB slice has roughly 16x the memory the daemon
actually wants, and the 20GB disk is spent almost entirely on the OS — the
SQLite database for a small server is measured in kilobytes.

Bandwidth is the only resource worth arithmetic, and amateur radio gets the
friendliest possible case: traffic is **half-duplex**, so exactly one station is
transmitting at a time. Egress is therefore `1 talker x N listeners`, not the
`N x N` mesh a gaming server deals with. At Mumble's default 72 kbps per-user
ceiling, 20 listeners is ~1.4 Mbps at peak. Even pathologically assuming
someone keys up 24/7 for a month, that's ~470 GB — inside RackNerd's 3TB cap,
inside InterServer's 2TB, and irrelevant on BuyVM's unmetered port. A real net
runs a rounding error against that.

Practical ceiling: several dozen concurrent listeners on the $1.83/mo box, and
you will hit net etiquette limits long before hardware limits. If a net ever
outgrows this, the fix is bandwidth or a closer datacenter — not a bigger
plan.

## Setup

See [the setup guide](README.md) for the scripted install (`setup-mumble.sh` in this folder),
including the callsign-pattern username policy — the [technical reference](technical-reference.md)
covers it as `MUMBLE_USERNAME_MODE=us`.
