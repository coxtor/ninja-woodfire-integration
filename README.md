# Ninja Woodfire — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Hobby project — published as-is, no support, no warranty.**
> See [Status & support](#status--support) and [Disclaimer](#disclaimer)
> below before installing.

Home Assistant integration for the Ninja Woodfire Connect Pro XL
outdoor grill. Connects via your Ninja Kitchen account; no local
network access to the grill is required.

## Features

- Live sensors: grill / air / smoke / probe temperatures, cook progress,
  preheat progress, end-time, active mode, current setpoint, lid state,
  per-probe status & target.
- Cook controls: start, stop, skip-preheat. Mode-aware temperature
  number (heat-level for grill, °C for everything else). Duration,
  smoke, per-probe targets.
- Adaptive polling — fast while cooking, relaxed while idle.
- Auto-reconnect on token expiry.
- HA event-bus events for automations:
  - `ninja_woodfire_cook_started`
  - `ninja_woodfire_preheat_complete`
  - `ninja_woodfire_cook_halftime`
  - `ninja_woodfire_cook_done`
  - `ninja_woodfire_probe_target_reached`

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
2. Sign in with your Ninja Kitchen account
3. Pick your grill

The four "advanced" fields in the form are optional and used only if
the bundled defaults stop working — see [Fallback: regenerating
credentials](#fallback-regenerating-credentials) below.

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
