# Ninja Woodfire — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Hobby project — published as-is, no support, no warranty.** Primarily a
> Claude-assisted project. See [Status](#status) and [Disclaimer](#disclaimer).

Home Assistant integration for the **Ninja Woodfire Connect Pro XL** (`OG900-EU`).
Two modes, chosen in the setup wizard:

- **Local (Bluetooth)** — no account, no cloud. The grill is found over
  Bluetooth and controlled directly, including through ESPHome Bluetooth
  proxies. Full cook control plus live temperatures and the meat probe.
  See [Local control](#local-control-no-cloud).
- **Cloud** — sign in with your Ninja Kitchen account; the grill is read and
  controlled over SharkNinja's Ayla/AWS backends.

Temperatures follow your Home Assistant unit system (°C/°F). For a matching
Lovelace card, see [ninja-woodfire-card](https://github.com/coxtor/ninja-woodfire-card).

## Features

- **Cook control** — start, stop, skip-preheat, wake. Mode-aware target
  temperature (heat level for grill, °C otherwise), duration, smoke, per-probe
  targets. Changes during a running cook are applied live, keeping heat level,
  probe targets and elapsed time.
- **Live sensors** — grill / air / smoke / meat-probe temperatures, cook and
  preheat progress, end-time, mode, setpoint, lid state, per-probe status & target.
- **Diagnostics** — connection, backend (local / Ayla / AWS), last report,
  Wi-Fi signal (AWS), last successful poll and failed-poll count with the last
  error — readable during an outage.
- **Adaptive polling** — 3 s while cooking, 10 s while idle; auto-reconnect and
  re-auth prompt on cloud token/password changes.
- **Event bus** for automations: `ninja_woodfire_cook_started`,
  `preheat_complete`, `cook_halftime`, `cook_done`, `probe_halfway`,
  `probe_target_reached`, `prompt` (add/flip/get food — also a *Prompt* sensor;
  *add food* is acknowledged by opening and closing the lid on the grill).

## Local control (no cloud)

Local mode talks to the grill directly over Bluetooth Low Energy — no account,
nothing to paste. The grill is identified by its Bluetooth service (`fcbb`) and
the session keys are derived at runtime from its **public** Bluetooth address,
so there is no secret, token or key to enter or store. It runs on Home
Assistant's own Bluetooth stack, so a local adapter **or** an ESPHome Bluetooth
proxy in range of the grill both work.

**Everything works locally:** start/stop and every cooking mode, target
temperature, duration, smoke and per-probe targets, live chamber temperature,
timer/remaining time and status/phase — and the **meat-probe temperature**,
which the grill broadcasts in its BLE advertisement and the integration reads
**passively** (no connection needed). The *Wake* button nudges a grill in
standby back online.

Transport is transparent: a local-only entry does everything over Bluetooth;
the cloud backends are used only if an account was set up.

> Built by reverse engineering the grill's Bluetooth protocol for
> interoperability. **No SharkNinja keys are shipped** — the session keys are
> derived from the grill's own public address.

## Installation

**HACS** (recommended): HACS → Integrations → ⋮ → *Custom repositories* → add
this repo as category *Integration* → install *Ninja Woodfire* → restart.

**Manual**: copy `custom_components/ninja_woodfire/` into your
`config/custom_components/` and restart.

## Setup

*Settings → Devices & services → Add Integration → Ninja Woodfire*, then:

- **Local** — pick your grill from the detected Bluetooth devices (or type its
  address). That is all; there is no token, key or slot to paste.
- **Cloud** — sign in with your Ninja Kitchen account and pick your grill. The
  six "advanced" fields are optional, used only if the bundled cloud identifiers
  stop working (see [Credentials fallback](#credentials-fallback)).

## Node-RED notifications

A ready-to-import Node-RED tab turns the integration's events into phone
notifications (cook started, preheat done, halftime, probe milestones, done,
prompts). It is generic — no private config, no custom subflows. See
[`examples/node-red/`](examples/node-red/).

## SharkNinja AWS backend

SharkNinja is moving grills from the Ayla cloud to its own AWS backend. Grills
migrated by the Ninja app are detected and controlled over AWS automatically;
Ayla is still used for setup and renaming. The *Backend* sensor shows which is
in use. AWS queues commands for a disconnected grill, so a cook is only started
on a connected grill and a start it does not acknowledge within 20 s is
withdrawn and reported as failed.

## Region

EU and NA region defaults are bundled; only EU is exercised by the author.

## Credentials fallback

The integration ships the per-region cloud identifiers used by the official
Ninja Kitchen app. If those ever rotate and cloud login starts failing, extract
a fresh set from your own phone:

```bash
python3 scripts/extract_credentials.py --region EU   # or NA
```

Needs an Android phone with the Ninja Kitchen app, USB debugging, and `adb`. It
captures logcat for ~30 s while you open the app and writes four values to
`ninja_woodfire_credentials.txt`; paste them into the *advanced* config fields,
then delete the file. `--help` for options.

## Automation example — notify when the probe hits target

```yaml
automation:
  - alias: "Steaks ready"
    trigger:
      - platform: event
        event_type: ninja_woodfire_probe_target_reached
        event_data:
          probe_index: 0
    action:
      - service: notify.mobile_app
        data:
          message: "Probe 1 reached {{ trigger.event.data.target }}°C."
```

## Status

Personal hobby project, published as-is: **no support, no warranty, no
guarantee of fitness for any purpose.** Issues and PRs may go unanswered; the
cloud path depends on a third party that can change or break it at any time.
If that is a problem, do not install this.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Unofficial, independent project — **not affiliated with, endorsed by, or
supported by SharkNinja Operating LLC**. "Ninja", "Woodfire", "Connect Pro XL"
and related names are trademarks of their respective owners, used here only
descriptively to identify the device this integration interoperates with. The
integration interacts with a third-party cloud the author does not control; you
are responsible for ensuring your use complies with applicable terms and laws.
Use at your own risk.
