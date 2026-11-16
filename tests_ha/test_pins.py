"""Pintest: what a user's Home Assistant holds for each hub device must not move.

0.5 changes how the integration decides which hub devices form one physical
device (RFC #77). Automations, dashboards and history hang on things Home
Assistant stores and on what the integration does with hub events, so this
records both for a real hub's device list and fails on any difference.

Sections of a pin file:

- ``hub_ids``: per hub id, what a hub event for that id changes in Home
  Assistant (entity states, device names and areas): when its reachability
  flips, when the hub gives it another name, and when one value typical of its
  type changes, measured from reachable. Then the device is put back and checked to be back. A
  regrouping in 0.5 reads here as one line per hub id.
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

Everything is observed through Home Assistant and the hub's message entry
point, not through the integration's internals, which 0.5 rewrites. Entities
and devices are read after the events, so an event that renames or moves a
device shows up. The push throttle is off (0 s), so a write is not held back by
a write of another part just before it.

The pins record what the integration DOES, not what it should do: a known
oddity stays pinned until a change fixes it on purpose, and then the pin file
is regenerated in the same commit so the difference is reviewed.

Regenerate: PIN_UPDATE=1 (see conftest.py for how to run).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dirigera_platform import device_trigger
from custom_components.dirigera_platform.const import CONF_DEVICE_SET_MODE, DOMAIN

PINS = Path(__file__).parent / "pins"

CONTROLLER_TYPES = {"genericSwitch", "lightController", "blindsController", "shortcutController"}
CLICK_PATTERNS = ("singlePress", "longPress", "doublePress")
BUTTON_TYPE = re.compile(r"^button(\d+)_")
PRESS_TIME = "2026-01-01T00:00:00Z"
ENTRY_DATA = {"ip_address": "192.0.2.1", "token": "pintest-token", "power_push_throttle_seconds": 0}

# One value per device type that a hub event changes: (attribute, how).
PROBES = {
    "light": ("isOn", "flip"),
    "outlet": ("isOn", "flip"),
    "occupancySensor": ("isDetected", "flip"),
    "openCloseSensor": ("isOpen", "flip"),
    "waterSensor": ("waterLeakDetected", "flip"),
    "lightSensor": ("illuminance", "up"),
    "environmentSensor": ("currentTemperature", "up"),
    # Not the power: with the plug off the integration clamps that to 0 W (#36).
    "electricalSensor": ("totalEnergyConsumed", "up"),
    "genericSwitch": ("batteryPercentage", "down"),
    "lightController": ("batteryPercentage", "down"),
}

# (fixture, label, entry data on top of ip/token). An entry without a device
# set mode was created before the option existed: the legacy "sets with lights".
CASES = [
    ("hub_norbert", "legacy", {}),
    ("hub_norbert", "sets_only", {CONF_DEVICE_SET_MODE: "sets_only"}),
    ("hub_norbert", "lights_only", {CONF_DEVICE_SET_MODE: "lights_only"}),
]


def _ident(device) -> str | None:
    if device is None:
        return None
    return ",".join(sorted(f"{d}:{i}" for d, i in device.identifiers))


def _room(hub_device) -> str | None:
    return (hub_device.get("room") or {}).get("name")


def _flag(value) -> str | None:
    return str(value) if value else None


class Pinner:
    """Collects the pins of one set-up entry."""

    def __init__(self, hass, entry, hub, listener):
        self.hass, self.entry, self.hub, self.listener = hass, entry, hub, listener
        self.ent_reg = er.async_get(hass)
        self.dev_reg = dr.async_get(hass)
        self.area_reg = ar.async_get(hass)
        self.area_slugs = sorted({slugify(_room(d)) for d in hub.devices if _room(d)}, key=len, reverse=True)
        self.remote_ids = [d["id"] for d in hub.devices if d.get("deviceType") in CONTROLLER_TYPES]
        self.read_registries()

    def read_registries(self) -> None:
        self.entries = er.async_entries_for_config_entry(self.ent_reg, self.entry.entry_id)
        self.devices = dr.async_entries_for_config_entry(self.dev_reg, self.entry.entry_id)
        self.key_by_entity_id = {e.entity_id: f"{e.domain}|{e.unique_id}" for e in self.entries}
        self.device_by_ident = {_ident(d): d for d in self.devices}

    def owner(self, unique_id: str) -> dict | None:
        """The hub device an entity belongs to: the longest hub id its unique_id starts with."""
        found = [d for d in self.hub.devices if unique_id.startswith(d["id"])]
        return max(found, key=lambda d: len(d["id"]), default=None)

    def racy(self, device_id: str) -> bool:
        """The entities of this device come from parts with different rooms.

        At registration the area comes from whichever part registers first,
        and the platforms set up concurrently. The area is put right by the
        name and area sync after setup, but an entity_id made before that has
        the area in it or not. Seen only for MYGGSPRAY (light sensor part
        without a room, motion part with one): 8 of 36 setups without the area.
        """
        rooms = {_room(o) for e in self.entries if e.device_id == device_id
                 if (o := self.owner(e.unique_id)) is not None}
        return len(rooms) > 1

    def entity_id(self, entry) -> str:
        """The entity_id, with the area slug marked only where it is racy."""
        if entry.device_id is None or not self.racy(entry.device_id):
            return entry.entity_id
        domain, obj = entry.entity_id.split(".", 1)
        for slug in self.area_slugs:
            if obj.startswith(slug + "_"):
                obj = obj[len(slug) + 1:]
                break
        return f"{domain}.(racy area)_{obj}"

    def pin_devices(self) -> dict:
        out = {}
        for device in self.devices:
            via = self.dev_reg.async_get(device.via_device_id) if device.via_device_id else None
            out[_ident(device)] = {
                "name": device.name,
                "name_by_user": device.name_by_user,
                "area": self.area_reg.async_get_area(device.area_id).name if device.area_id else None,
                "model": device.model,
                "model_id": device.model_id,
                "manufacturer": device.manufacturer,
                "sw_version": device.sw_version,
                "hw_version": device.hw_version,
                "serial_number": device.serial_number,
                "connections": sorted(list(c) for c in device.connections),
                "via": _ident(via),
                "entry_type": _flag(device.entry_type),
                "disabled_by": _flag(device.disabled_by),
            }
        return out

    def pin_entities(self) -> dict:
        out = {}
        for e in self.entries:
            device = self.dev_reg.async_get(e.device_id) if e.device_id else None
            out[f"{e.domain}|{e.unique_id}"] = {
                "entity_id": self.entity_id(e),
                "original_name": e.original_name,
                "has_entity_name": e.has_entity_name,
                "translation_key": e.translation_key,
                "device_class": e.original_device_class,
                "entity_category": _flag(e.entity_category),
                "unit": e.unit_of_measurement,
                "device": _ident(device),
                "disabled_by": _flag(e.disabled_by),
                "hidden_by": _flag(e.hidden_by),
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
        ids = set(self.remote_ids)
        for d in self.hub.devices:
            if d.get("deviceType") not in CONTROLLER_TYPES:
                continue
            device = self.device_by_ident.get(f"{DOMAIN}:{d.get('relationId') or d['id']}")
            numbers = {int(m.group(1)) for t in offered.get(device.id if device else None, ())
                       if (m := BUTTON_TYPE.match(t or ""))}
            ids |= {f"{d['id'].rsplit('_', 1)[0]}_{n}" for n in numbers}
        return sorted(ids)

    async def pin_presses(self, offered: dict) -> dict:
        fired: list = []
        unsub = self.hass.bus.async_listen("dirigera_platform_event", lambda ev: fired.append(dict(ev.data)))
        out = {}
        # One time for all presses: no two presses share a debounce key
        # (device and trigger type), so none is debounced.
        for hub_id in self.press_ids(offered):
            for pattern in CLICK_PATTERNS:
                fired.clear()
                self.listener._apply_message({
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

    def states(self) -> dict:
        """Entity states, and each device's name and area: what an event can change."""
        out = {}
        for e in er.async_entries_for_config_entry(self.ent_reg, self.entry.entry_id):
            st = self.hass.states.get(e.entity_id)
            out[f"{e.domain}|{e.unique_id}"] = None if st is None else (st.state, dict(st.attributes))
        for d in dr.async_entries_for_config_entry(self.dev_reg, self.entry.entry_id):
            area = self.area_reg.async_get_area(d.area_id).name if d.area_id else None
            out[f"device|{_ident(d)}"] = (d.name, {"area": area})
        return out

    @staticmethod
    def changes(before: dict, after: dict) -> dict:
        """Per entity that changed: its state "old -> new", or the attributes that changed."""
        out = {}
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            if a is None or b is None or a[0] != b[0]:
                out[key] = f"{a and a[0]} -> {b and b[0]}"
            else:
                out[key] = sorted(k for k in set(a[1]) | set(b[1]) if a[1].get(k) != b[1].get(k))
        return out

    async def send(self, data: dict) -> None:
        self.listener._apply_message({"type": "deviceStateChanged", "data": data})
        await self.hass.async_block_till_done()

    async def pin_hub_ids(self) -> dict:
        """Per hub id: what an event for it changes in Home Assistant, then put back."""
        out = {}
        for d in self.hub.devices:
            row = {}
            base = self.states()

            await self.send({**d, "isReachable": not d.get("isReachable", True)})
            row["reachability_flipped"] = self.changes(base, self.states())
            await self.send(d)

            # Each part has its own name on the hub. Whether a part's name
            # reaches the device (and which) is what this shows; another part
            # routed here must not rename it.
            await self.send({**d, "attributes": {**(d.get("attributes") or {}), "customName": f"Renamed {d['id']}"}})
            row["renamed"] = self.changes(base, self.states())
            await self.send(d)

            # Measured from reachable: on an unreachable device a changed
            # value does not show, whatever entity it reaches.
            probe = PROBES.get(d.get("deviceType"))
            attrs = d.get("attributes") or {}
            if probe and probe[0] in attrs:
                name, how = probe
                value = attrs[name]
                changed = (not value) if how == "flip" else value + (1 if how == "up" else -1)
                await self.send({**d, "isReachable": True})
                reachable = self.states()
                await self.send({**d, "isReachable": True, "attributes": {**attrs, name: changed}})
                row["attribute"] = name
                row["attribute_changes"] = self.changes(reachable, self.states())
                await self.send(d)

            back = self.changes(base, self.states())
            if back:
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
        pins["hub_writes"] = sorted(f"{m} {r}" for m, r, _ in self.hub.writes)
        pins["unknown_hub_routes"] = sorted(set(self.hub.unknown))
        return pins


def _diff(old: dict, new: dict) -> list[str]:
    out = []
    for section in sorted(set(old) | set(new)):
        a, b = old.get(section), new.get(section)
        if not (isinstance(a, dict) and isinstance(b, dict)):
            if a != b:
                out.append(f"{section}:\n    was {json.dumps(a)}\n    now {json.dumps(b)}")
            continue
        for key in sorted(set(a) | set(b)):
            if key not in b:
                out.append(f"{section}: GONE   {key}: {json.dumps(a[key])}")
            elif key not in a:
                out.append(f"{section}: NEW    {key}: {json.dumps(b[key])}")
            elif a[key] != b[key]:
                out.append(f"{section}: CHANGE {key}:\n    was {json.dumps(a[key])}\n    now {json.dumps(b[key])}")
    return out


@pytest.mark.parametrize(("fixture", "label", "extra"), CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
async def test_pins(hass, fake_hub_factory, captured_listener, fixture, label, extra):
    hub = fake_hub_factory(fixture)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="pintest",
        data={**ENTRY_DATA, **extra},
        unique_id="192.0.2.1",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert captured_listener, "setup did not create the hub event listener"

    pins = await Pinner(hass, entry, hub, captured_listener[0]).collect()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    path = PINS / f"{fixture}__{label}.json"
    if os.environ.get("PIN_UPDATE") or not path.exists():
        PINS.mkdir(exist_ok=True)
        path.write_text(json.dumps(pins, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        if not os.environ.get("PIN_UPDATE"):
            pytest.fail(f"no pin file yet; wrote {path.name}, review it and commit it")
        return

    differences = _diff(json.loads(path.read_text()), pins)
    assert not differences, "pins moved:\n" + "\n".join(differences)
