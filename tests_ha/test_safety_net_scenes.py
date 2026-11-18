"""Scenes in the safety net (#207), in a real Home Assistant.

Until now a renamed scene kept its old name until a reload: ikea_scene has an
async_update that fetches GET /scenes/<id>, but Home Assistant's BaseScene
sets _attr_should_poll = False, so it never ran (we assumed in #207 that it
polled every 30 s). A scene created or deleted in the IKEA app also only
showed after a reload. Whether the hub sends an event for a rename is not measured. Now the
safety net also fetches /scenes once per run: a renamed scene gets its new
name, a new scene appears, and a scene the hub no longer lists becomes
unavailable (not removed: its entity, and automations using it, stay until
the user removes it, and it comes back if the hub lists it again). Scene
entities do not poll (pinned, as a guard). A failing scene fetch does not stop the device
resync, nor the other way round.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from test_pins import ENTRY_DATA, HUB_IP

from conftest import FIXTURES
from custom_components.dirigera_platform.const import DOMAIN
from custom_components.dirigera_platform.hub_event_listener import SAFETY_NET_INTERVAL

FIXTURE = sorted(FIXTURES.glob("*.json"))[0].stem


class Hub:
    def __init__(self, hass, fake):
        self.hass, self.fake = hass, fake
        self.gets: list[str] = []
        self.failing: set[str] = set()
        real_get = fake.get

        def get(route):
            route = route.strip("/")
            self.gets.append(route)
            if route in self.failing:
                raise ConnectionError(f"fake hub: {route} does not answer")
            return real_get(route)
        fake.get = get

    def scene(self, scene_id):
        return next(s for s in self.fake.scenes if s["id"] == scene_id)


@pytest.fixture
async def hub(hass, fake_hub, captured_listener):
    h = Hub(hass, fake_hub(FIXTURE))
    h.entry = MockConfigEntry(domain=DOMAIN, title="scenes", data=dict(ENTRY_DATA), unique_id=HUB_IP)
    h.entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(h.entry.entry_id)
    await hass.async_block_till_done()
    h.gets.clear()  # what setup fetched does not count
    yield h
    # Unload fetches /scenes too (it deletes the integration's empty scenes),
    # and fails when that fetch does: a property of unload, not of this test.
    h.failing.clear()
    if h.entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(h.entry.entry_id)
        await hass.async_block_till_done()


def _state(hub, scene_id):
    entries = er.async_entries_for_config_entry(er.async_get(hub.hass), hub.entry.entry_id)
    entity_id = next(e.entity_id for e in entries if e.domain == "scene" and e.unique_id == scene_id)
    return hub.hass.states.get(entity_id)


async def _run(hub, n):
    """The n-th run of the safety net since setup."""
    await _later(hub.hass, n * (SAFETY_NET_INTERVAL + 1))


async def _later(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    # The run is a background task of the entry: wait for it too.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_scene_entities_do_not_poll(hub):
    # A guard: this already held before the safety net (BaseScene), so there
    # is no change of ours to take out for a negative control.
    await _later(hub.hass, 120)  # four scan intervals, no safety-net run yet
    assert [g for g in hub.gets if g.startswith("scenes")] == []


async def test_the_safety_net_brings_scenes_in_line(hub):
    hub.scene("device-21")["info"]["name"] = "Renamed in the app"
    hub.fake.scenes.remove(hub.scene("device-22"))
    added = {**hub.scene("device-21"), "id": "device-99",
             "info": {"name": "New in the app", "icon": "scenes_clean_sparkles"}}
    hub.fake.scenes.append(added)

    await _run(hub, 1)

    assert hub.gets.count("scenes") == 1
    assert _state(hub, "device-21").attributes["friendly_name"] == "Renamed in the app"
    assert _state(hub, "device-99").attributes["friendly_name"] == "New in the app"
    # Missing from one answer is not enough to be gone ...
    assert _state(hub, "device-22").state != "unavailable"
    await _run(hub, 2)
    # ... from two in a row it is.
    assert _state(hub, "device-22").state == "unavailable"

    # Back on the hub: available again, same entity.
    hub.fake.scenes.append({**hub.scene("device-21"), "id": "device-22",
                            "info": {"name": "Scene 2", "icon": "scenes_clean_sparkles"}})
    await _run(hub, 3)
    assert _state(hub, "device-22").state != "unavailable"


async def test_an_empty_answer_once_is_not_every_scene_gone(hub):
    # Review: an empty or partial answer (a hub that is busy, starting up)
    # made every scene unavailable. A user can delete all scenes, so an empty
    # answer is not ignored: it counts once two answers in a row agree.
    scenes = list(hub.fake.scenes)
    hub.fake.scenes.clear()
    await _run(hub, 1)
    assert _state(hub, "device-21").state != "unavailable"
    hub.fake.scenes.extend(scenes)
    await _run(hub, 2)                       # back: the miss is forgotten
    hub.fake.scenes.clear()
    await _run(hub, 3)
    assert _state(hub, "device-21").state != "unavailable"
    await _run(hub, 4)
    assert _state(hub, "device-21").state == "unavailable"
    hub.fake.scenes.extend(scenes)
    await _run(hub, 5)
    assert _state(hub, "device-21").state != "unavailable"


async def test_the_integrations_own_scenes_get_no_entity(hub):
    # The empty scenes the integration makes for remote buttons.
    own = {**hub.scene("device-21"), "id": "device-98",
           "info": {"name": "dirigera_integration_empty_scene_device-07_1_singlePress",
                    "icon": "scenes_cake"}}
    hub.fake.scenes.append(own)
    await _run(hub, 1)
    entries = er.async_entries_for_config_entry(er.async_get(hub.hass), hub.entry.entry_id)
    assert [e for e in entries if e.unique_id == "device-98"] == []


async def test_a_failing_scene_fetch_does_not_stop_the_device_resync(hub):
    env = next(d for d in hub.fake.devices if d["deviceType"] == "environmentSensor")
    env["attributes"]["currentTemperature"] = 23.5
    hub.failing.add("scenes")
    await _later(hub.hass, SAFETY_NET_INTERVAL + 1)
    assert float(_state_by_uid(hub, "sensor", env["id"] + "TEMP").state) == 23.5
    # Nothing became unavailable for a fetch that failed.
    assert _state(hub, "device-21").state != "unavailable"


async def test_a_failing_device_fetch_does_not_stop_the_scene_sync(hub):
    hub.scene("device-21")["info"]["name"] = "Renamed in the app"
    hub.failing.add("devices")
    await _later(hub.hass, SAFETY_NET_INTERVAL + 1)
    assert _state(hub, "device-21").attributes["friendly_name"] == "Renamed in the app"


async def test_unload_ends_the_scene_sync(hub):
    assert await hub.hass.config_entries.async_unload(hub.entry.entry_id)
    await hub.hass.async_block_till_done()
    before = len(hub.gets)
    await _later(hub.hass, 2 * SAFETY_NET_INTERVAL + 2)
    assert hub.gets[before:] == []


def _state_by_uid(hub, domain, unique_id):
    entries = er.async_entries_for_config_entry(er.async_get(hub.hass), hub.entry.entry_id)
    entity_id = next(e.entity_id for e in entries if e.domain == domain and e.unique_id == unique_id)
    return hub.hass.states.get(entity_id)


async def test_update_entity_makes_a_scene_the_hub_has_available_again(hub):
    scenes = list(hub.fake.scenes)
    hub.fake.scenes.clear()
    await _run(hub, 1)
    await _run(hub, 2)
    assert _state(hub, "device-21").state == "unavailable"
    hub.fake.scenes.extend(scenes)
    entity_id = _state(hub, "device-21").entity_id
    await hub.hass.services.async_call("homeassistant", "update_entity",
                                       {"entity_id": entity_id}, blocking=True)
    await hub.hass.async_block_till_done()
    assert hub.hass.states.get(entity_id).state != "unavailable"
