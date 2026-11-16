"""Pintest: what a user's Home Assistant holds for each hub device must not move.

0.5 changes how the integration decides which hub devices form one physical
device (RFC #77). Automations, dashboards and history hang on things Home
Assistant stores, so this test records them for a real hub's device list and
fails on any difference:

- every entity: platform, unique_id, entity_id, name, the device it sits on,
  disabled/hidden;
- every device: its identifiers, name, model, the device it is connected via;
- the device triggers each device offers (async_get_triggers), by type;
- where a hub event for each hub id ends up: the id it is routed to and the
  entity registered under it;
- what a press on each remote fires: the event type and the device it names,
  and whether that type is one of the triggers the device offers. The trigger
  type of a multi-button remote comes from a regex on the hub id, so this can
  break automations without any id changing.

The pins record what the integration DOES, not what it should do: a known
oddity stays pinned until a change fixes it on purpose, and then the pin file
is regenerated in the same commit so the difference is reviewed.

Regenerate: PIN_UPDATE=1 (see conftest.py for how to run).
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

import importlib

from custom_components.dirigera_platform import device_trigger
from custom_components.dirigera_platform.const import (
    CONF_DEVICE_SET_MODE,
    DOMAIN,
)

# The package re-exports the class under the module's name; take the module.
hel = importlib.import_module("custom_components.dirigera_platform.hub_event_listener")

PINS = Path(__file__).parent / "pins"

CONTROLLER_TYPES = {"genericSwitch", "lightController", "blindsController", "shortcutController"}

# (fixture, entry data on top of ip/token). An entry without a device set mode
# is one created before the option existed: the legacy "sets with lights".
CASES = [
    ("hub_norbert", "legacy", {}),
    ("hub_norbert", "sets_only", {CONF_DEVICE_SET_MODE: "sets_only"}),
    ("hub_norbert", "lights_only", {CONF_DEVICE_SET_MODE: "lights_only"}),
]


def _ident(device) -> str | None:
    if device is None:
        return None
    return ",".join(sorted(f"{d}:{i}" for d, i in device.identifiers))


def _members(hub) -> dict[str, list[dict]]:
    """Hub devices per device identifier (relationId, else id), as the fixture has them."""
    out: dict[str, list[dict]] = {}
    for d in hub.devices:
        out.setdefault(f"{DOMAIN}:{d.get('relationId') or d['id']}", []).append(d)
    return out


def _room(d) -> str | None:
    return (d.get("room") or {}).get("name")


async def _collect(hass, entry, hub, listener) -> dict:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    members = _members(hub)
    area_slugs = sorted({slugify(_room(d)) for d in hub.devices if _room(d)}, key=len, reverse=True)

    def without_area(entity_id: str) -> str:
        """entity_id with a leading area slug taken out.

        Home Assistant puts the device's area in a new entity_id. Which area a
        split device gets on a fresh install is a race today (see the area pin
        below), so the area is pinned there and taken out here.
        """
        domain, obj = entity_id.split(".", 1)
        for slug in area_slugs:
            if obj.startswith(slug + "_"):
                return f"{domain}.{obj[len(slug) + 1:]}"
        return entity_id

    devices = {}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        via = dev_reg.async_get(device.via_device_id) if device.via_device_id else None
        area = area_reg.async_get_area(device.area_id).name if device.area_id else None
        # The area comes from the room of the part that registers the device
        # first. Parts of one split device can differ (a MYGGSPRAY's light
        # sensor part has no room), and the platforms set up concurrently, so
        # then the area is one of the parts' rooms, or none: pinned as the set.
        rooms = sorted({str(_room(d)) for d in members.get(_ident(device), [])})
        if len(rooms) > 1:
            assert str(area) in rooms, f"{_ident(device)}: area {area} is none of its parts' rooms {rooms}"
            area = "one of " + "|".join(rooms)
        devices[_ident(device)] = {
            "area": area,
            "name": device.name,
            "name_by_user": device.name_by_user,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
            "via": _ident(via),
            "disabled_by": str(device.disabled_by) if device.disabled_by else None,
        }

    entities = {}
    by_entity_id = {}
    for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        device = dev_reg.async_get(e.device_id) if e.device_id else None
        entities[f"{e.domain}|{e.unique_id}"] = {
            "entity_id": without_area(e.entity_id),
            "original_name": e.original_name,
            "has_entity_name": e.has_entity_name,
            "device_class": e.original_device_class,
            "device": _ident(device),
            "disabled_by": str(e.disabled_by) if e.disabled_by else None,
            "hidden_by": str(e.hidden_by) if e.hidden_by else None,
        }
        by_entity_id[e.entity_id] = f"{e.domain}|{e.unique_id}"

    triggers = {}
    offered = {}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        found = await device_trigger.async_get_triggers(hass, device.id)
        if found:
            rows = sorted(
                f"{t.get('type')} @ {by_entity_id.get(t.get('entity_id'), t.get('entity_id'))}"
                for t in found
            )
            triggers[_ident(device)] = rows
            offered[device.id] = {t.get("type") for t in found}

    # Hub event routing, per hub id in the fixture.
    routes = {}
    for d in hub.devices:
        reg_id = listener._registered_id(d["id"], d.get("deviceType"))
        targets, cascade = [], None
        if reg_id is not None:
            value = hel.hub_event_listener.get_registry_entry(listener._hub_key, reg_id)
            ent = getattr(value, "entity", None)
            # Registered is either a Home Assistant entity (lights, sets) or the
            # integration's device object, whose entities listen on it.
            for target in [ent, *getattr(ent, "_listeners", [])]:
                reg = getattr(target, "registry_entry", None)
                if reg is not None:
                    targets.append(f"{reg.domain}|{reg.unique_id}")
            casc = getattr(getattr(value, "cascade_entity", None), "registry_entry", None)
            cascade = f"{casc.domain}|{casc.unique_id}" if casc else None
        routes[d["id"]] = {"routed_to": reg_id, "entities": sorted(targets), "cascade": cascade}

    # A press on each remote part: what is fired, and is it a trigger the device offers.
    fired: list = []
    unsub = hass.bus.async_listen("dirigera_platform_event", lambda ev: fired.append(dict(ev.data)))
    presses = {}
    t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    for n, d in enumerate(x for x in hub.devices if x.get("deviceType") in CONTROLLER_TYPES):
        for pattern in ("singlePress", "longPress", "doublePress"):
            hel.controller_trigger_last_time_map.clear()
            hel.remotes_with_press_events.clear()
            fired.clear()
            stamp = (t0 + datetime.timedelta(minutes=n)).isoformat().replace("+00:00", "Z")
            listener._apply_message({
                "type": "remotePressEvent",
                "time": stamp,
                "data": {"id": d["id"], "clickPattern": pattern},
            })
            await hass.async_block_till_done()
            row = []
            for ev in fired:
                device = dev_reg.async_get(ev.get("device_id"))
                # The event names the entity_id the entity object knows; an id
                # the registry does not have (any more) is pinned as such.
                entity = by_entity_id.get(ev.get("entity_id"))
                row.append({
                    "type": ev.get("type"),
                    "device": _ident(device),
                    "entity": entity or f"NOT IN REGISTRY: {ev.get('entity_id')}",
                    "offered": ev.get("type") in offered.get(ev.get("device_id"), set()),
                })
            presses[f"{d['id']} {pattern}"] = row
    unsub()

    return {
        "devices": dict(sorted(devices.items(), key=lambda kv: str(kv[0]))),
        "entities": dict(sorted(entities.items())),
        "triggers": dict(sorted(triggers.items(), key=lambda kv: str(kv[0]))),
        "routes": dict(sorted(routes.items())),
        "presses": dict(sorted(presses.items())),
    }


def _diff(old: dict, new: dict) -> list[str]:
    out = []
    for section in sorted(set(old) | set(new)):
        a, b = old.get(section, {}), new.get(section, {})
        for key in sorted(set(a) | set(b), key=str):
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
        data={"ip_address": "192.0.2.1", "token": "pintest-token", **extra},
        unique_id="192.0.2.1",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert captured_listener, "setup did not create the hub event listener"

    pins = await _collect(hass, entry, hub, captured_listener[0])
    pins["unknown_hub_routes"] = sorted(set(hub.unknown))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    path = PINS / f"{fixture}__{label}.json"
    if os.environ.get("PIN_UPDATE") or not path.exists():
        PINS.mkdir(exist_ok=True)
        path.write_text(json.dumps(pins, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        if not os.environ.get("PIN_UPDATE"):
            pytest.fail(f"no pin file yet; wrote {path.name}, review it and commit it")
        return

    old = json.loads(path.read_text())
    differences = _diff(old, json.loads(json.dumps(pins, sort_keys=True)))
    assert not differences, "pins moved:\n" + "\n".join(differences)
