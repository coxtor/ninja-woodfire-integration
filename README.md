# Ninja Woodfire — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Hobby project — published as-is, no support, no warranty.**
> See [Status & support](#status--support) and [Disclaimer](#disclaimer)
> below before installing.

> **Note:** This is primarily a Claude-assisted project.

Home Assistant integration for the Ninja Woodfire Connect Pro XL
outdoor grill. It offers two modes, chosen in the setup wizard:

- **Cloud** — sign in with your Ninja Kitchen account; the grill is
  read and controlled over SharkNinja's Ayla/AWS backends. No local
  network access to the grill is required.
- **Local (Bluetooth)** — no account at all. The grill is found over
  Bluetooth and controlled directly, including through ESPHome
  Bluetooth proxies. See [Local control (no cloud)](#local-control-no-cloud).

Temperatures are shown in whatever unit your Home Assistant system
uses (°C or °F) — there is no separate unit switch.

For a custom Lovelace card, see [ninja-woodfire-card](https://github.com/coxtor/ninja-woodfire-card).

## Features

- Live sensors: grill / air / smoke / probe temperatures, cook progress,
  preheat progress, end-time, active mode, current setpoint, lid state,
  per-probe status & target.
- Cook controls: start, stop, skip-preheat. Mode-aware temperature
  number (heat-level for grill, °C for everything else). Duration,
  smoke, per-probe targets — changes during a running cook are applied
  live and keep heat level, probe targets and elapsed time.
- Local Bluetooth control: the same cook controls and live chamber
  temperature, timer / remaining time and status / phase, with no
  cloud account. The meat-probe temperature is read passively from
  the grill's BLE advertisement (broadcast in cleartext, no
  connection needed) and exposed as a sensor.
- Cloud diagnostic entities: connection, backend, last report, Wi-Fi
  signal (AWS only), last successful poll and failed polls with the
  last error — readable during an outage.
- Adaptive polling — every 3 s while cooking, 10 s while idle.
- Auto-reconnect on token expiry; re-authentication prompt when the
  password changes.
- HA event-bus events for automations:
  - `ninja_woodfire_cook_started`
  - `ninja_woodfire_preheat_complete`
  - `ninja_woodfire_cook_halftime`
  - `ninja_woodfire_cook_done`
  - `ninja_woodfire_probe_halfway` (halfway from the probe's start
    temperature to its target)
  - `ninja_woodfire_probe_target_reached`
  - `ninja_woodfire_prompt` — the grill asks for something (`prompt`:
    `addfood`, `flipfood`, `done`, `getfood`); also exposed as the *Prompt*
    sensor. *Add food* is acknowledged on the grill by opening and closing
    the lid; there is no remote command for it.

## Tested models

Tested only on the Woodfire Connect Pro XL EU (`OG900-EU`).
Other OG9-series models fall back to the same capability set and
*may* work — unverified. NA region endpoints are bundled but were
never tested by the author.

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → *Custom repositories*
2. Add this repository, category *Integration*
3. Install *Ninja Woodfire*
4. Restart Home Assistant

### Manual

Copy `custom_components/ninja_woodfire/` into your Home Assistant
`config/custom_components/` directory, then restart.

## Setup

1. *Settings → Devices & services → Add Integration → Ninja Woodfire*
2. Choose **Cloud** or **Local**:

   **Cloud**
   1. Sign in with your Ninja Kitchen account
   2. Pick your grill

   **Local**
   1. Pick your grill from the detected Bluetooth devices (or type its
      Bluetooth address)

   That is all there is to local setup — there is no token, key or slot
   to paste.

In cloud mode, the six "advanced" fields in the form are optional and
used only if the bundled defaults stop working — see [Fallback:
regenerating credentials](#fallback-regenerating-credentials) below.

## Local control (no cloud)

Local mode talks to the grill directly over Bluetooth Low Energy, with
no Ninja Kitchen account and nothing to paste. The grill is identified
by its Bluetooth service (`fcbb`); the session keys are derived at
runtime from the grill's public Bluetooth address (MAC), so there is no
secret, token or key to enter or store.

It runs on Home Assistant's own Bluetooth stack, so a local Bluetooth
adapter *or* an ESPHome Bluetooth proxy within range of the grill both
work.

**Requirements**

- A Bluetooth adapter on the Home Assistant host, or an ESPHome
  Bluetooth proxy in range of the grill.
- The grill powered on and within Bluetooth range of that adapter/proxy.

**What works locally**

- Start / stop a cook, all cooking modes.
- Target temperature, duration, smoke and per-probe targets.
- Live chamber temperature, timer / remaining time, status / phase.
- Meat-probe temperature, read passively from the grill's BLE
  advertisement. The grill broadcasts it in cleartext, so this works
  without any connection and shows up as a sensor.

Transport choice is transparent: a local-only entry does everything
over Bluetooth, and the cloud backends are used only if an account was
set up.

> **Known limitation:** the local chamber-temperature scaling is still
> being finally verified; the passive probe reading is confirmed
> against the grill display.

### On reverse engineering

Local control was built by reverse engineering the grill's Bluetooth
protocol for interoperability. No SharkNinja keys are shipped with the
integration — the session keys are derived at runtime from the grill's
public Bluetooth address.

## SharkNinja AWS backend

SharkNinja is moving grills from the Ayla cloud to its own AWS backend.
Grills migrated by the Ninja app are detected automatically and read and
controlled over AWS; Ayla is still used for setup and renaming. The
*Cloud backend* diagnostic sensor shows which one is in use.

AWS queues commands for a grill that is not connected and delivers them
when it reconnects. A cook is therefore only started on a connected grill,
and a start the grill does not acknowledge within 20 seconds is withdrawn
and reported as failed.

## Region

EU and NA region defaults are bundled. Only EU is exercised by the
author.

## Fallback: regenerating credentials

The integration ships with the per-region cloud identifiers used by
the official Ninja Kitchen mobile app. If those identifiers ever
rotate (vendor change, new app version, regional split), login will
start failing — at which point you can extract a fresh set from
your own phone:

```bash
git clone https://github.com/coxtor/ninja-woodfire-integration
cd ninja-woodfire-integration
python3 scripts/extract_credentials.py --region EU   # or NA
```

Requirements: Android phone with the Ninja Kitchen app installed,
USB debugging enabled, `adb` on your PATH. The script captures
logcat for ~30 seconds while you open the app and writes the four
values to `ninja_woodfire_credentials.txt`. Paste them into the
integration's *advanced* config-flow fields, then delete the file.

`scripts/extract_credentials.py --help` for options including
parsing an existing logfile.

## Status & support

This is a personal hobby project published as-is. There is **no
support, no warranty, no guarantee of fitness for any purpose**.

- Issues and pull requests may be ignored. The author has no
  obligation to respond.
- The integration depends on a third-party cloud that may change or
  break it at any time without warning.
- Things that work today may not work tomorrow. Things that don't
  work may never work.
- The maintainer makes no commitment to keep the project alive,
  compatible with future Home Assistant versions, or working at all.

If any of that is a problem for you, do not install this.

## Automation example — notify when probe hits target

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
          message: >
            Probe 1 reached {{ trigger.event.data.target }}°C.
```

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This is an unofficial, independent project. It is **not affiliated
with, endorsed by, sponsored by, or supported by SharkNinja
Operating LLC** or any of its subsidiaries.

"Ninja", "Woodfire", "Connect Pro XL", and any related product
names are trademarks of their respective owners. They are used
here only descriptively to identify which physical device this
integration interoperates with — no claim of trademark ownership
or affiliation is made or implied.

The integration interacts with a third-party cloud service the
author does not own or control. The author makes no representation
that such interaction is permitted by the operators of that
service; users are solely responsible for ensuring their use
complies with all applicable terms of service and laws in their
jurisdiction. Use at your own risk.
