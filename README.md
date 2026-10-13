<p align="center">
  <img src="custom_components/ninja_woodfire/brand/icon.png" width="180" alt="Ninja Woodfire">
</p>

<h1 align="center">Ninja Woodfire — Home Assistant</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-orange.svg" alt="HACS"></a>
</p>

Control and monitor the **Ninja Woodfire Connect Pro XL** (`OG900-EU`) grill in
Home Assistant — **fully local over Bluetooth**, or over the Ninja cloud.

> Hobby project, published as-is — no support, no warranty. Primarily a
> Claude-assisted project.

## Two ways to connect

Chosen in the setup wizard:

- **Local (Bluetooth)** — no account, no cloud. Full cook control, live
  temperatures, meat probe and lid over Bluetooth (ESPHome proxies work). Needs
  a one-time key bundle you extract yourself from your own copy of the vendor
  app — see **[Local setup — extract your keys](#local-setup--extract-your-keys)**.
- **Cloud** — sign in with your Ninja Kitchen account (Ayla/AWS). *(no extra steps)*

Matching Lovelace card: [ninja-woodfire-card](https://github.com/coxtor/ninja-woodfire-card).

## Features

- **Cook control** — start, stop, skip-preheat, wake. Mode-aware target
  temperature, duration, smoke, per-probe targets. Live changes keep heat
  level, probe targets and elapsed time.
- **Sensors** — grill / air / smoke / meat-probe temperatures, cook & preheat
  progress, end-time, mode, setpoint, lid, per-probe status & target.
- **Diagnostics** — connection, backend, last report, Wi-Fi signal, poll health.
- **Events** for automations: `cook_started`, `preheat_complete`,
  `cook_halftime`, `cook_done`, `probe_halfway`, `probe_target_reached`,
  `prompt` (all prefixed `ninja_woodfire_`).

## Local control (no cloud)

Local communication with the grill over Bluetooth LE — no account needed. Cook
controls, live chamber and meat-probe temperatures, the lid state, the cook
phase (ignition / preheat / cooking) and the add-food / flip / done / get-food
prompts all work locally; the *Wake* button nudges a grill in standby back
online. Works through ESPHome Bluetooth proxies.

The phase and prompt are read from the grill's advertisement beacon. A few
states aren't mapped there yet — **contributions welcome**: the *resting* phase,
the *close-lid* prompt and the *end* screen. "Powered off" isn't broadcast at
all (the grill just stops advertising), so it simply shows as offline.

## Local setup — extract your keys

**This project ships no key material.** Talking to the grill locally needs a few
AES keys that live inside the vendor app. You extract them **once, yourself, from
your own copy of the app**, and paste the result into the integration. Nothing
secret is distributed here; the extraction tool only reads the keys out of a copy
you already have.

1. Download the Ninja app APK (e.g. from APKMirror) and unzip it; find
   `lib/arm64-v8a/libgrillcore.so` inside.
2. Run the extractor with that file and your grill's Bluetooth address
   (needs Python with `unicorn`, `capstone` and `cryptography`):
   ```bash
   python3 scripts/extract_ninja_keys.py --so libgrillcore.so --mac AA:BB:CC:DD:EE:FF
   ```
   It emulates the app's own key routine on your copy and prints a single key
   bundle string. No keys are baked into this tool.
3. In Home Assistant: *Add Integration → Ninja Woodfire → Local*, pick your grill
   and paste the bundle. Done — the keys stay on your machine.

Cloud setup needs none of this.

## Install & set up

**Install (HACS):** HACS → Integrations → ⋮ → *Custom repositories* → add this
repo as *Integration* → install → restart. *(Or copy
`custom_components/ninja_woodfire/` manually and restart.)*

**Set up:** *Settings → Devices & services → Add Integration → Ninja Woodfire*,
then pick **Local** (choose your grill from the Bluetooth list) or **Cloud**
(sign in, pick your grill).

## Node-RED notifications

A ready-to-import tab turns the integration's events into phone notifications —
generic, no private config. English (`woodfire_flow.json`) and German
(`woodfire_flow_de.json`): [`examples/node-red/`](examples/node-red/).

## Automations

```yaml
automation:
  - alias: "Steaks ready"
    trigger:
      - platform: event
        event_type: ninja_woodfire_probe_target_reached
        event_data: { probe_index: 0 }
    action:
      - service: notify.mobile_app
        data:
          message: "Probe 1 reached {{ trigger.event.data.target }}°C."
```

## Cloud notes

<details>
<summary>AWS migration, regions, credentials</summary>

SharkNinja is moving grills from Ayla to its own AWS backend. Migrated grills
are detected and used automatically (the *Backend* sensor shows which is live);
Ayla is still used for setup and renaming. AWS queues commands for a
disconnected grill, so a cook is only started on a connected grill and an
unacknowledged start is withdrawn after 20 s.

EU and NA defaults are bundled (only EU is exercised by the author). The bundled
cloud identifiers come from the official app; if they ever rotate and login
fails, extract a fresh set from your phone with
`python3 scripts/extract_credentials.py --region EU` (needs the Ninja app, USB
debugging and `adb`) and paste them into the *advanced* config fields.

</details>

## Status & disclaimer

Personal hobby project, **as-is — no support, no warranty**; the cloud path
depends on a third party that may change or break it at any time.

Unofficial and independent — **not affiliated with or endorsed by SharkNinja
Operating LLC**. "Ninja", "Woodfire" and related names are trademarks of their
owners, used here only to identify the device this integrates with. You are
responsible for complying with applicable terms and laws. Use at your own risk.
MIT — see [LICENSE](LICENSE).
