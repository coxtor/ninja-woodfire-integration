# Node-RED: Woodfire push notifications

A ready-to-import Node-RED tab that turns the integration's grill events into
notifications. It is generic — no private config, no custom subflows.

## What it does

Subscribes to the integration's Home Assistant bus events and sends one
notification each:

| Event | Notification |
|---|---|
| `ninja_woodfire_cook_started` | Grill started (mode, duration) |
| `ninja_woodfire_preheat_complete` | Preheat done — add food |
| `ninja_woodfire_cook_halftime` | Halfway — time to flip |
| `ninja_woodfire_probe_halfway` | Probe halfway to target |
| `ninja_woodfire_probe_target_reached` | Probe target reached |
| `ninja_woodfire_cook_done` | Cook done / stopped (+ clears probe notifications) |
| `ninja_woodfire_prompt` | Display prompt (add / flip / get food) |

## Import & setup

1. Node-RED → ☰ → *Import* → paste `woodfire_flow.json`.
2. Pick your Home Assistant **server** in the event and notify nodes
   (Node-RED prompts for the unknown server config on import).
3. Open the **notify node** and change `notify.notify` to your own service,
   e.g. `notify.mobile_app_<yourphone>`. The function already emits the
   companion-app `data` keys (tag, color, group, channel, sticky, chronometer,
   progress) — rich notifications work on the Home Assistant mobile app.
4. Deploy. The `inject` nodes let you fire each event once to test without the
   grill.

No entity IDs or DSN are needed — the flow is driven purely by the integration's
bus events.
