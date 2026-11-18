"""Pintest: what a user's Home Assistant holds for each hub device must not move.

0.5 changes how the integration decides which hub devices form one physical
device (RFC #77). Automations, dashboards and history hang on things Home
Assistant stores and on what the integration does with hub events, so this
records both for a real hub's device list and fails on any difference.

Sections of a pin file:

- ``hub_ids``: per hub id, what a hub event for that id changes in Home
  Assistant (entity states, device names and areas): when its reachability
  flips, when the hub gives it another name, and when one value typical of
  its type changes, measured from reachable. Then the device is put back and
  checked to be back. A regrouping in 0.5 reads here as one line per hub id.
- ``entities``: per ``domain|unique_id``: entity_id, name, device, category,
  unit, disabled/hidden.
- ``devices``: per device identifier: name, area, model, versions, via.
- ``triggers``: the device triggers each device offers.
- ``presses``: what a press fires, for every remote part in the fixture and
  every ``<base>_N`` for a ``buttonN`` its device offers, and whether that is
  an offered trigger. The trigger type of a multi-button remote comes from a
  regex on the hub id, so this can break automations without any id changing.
- ``hub_writes``: what setup wrote to the hub.
- ``unknown_hub_routes``: reads the fake hub could not answer.

What is pinned is read from Home Assistant; hub messages go in through the
listener's message entry point (see conftest.py, the one place that touches
the integration's internals). Entities and devices are read after the events,
so an event that renames or moves a device shows up. The push throttle is off,
so a write is not held back by a write of another part just before it.

The pins record what the integration DOES, not what it should do: a known
oddity stays pinned until a change fixes it on purpose, and then the pin file
is regenerated (``--pin-update``) in the same commit so the difference is
reviewed. A new fixture in fixtures/ is picked up by itself, once per device
set mode.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest
from conftest import (
    FIXTURES,
    REMOTE_EVENT,
    deliver,
    device_identifier,
    hub_device_of,
    listener_registrations_left,
)
from homeassistant.const import CONF_IP_ADDRESS, CONF_TOKEN
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dirigera_platform import device_trigger
from custom_components.dirigera_platform.const import (
    CONF_DEVICE_SET_MODE,
    CONF_POWER_PUSH_THROTTLE,
    DEVICE_SET_MODE_LIGHTS_ONLY,
    DEVICE_SET_MODE_SETS_ONLY,
    DOMAIN,
)

PINS = Path(__file__).parent / "pins"

HUB_IP = "192.0.2.1"  # TEST-NET-1: never a real hub
ENTRY_DATA = {CONF_IP_ADDRESS: HUB_IP, CONF_TOKEN: "pintest-token", CONF_POWER_PUSH_THROTTLE: 0}

# Device set modes. An entry without one was created before the option
# existed and keeps the legacy behaviour (sets with lights).
MODES = {
    "legacy": {},
    "sets_only": {CONF_DEVICE_SET_MODE: DEVICE_SET_MODE_SETS_ONLY},
    "lights_only": {CONF_DEVICE_SET_MODE: DEVICE_SET_MODE_LIGHTS_ONLY},
}
CASES = [(fixture.stem, mode) for fixture in sorted(FIXTURES.glob("*.json")) for mode in MODES]


def _pin_name(fixture: str, mode: str) -> str:
    return f"{fixture}__{mode}"

CONTROLLER_TYPES = {"genericSwitch", "lightController", "blindsController", "shortcutController"}
CLICK_PATTERNS = ("singlePress", "longPress", "doublePress")
BUTTON_TYPE = re.compile(r"^button(\d+)_")
PRESS_TIME = "2026-01-01T00:00:00Z"


def _flip(value):
    return not value


def _up(value):
    return value + 1


def _down(value):
    return value - 1


# Per device type: one attribute a hub event changes, and how.
PROBES = {
    "light": ("isOn", _flip),
    "outlet": ("isOn", _flip),
    "occupancySensor": ("isDetected", _flip),
    "openCloseSensor": ("isOpen", _flip),
    "waterSensor": ("waterLeakDetected", _flip),
    "lightSensor": ("illuminance", _up),
    "environmentSensor": ("currentTemperature", _up),
    # Not the power: with the plug off the integration clamps that to 0 W (#36).
    "electricalSensor": ("totalEnergyConsumed", _up),
    "genericSwitch": ("batteryPercentage", _down),
    "lightController": ("batteryPercentage", _down),
}
# Types with nothing to probe. Any other type without a probe is pinned as
# "NO PROBE", so a fixture with a new device type shows the gap in its pins.
UNPROBED = {"gateway"}


class Snapshot(NamedTuple):
    state: str
    attributes: dict


def _ident(device) -> str | None:
    if device is None:
        return None
    return ",".join(sorted(f"{d}:{i}" for d, i in device.identifiers))


def _entity_key(reg_entry) -> str:
    return f"{reg_entry.domain}|{reg_entry.unique_id}"


def _room(hub_device: dict) -> str | None:
    return (hub_device.get("room") or {}).get("name")


def _enum_str(value) -> str | None:
    return str(value) if value else None


class Pinner:
    """Collects the pins of one set-up config entry."""

    def __init__(self, hass, config_entry, hub, listener):
        self.hass, self.config_entry, self.hub, self.listener = hass, config_entry, hub, listener
        self.ent_reg = er.async_get(hass)
        self.dev_reg = dr.async_get(hass)
        self.area_reg = ar.async_get(hass)
        self.area_slugs = sorted({slugify(_room(d)) for d in hub.devices if _room(d)}, key=len, reverse=True)
        self.remotes = [d for d in hub.devices if d.get("deviceType") in CONTROLLER_TYPES]
        self.read_registries()

    # --- registries ---------------------------------------------------------

    def read_registries(self) -> None:
        """(Re)read this entry's entities and devices; events can change them."""
        self.reg_entries = er.async_entries_for_config_entry(self.ent_reg, self.config_entry.entry_id)
        self.devices = dr.async_entries_for_config_entry(self.dev_reg, self.config_entry.entry_id)
        self.key_by_entity_id = {e.entity_id: _entity_key(e) for e in self.reg_entries}
        self.device_by_ident = {_ident(d): d for d in self.devices}

    def area_name(self, device) -> str | None:
        return self.area_reg.async_get_area(device.area_id).name if device.area_id else None

    def racy(self, device_id: str) -> bool:
        """The entities of this device come from parts with different rooms.

        At registration the area comes from whichever part registers first,
        and the platforms set up concurrently. The area is put right by the
        name and area sync after setup, but an entity_id made before that has
        the area in it or not. Seen only for MYGGSPRAY (light sensor part
        without a room, motion part with one): 8 of 36 setups without the area.
        """
        rooms = {_room(o) for e in self.reg_entries if e.device_id == device_id
                 if (o := hub_device_of(e.unique_id, self.hub.devices)) is not None}
        return len(rooms) > 1

    def pinned_entity_id(self, reg_entry) -> str:
        """The entity_id, with the area slug marked only where it is racy."""
        if reg_entry.device_id is None or not self.racy(reg_entry.device_id):
            return reg_entry.entity_id
        domain, obj = reg_entry.entity_id.split(".", 1)
        for slug in self.area_slugs:
            if obj.startswith(slug + "_"):
                obj = obj[len(slug) + 1:]
                break
        return f"{domain}.(racy area)_{obj}"

    # --- sections -----------------------------------------------------------

    def pin_devices(self) -> dict:
        out = {}
        for device in self.devices:
            via = self.dev_reg.async_get(device.via_device_id) if device.via_device_id else None
            out[_ident(device)] = {
                "name": device.name,
                "name_by_user": device.name_by_user,
                "area": self.area_name(device),
                "model": device.model,
                "model_id": device.model_id,
                "manufacturer": device.manufacturer,
                "sw_version": device.sw_version,
                "hw_version": device.hw_version,
                "serial_number": device.serial_number,
                "connections": sorted(list(c) for c in device.connections),
                "via": _ident(via),
                "entry_type": _enum_str(device.entry_type),
                "disabled_by": _enum_str(device.disabled_by),
            }
        return out

    def pin_entities(self) -> dict:
        out = {}
        for e in self.reg_entries:
            device = self.dev_reg.async_get(e.device_id) if e.device_id else None
            out[_entity_key(e)] = {
                "entity_id": self.pinned_entity_id(e),
                "original_name": e.original_name,
                "has_entity_name": e.has_entity_name,
                "translation_key": e.translation_key,
                "device_class": e.original_device_class,
                "entity_category": _enum_str(e.entity_category),
                "unit": e.unit_of_measurement,
                "device": _ident(device),
                "disabled_by": _enum_str(e.disabled_by),
                "hidden_by": _enum_str(e.hidden_by),
            }
        return out

    async def pin_triggers(self) -> tuple[dict, dict]:
        """Triggers per device identifier, and the offered types per HA device id."""
        out, offered = {}, {}
        for device in self.devices:
            found = await device_trigger.async_get_triggers(self.hass, device.id)
            if found:
                out[_ident(device)] = sorted(
                    f"{t.get('type')} @ {self.key_by_entity_id.get(t.get('entity_id'), t.get('entity_id'))}"
                    for t in found
                )
                offered[device.id] = {t.get("type") for t in found}
        return out, offered

    def press_ids(self, offered: dict) -> list[str]:
        """Every remote part, plus <base>_N for each buttonN its own device offers."""
        ids = {d["id"] for d in self.remotes}
        for d in self.remotes:
            device = self.device_by_ident.get(":".join(device_identifier(d)))
            numbers = {int(m.group(1)) for t in offered.get(device.id if device else None, ())
                       if (m := BUTTON_TYPE.match(t or ""))}
            ids |= {f"{d['id'].rsplit('_', 1)[0]}_{n}" for n in numbers}
        return sorted(ids)

    async def pin_presses(self, offered: dict) -> dict:
        fired: list = []
        unsub = self.hass.bus.async_listen(REMOTE_EVENT, lambda ev: fired.append(dict(ev.data)))
        out = {}
        # One time for all presses: no two presses share a debounce key
        # (device and trigger type), so none is debounced.
        for hub_id in self.press_ids(offered):
            for pattern in CLICK_PATTERNS:
                fired.clear()
                deliver(self.listener, {
                    "type": "remotePressEvent",
                    "time": PRESS_TIME,
                    "data": {"id": hub_id, "clickPattern": pattern},
                })
                await self.hass.async_block_till_done()
                out[f"{hub_id} {pattern}"] = [
                    {
                        "type": ev.get("type"),
                        "device": _ident(self.dev_reg.async_get(ev.get("device_id"))),
                        "entity": self.key_by_entity_id.get(
                            ev.get("entity_id"), f"NOT IN REGISTRY: {ev.get('entity_id')}"),
                        "offered": ev.get("type") in offered.get(ev.get("device_id"), set()),
                    }
                    for ev in fired
                ]
        unsub()
        return out

    # --- hub event probes ---------------------------------------------------

    def snapshot(self) -> dict:
        """Entity states, and each device's name and area: what an event can change."""
        self.read_registries()
        out = {}
        for e in self.reg_entries:
            st = self.hass.states.get(e.entity_id)
            out[_entity_key(e)] = None if st is None else Snapshot(st.state, dict(st.attributes))
        for d in self.devices:
            out[f"device|{_ident(d)}"] = Snapshot(d.name, {"area": self.area_name(d)})
        return out

    @staticmethod
    def changes(before: dict, after: dict) -> dict:
        """Per entity or device that changed: its state "old -> new", or the attributes that changed."""
        out = {}
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            if a is None or b is None or a.state != b.state:
                out[key] = f"{a and a.state} -> {b and b.state}"
            else:
                out[key] = sorted(k for k in set(a.attributes) | set(b.attributes)
                                  if a.attributes.get(k) != b.attributes.get(k))
        return out

    async def send(self, data: dict) -> None:
        deliver(self.listener, {"type": "deviceStateChanged", "data": data})
        await self.hass.async_block_till_done()

    async def probe_reachability(self, d: dict, base: dict) -> dict:
        await self.send({**d, "isReachable": not d.get("isReachable", True)})
        return self.changes(base, self.snapshot())

    async def probe_rename(self, d: dict, base: dict) -> dict:
        """Each part has its own name on the hub. Whether a part's name reaches
        the device is what this shows; another part routed here must not rename it."""
        attrs = d.get("attributes") or {}
        await self.send({**d, "attributes": {**attrs, "customName": f"Renamed {d['id']}"}})
        return self.changes(base, self.snapshot())

    async def probe_attribute(self, d: dict) -> dict:
        """One value typical of the type, measured from reachable: on an
        unreachable device a changed value does not show, whatever it reaches."""
        device_type = d.get("deviceType")
        attrs = d.get("attributes") or {}
        if device_type in UNPROBED:
            return {}
        if device_type not in PROBES:
            return {"attribute": f"NO PROBE for {device_type}"}
        name, change = PROBES[device_type]
        if name not in attrs:
            return {"attribute": f"{name} not in the fixture"}
        await self.send({**d, "isReachable": True})
        reachable = self.snapshot()
        await self.send({**d, "isReachable": True, "attributes": {**attrs, name: change(attrs[name])}})
        return {"attribute": name, "attribute_changes": self.changes(reachable, self.snapshot())}

    async def pin_hub_ids(self) -> dict:
        """Per hub id: what an event for it changes, each probe followed by putting it back."""
        out = {}
        for d in self.hub.devices:
            base = self.snapshot()
            row = {"reachability_flipped": await self.probe_reachability(d, base)}
            await self.send(d)
            row["renamed"] = await self.probe_rename(d, base)
            await self.send(d)
            row.update(await self.probe_attribute(d))
            await self.send(d)
            if back := self.changes(base, self.snapshot()):
                row["not_back_after_restore"] = back
            out[d["id"]] = row
        return out

    async def collect(self) -> dict:
        triggers, offered = await self.pin_triggers()
        pins = {
            "triggers": triggers,
            "presses": await self.pin_presses(offered),
            "hub_ids": await self.pin_hub_ids(),
        }
        # After the events: one that renamed or moved a device shows up here.
        self.read_registries()
        pins["entities"] = self.pin_entities()
        pins["devices"] = self.pin_devices()
        pins["hub_writes"] = sorted(f"{method} {route}" for method, route in self.hub.writes)
        pins["unknown_hub_routes"] = sorted(set(self.hub.unknown))
        return pins


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _diff(old, new, path: str = "") -> list[str]:
    """One line per moved value, with its path (section.key.field...)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            sub = f"{path}.{key}" if path else key
            if key not in new:
                out.append(f"{sub}: GONE (was {_dump(old[key])})")
            elif key not in old:
                out.append(f"{sub}: NEW {_dump(new[key])}")
            else:
                out.extend(_diff(old[key], new[key], sub))
        return out
    return [] if old == new else [f"{path}: {_dump(old)} -> {_dump(new)}"]


@pytest.mark.parametrize(("fixture", "mode"), CASES, ids=[_pin_name(*c) for c in CASES])
async def test_pins(hass, fake_hub, captured_listener, pin_update, fixture, mode):
    hub = fake_hub(fixture)
    config_entry = MockConfigEntry(
        domain=DOMAIN, title="pintest", data={**ENTRY_DATA, **MODES[mode]}, unique_id=HUB_IP)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert captured_listener, "setup did not create the hub event listener"

    pins = await Pinner(hass, config_entry, hub, captured_listener[0]).collect()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not listener_registrations_left(), "listener registrations left after unload"

    path = PINS / f"{_pin_name(fixture, mode)}.json"
    if pin_update or not path.exists():
        PINS.mkdir(exist_ok=True)
        path.write_text(json.dumps(pins, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if not pin_update:
            pytest.fail(f"no pin file yet; wrote {path.name}, review it and commit it", pytrace=False)
        return

    differences = _diff(json.loads(path.read_text(encoding="utf-8")), pins)
    if differences:
        pytest.fail(f"{len(differences)} pins moved in {path.name}:\n" + "\n".join(differences), pytrace=False)


def test_every_pin_file_has_a_case():
    """A pin file whose fixture or mode is gone would never be compared again."""
    names = {_pin_name(*c) for c in CASES}
    orphans = sorted(p.stem for p in PINS.glob("*.json") if p.stem not in names)
    assert not orphans, f"pin files without a case (remove them or restore their fixture): {orphans}"
