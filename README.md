# IKEA Dirigera Hub Integration for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Version](https://img.shields.io/github/v/release/nrbrt/dirigera_platform)](https://github.com/nrbrt/dirigera_platform/releases)


A Home Assistant integration for the IKEA Dirigera hub, built on the [dirigera](https://github.com/Leggin/dirigera) Python library (v1.2.6). Originally forked from [sanjoyg/dirigera_platform](https://github.com/sanjoyg/dirigera_platform) and actively maintained with new features, device support, and bug fixes.

Contributions are welcome — feel free to open [issues](https://github.com/nrbrt/dirigera_platform/issues) or submit pull requests. Device data dumps are especially helpful for adding support for new devices ([how to dump](docs/dump-data.md)).

## Highlights

- **Dynamic Device Discovery** — devices added, removed, renamed, or moved to a different room in the IKEA Home app are automatically reflected in Home Assistant without a restart
- **Split-Device Merging** — newer IKEA devices (GRILLPLATS, TOFSMYGGA, TIMMERFLOTTE, MYGGSPRAY) expose as multiple API devices linked by `relationId`; the integration automatically merges them into single HA entities
- **Real-Time Updates** — full WebSocket event support for instant state changes across all device types
- **Reliable Startup** — automatic retry on connection failure (`ConfigEntryNotReady`) instead of requiring manual reloads

## Upstream issues addressed in this fork

If you arrived here from an open issue on [sanjoyg/dirigera_platform](https://github.com/sanjoyg/dirigera_platform/issues), the fix may already be in this fork. Switching to this repository in HACS should resolve the listed issues without further action.

| Upstream issue | Title | Fixed in |
|----------------|-------|----------|
| [#155](https://github.com/sanjoyg/dirigera_platform/issues/155) | Integration failed setup on 2025.6.0 | v0.2.7 (`ConfigEntryNotReady` retry + deprecated `hass.loop` migration) |
| [#160](https://github.com/sanjoyg/dirigera_platform/issues/160) | TRADFRI on/off switch — not properly integrated | v0.2.6 (multi-button scene-creation per controller half) |
| [#165](https://github.com/sanjoyg/dirigera_platform/issues/165) | VALLHORN motion sensor not providing illuminance | v0.2.1 (separate light sensor entities with lux conversion) |
| [#168](https://github.com/sanjoyg/dirigera_platform/issues/168) | Disconnected due to inactivity | v0.2.11/12 (application-level WebSocket keepalive) |
| [#175](https://github.com/sanjoyg/dirigera_platform/issues/175) | MotionSensor fails setup when batteryPercentage is missing | v0.2.1 (`batteryPercentage` made optional) |
| [#177](https://github.com/sanjoyg/dirigera_platform/issues/177) | Styrbar 4-button — no events | v0.2.6 (per-button scene creation) |
| [#183](https://github.com/sanjoyg/dirigera_platform/issues/183) | MYGGSPRAY motion sensor not visible | upstream 2.7.1 + v0.2.16 (battery-sensor dedup unique to this fork) |
| [#184](https://github.com/sanjoyg/dirigera_platform/issues/184) | BILRESA dual button — no activities | v0.2.6 (dual-button to controller map + scenes) |
| [#195](https://github.com/sanjoyg/dirigera_platform/issues/195) | Lights can't be used as automation triggers | v0.2.9 (merged upstream PR #197) |
| [#198](https://github.com/sanjoyg/dirigera_platform/issues/198) | `ikea_bulb_device_set` has no attribute 'entity' | v0.2.8 (merged upstream PR #196) |
| [#152](https://github.com/sanjoyg/dirigera_platform/issues/152) | IKEA Inspelning (plug with power sensor) | v0.2.5 (outlet + electricalSensor split-device merge) |
| [#148](https://github.com/sanjoyg/dirigera_platform/issues/148) | Energy Consumed at Last Reset not updating | v0.2.5 (electricalSensor events routed to outlet) |

### Not addressed (out of scope or different root cause)

- [#143](https://github.com/sanjoyg/dirigera_platform/issues/143) — *Power factor for Inspelning*: the Dirigera API does not expose `currentPowerFactor`; would require a user-configurable correction factor + derived sensor, not a code fix.
- [#150](https://github.com/sanjoyg/dirigera_platform/issues/150) — *Duplicate Devices with Matter & HACS*: caused by running both Matter and Dirigera integrations against the same device, not by this integration.
- [#194](https://github.com/sanjoyg/dirigera_platform/issues/194) — *TIMMERFLOTTE humidity sensor*: TIMMERFLOTTE temperature works (split-device merging), humidity needs separate device support — not yet implemented.
- New device support requests (STARKVIND quirks, VINDYKSTRA air quality, Matter device coverage, etc.) are out of scope for this fork's reliability focus.

## Supported Devices

| Category | Devices | Notes |
|----------|---------|-------|
| **Lights** | TRÅDFRI, FLOALT, and other IKEA lights | RGBWW with dynamic color mode switching (HS ↔ color temp) |
| **Outlets** | INSPELNING, GRILLPLATS, TOFSMYGGA | Energy monitoring (voltage, current, power, energy); split-device pairs auto-merged |
| **Motion Sensors** | VALLHORN, MYGGSPRAY (E2494) | Handles both `motionSensor` and `occupancySensor` device types |
| **Light Sensors** | MYGGSPRAY (E2494) | Illuminance in lux (Matter raw value conversion) |
| **Open/Close Sensors** | PARASOLL, MYGGBETT | |
| **Environment Sensors** | VINDSTYRKA (PM2.5, VOC), ALPSTUGA (CO2), TIMMERFLOTTE | Split-device pairs auto-merged; `state_class: measurement` for Long Term Statistics |
| **Blinds** | FYRTUR, KADRILJ | |
| **Remotes** | STYRBAR, RODRET, SOMRIG, BILRESA | `remotePressEvent` + `shortcutController` support; automation triggers for all buttons |
| **Air Purifiers** | STARKVIND | Native unit of measurement fix |
| **Water Sensors** | BADRING | |
| **Scenes** | All Dirigera scenes | Exposed as entities with Activate button |

## Requirements

- Home Assistant 2026.1 or newer (v0.2.6 and earlier work with HA 2024.12+)
- IKEA Dirigera hub on your local network
- HACS installed

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nrbrt&repository=dirigera_platform&category=integration)

Or manually:
1. In Home Assistant, go to **HACS** → **Integrations** → **⋮** (top right) → **Custom repositories**
2. Add `https://github.com/nrbrt/dirigera_platform` as an **Integration**
3. Search for "Dirigera" and install

## Setup

1. Go to **Settings** → **Devices & Services** → **Add Integration** → search for **Dirigera**
2. Enter the IP address of your Dirigera hub (find it in your router's DHCP client list)
3. When prompted, press the **action button** on the Dirigera hub, then click Submit
4. Your devices will appear automatically

To test with mock devices, enter `mock` as the IP address.

## Bug Reports & Feature Requests

When opening an issue, please include a device data dump — it helps enormously:

1. Go to **Developer Tools** → **Services**
2. Call `dirigera_platform.dump_data` (no parameters needed)
3. Copy the output from the Home Assistant log

See [detailed instructions](docs/dump-data.md).

## Changelog

See [Releases](https://github.com/nrbrt/dirigera_platform/releases) for a full changelog.

### Recent

- **v0.2.17** (2026-05-19) — Fix: GRILLPLATS / TOFSMYGGA energy data missing on runtime-added plugs (issue [#31](https://github.com/nrbrt/dirigera_platform/issues/31)). When a smart plug is added to the Dirigera hub while the HA integration is already running, the ADD-event discovery path constructed the outlet directly via `dict_to_outlet()` and bypassed the energy-attribute merge from the linked `electricalSensor` device. Result: `current_active_power`, `total_energy_consumed`, voltage and amps were missing until an integration restart. The runtime-discovery path now uses `get_outlet_by_id()`, applying the same merge used at integration startup. Falls back to raw payload with a warning log if the lookup fails.
- **v0.2.16** (2026-04-27) — Battery sensor fixes (PRs #27, #28, #29 by @ermitovski):
  - Fix: avoid duplicate `Battery Percentage` entity on MYGGSPRAY split-devices. The hub exposes MYGGSPRAY as `occupancySensor` + `lightSensor` sharing a `relation_id`, both reporting `battery_percentage`; the duplicate `*_battery_percentage_2` entity is now suppressed by binding the battery diagnostic to the motion side only. Pre-existing `_2` entities become orphaned (state `unavailable`) and can be deleted from the device page.
  - Fix: de-duplicate the controller battery diagnostic on multi-button controllers (BILRESA, SOMRIG, RODRET, STYRBAR, ...). Group controllers by `relation_id` and elect a single primary that becomes the HA entity; secondary halves rebind to the primary in the device registry so `remotePressEvents` still resolve to a registered entity. Multi-button device-trigger generation is preserved.
  - Feature: emit `battery_percentage` diagnostic when a sensor (`waterSensor`, `motionSensor`, `occupancySensor`, `openCloseSensor`) is paired with HA already running. The WebSocket-driven discovery path now mirrors the static startup path, so freshly paired BADRING / motion / open-close sensors show the battery diagnostic without a HA restart.
- **v0.2.15** (2026-04-23) — Fix: guard `_color_mode` writes against unsupported modes on brightness-only lights (TRÅDFRI Driver). Prevents `HomeAssistantError: "... set to unsupported color mode hs"` after scene activation. (PR #26 by @charleslemaux)
- **v0.2.14** (2026-04-20) — Fix: proper split-device entity naming using has_entity_name (PR #25 by @crowbarz)
- **v0.2.13** (2026-04-20) — Fix: split-device entity naming — secondary entities (e.g. MYGGSPRAY illuminance) now inherit the user-configured name from the primary entity
- **v0.2.11/12** (2026-04-17) — Fix: application-level WebSocket keepalive to prevent Dirigera hub "disconnected due to inactivity" (issue #12)
- **v0.2.10** (2026-04-16) — Fix: honor 10s sleep between listener reconnects; add on_open/on_close diagnostics for WebSocket disconnect debugging
- **v0.2.9** (2026-04-15) — Feature: light device triggers — turned_on/turned_off automations (upstream PR #197)
- **v0.2.8** (2026-04-15) — Fix: `ikea_bulb_device_set` registry_entry registration (upstream PR #196)
- **v0.2.7** (2026-04-13) — Fix: replace deprecated `hass.loop` with `asyncio.get_event_loop()` (HA 2026.1+ compatibility)
- **v0.2.6** (2026-04-04) — BILRESA dual button support; consistent device naming for split-devices
- **v0.2.5** (2026-04-04) — Split-device plug support (GRILLPLATS, TOFSMYGGA)
- **v0.2.0** (2026-03-27) — Split-device merging framework; TIMMERFLOTTE support
