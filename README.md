# TNMM Demo UI

A Python application that acts as the **server endpoint** for the BlueStation MS
external interface (`bluestation-ms-interface-1`) and gives an operator a
**colour touchscreen radio UI** (styled after Motorola TETRA terminals) to watch
registration/service status, stream telemetry, and send TNMM + management
commands.

It ships with a **fake stack simulator** so the whole thing demos on one laptop
with **zero hardware**.

## The counter-intuitive bit

**The stack is the WebSocket _client_; this app is the WebSocket _server_.** The
stack dials *out* to us on two ports:

| Channel   | This app listens on | Subprotocol                 | Traffic                          |
|-----------|---------------------|-----------------------------|----------------------------------|
| Control   | `9102`              | `bluestation-control-v1`    | UI→stack commands, stack→UI resp |
| Telemetry | `9101`              | `bluestation-telemetry-v1`  | stack→UI events (receive-only)   |

Messages are **JSON encoded as UTF-8 inside _binary_ WebSocket frames**, using
serde's externally-tagged enum shape `{"Variant": {..}}`. Keepalive is WS
ping/pong. See [`PROTOCOL.md`](PROTOCOL.md) for the full message catalog.

## Quick start (no hardware)

```bash
python -m pip install -r requirements.txt
python -m app --simulate
```

Then open <http://127.0.0.1:8080>. The fake stack auto-connects both channels.
The terminal home screen shows the radio's own identity (ISSI + home MCC/MNC) as
soon as the MS reports it. Tap **Register** → state flips to `Registered` and the
confirm/service telemetry streams in. Kill and restart the fake stack (`--chaos`
does it for you) and watch the channels drop and recover.

```bash
python -m app --simulate --chaos   # randomly drops channels + injects failures
```

## Running the pieces separately

```bash
# Terminal 1 — the demo UI (servers + dashboard)
python -m app

# Terminal 2 — the fake stack (WS client) pointed at the UI
python fake_stack.py --control ws://127.0.0.1:9102 --telemetry ws://127.0.0.1:9101 [--chaos]
```

## Pointing the real stack at this app

Add to your `config-ms.toml`:

```toml
[telemetry]
host = "127.0.0.1"   # address of the machine running this demo UI
port = 9101
use_tls = false

[command]
host = "127.0.0.1"
port = 9102
use_tls = false
```

Add matching `username`/`password` on both sides for Basic auth; set
`use_tls = true` + a `ca_cert` PEM for `wss`.

## Configuration

Edit [`config.toml`](config.toml) (host/ports/credentials/TLS), or override with
env vars (`TNMM_CONTROL_PORT`, `TNMM_TELEMETRY_PORT`, `TNMM_DASHBOARD_PORT`, …)
or CLI flags (`--control-port`, `--telemetry-port`, `--dashboard-port`,
`--config path.toml`). Defaults: `127.0.0.1`, control `9102`, telemetry `9101`,
dashboard `8080`, no auth, no TLS.

The radio **identity is not configured** — it is reported by the MS. The only
registration knob is the operator preference in `[registration]`:

```toml
[registration]
registration_type = "RegistrationToIndicatedCell"
```

## Features

- **Two WS servers** accepting the correct per-channel subprotocol, binary JSON
  frames, tolerant of stack reconnects (per-channel connection status shown).
- **Colour touchscreen UI** styled after a rugged TETRA radio: a home status
  screen with icon tiles and dedicated sub-screens (Register, Deregister, Status,
  Messages, Groups, Config, Info) — large touch targets, no desktop form clutter.
- **Identity comes from the MS, not config.** The radio's ISSI and home MCC/MNC
  are read live from `MsRuntimeState` (`GetState`) — the MS is the source of
  truth. Register/Deregister need no operator input (a radio can only act on its
  own identity); the buttons stay disabled until the MS reports identity.
- **Group management (TNMM attach/detach)**: the Groups screen presents the
  **codeplug talkgroup tree** (folders + talkgroups read from the MS config,
  `config_version 0.7`). Tap a talkgroup to **select/switch** to it
  (`DetachTheCurrentlyActiveGroupIdentities` + `Attachment`), **+** to add a
  scanning group (`Amendment` + `Attachment`), **−** to drop one. Codeplug
  `class_of_usage` (on-air 0..7) is mapped to TNMM `ClassOfUsage(N+1)`. Arbitrary
  GSSIs can still be attached by hand. The home idle screen shows the selected
  **talkgroup name** and a **SCAN** indicator when more than one group is attached.
- **Live status**: per-channel connection state; latest `MsRuntimeState`
  (polled via `GetState` every ~2 s), including serving-cell `rssi_dbfs` (drives
  the signal bars); a scrolling, timestamped telemetry event log; interface
  schema version.
- **Two-way voice** (ACELP): downlink RX decodes `MsSpeechFrame` telemetry to
  PCM the browser plays; uplink TX captures the mic, encodes to ACELP and sends
  `MsUplinkSpeech`. Push-to-talk floor arbitration for simplex and group calls
  (ON AIR / floor indicators), and hands-free auto-streaming for full-duplex
  individual calls. Works over `http://localhost` with no TLS; TLS is only
  needed for mic access over the LAN.
- **Command controls** with `handle` correlation: Register, Deregister, group
  attach/detach, Get State / Config / Interface Version, Set Config + Apply
  Config with a `restart_required` banner.
- **Secrets**: `GetConfig` returns TOML with secrets redacted to `********`;
  leave the sentinel untouched in `SetConfig` to preserve the on-disk secret.

## Tests

```bash
python -m pytest
```

`tests/test_roundtrip.py` spins the servers + fake stack in-process and asserts
a full register→confirm→deregister round-trip over the **real binary/JSON wire
path**, a group attach/detach round-trip, a talkgroup **switch** round-trip
(`DetachTheCurrentlyActiveGroupIdentities`), codeplug parsing from the
`config_version 0.7` TOML, dormant-primitive rejection, and a config round-trip.

## Layout

```
app/
  __main__.py       CLI entry (--simulate, --chaos, port overrides)
  config.py         config.toml / env / CLI loading
  protocol.py       message builders + enum vocabularies (no I/O)
  hub.py            shared state, handle correlation, browser broadcast
  stack_servers.py  control + telemetry WS servers + GetState poller
  dashboard.py      browser HTTP + WS server
  static/index.html colour touchscreen radio UI (single page)
fake_stack.py       hardware-free simulator (WS client)
tests/              in-process wire round-trip
native/             ACELP codec wrappers + build glue (ETSI source obtained
                    separately — see native/ETSI-CODEC.md)
config.toml         defaults
PROTOCOL.md         wire protocol reference
```

## Requirements

Python 3.11+ and the `websockets` library (pure-Python) for the core
server/dashboard. Two features pull in optional extras, both listed in
`requirements.txt`:

- **TLS dashboard** (`--tls`): `cryptography`, used only to mint a cached
  self-signed cert when you don't supply `--tls-cert`/`--tls-key`. A secure
  (HTTPS) origin is what lets the browser grant microphone access over the LAN.
- **Voice (up/downlink)**: the native ETSI ACELP codec, compiled on demand with
  `clang`. The ETSI reference source is **not shipped here** — see below.

## Voice & TLS

For push-to-talk / duplex audio the browser needs both a secure origin and the
codec:

```bash
# From the same machine: http://localhost:18080 is already a secure context,
# so the mic works over plain HTTP — no TLS needed.
python -m app --simulate

# Over the LAN (any other host/IP): browsers require HTTPS for mic access, so
# serve TLS and accept the one-time self-signed warning.
python -m app --simulate --tls
```

Notes:
- Browser AEC/NS/AGC are intentionally disabled (they forced Chrome's
  "communications" audio pipeline and made downlink choppy) — **use a headset**.
- The ACELP shared library builds itself the first time audio is used, provided
  `clang` is on `PATH` and the ETSI sources are present in `native/etsi/`.

## License & third-party code

This project's own code is licensed under the **Apache License 2.0** — see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Reuse is permitted provided you
keep the copyright/attribution notices.

**The ETSI EN 300 395-2 TETRA ACELP speech codec is not included** and is *not*
covered by that license: it is ETSI-copyrighted and must be obtained separately
and placed in `native/etsi/`. See [`native/ETSI-CODEC.md`](native/ETSI-CODEC.md)
for the required files and build details. Confirm your own redistribution rights
before publishing any fork that bundles those sources or the libraries built
from them.
