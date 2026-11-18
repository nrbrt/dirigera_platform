"""The safety net (#207) in a real Home Assistant: setup schedules it, it
restores what an event did not bring, and unload ends it.

Polling per entity is off for blinds and environment sensors. Every
SAFETY_NET_INTERVAL one GET /devices is replayed through the resync path.
Here the hub changes without sending an event, time moves on, and the
entities must show the hub's values, including the min/max PM2.5 of an
environment sensor (never in an event) and the level and battery of a blind,
without a movement for the blind. After unload no run is left: the fixture
of pytest-homeassistant-custom-component fails a test with a lingering timer.
"""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from test_pins import ENTRY_DATA, HUB_IP

from conftest import FIXTURES
from custom_components.dirigera_platform.const import DOMAIN
from custom_components.dirigera_platform.hub_event_listener import SAFETY_NET_INTERVAL

FIXTURE = sorted(FIXTURES.glob("*.json"))[0].stem
BLIND_ID = "blind-test_1"


def _blind_like(template: dict) -> dict:
    """A FYRTUR in the hub's shape, built on a device of the fixture."""
    blind = copy.deepcopy(template)
    blind.update(id=BLIND_ID, deviceType="blinds", type="blinds", deviceSet=[], remoteLinks=[])
    blind.pop("relationId", None)
    blind["capabilities"] = {"canReceive": ["customName", "blindsTargetLevel", "blindsState"],
                             "canSend": []}
    blind["attributes"] = {
        "customName": "Blind", "model": "FYRTUR block-out roller blind",
        "manufacturer": "IKEA of Sweden", "firmwareVersion": "24.4.11",
        "hardwareVersion": "1", "serialNumber": "**REDACTED**", "productCode": "E1757",
        "blindsCurrentLevel": 0, "blindsTargetLevel": 0, "blindsState": "up",
        "batteryPercentage": 90,
    }
    return blind


class Net:
    def __init__(self, hass, hub):
        self.hass, self.hub = hass, hub
        self.entry = MockConfigEntry(domain=DOMAIN, title="safety net", data=dict(ENTRY_DATA),
                                     unique_id=HUB_IP)
        self.entry.add_to_hass(hass)
        self.env = next(d for d in hub.devices if d["deviceType"] == "environmentSensor")
        self.blind = next(d for d in hub.devices if d["id"] == BLIND_ID)
        self.fetches = 0
        real_get = hub.get

        def counting_get(route):
            if route.strip("/") == "devices":
                self.fetches += 1
            return real_get(route)
        hub.get = counting_get

    def state(self, unique_id: str):
        entries = er.async_entries_for_config_entry(er.async_get(self.hass), self.entry.entry_id)
        entity_id = next(e.entity_id for e in entries if e.unique_id == unique_id)
        return self.hass.states.get(entity_id)

    async def interval(self) -> None:
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=SAFETY_NET_INTERVAL + 1))
        await self.hass.async_block_till_done()


@pytest.fixture
async def net(hass, fake_hub, captured_listener) -> Net:
    hub = fake_hub(FIXTURE)
    env = next(d for d in hub.devices if d["deviceType"] == "environmentSensor")
    hub.devices.append(_blind_like(env))
    n = Net(hass, hub)
    assert await hass.config_entries.async_setup(n.entry.entry_id)
    await hass.async_block_till_done()
    return n


async def test_the_safety_net_restores_what_no_event_brought(net):
    env_id = net.env["id"]
    assert net.state(env_id + "MAXPM25").state == "1000"
    assert net.state(BLIND_ID).state == "open"

    # The hub changes, and no event comes.
    net.env["attributes"].update(currentTemperature=21.5, maxMeasuredPM25=999, minMeasuredPM25=1)
    # Closed with its remote: the hub's target stays where it was.
    net.blind["attributes"].update(blindsCurrentLevel=100, batteryPercentage=55)
    before = net.fetches
    await net.interval()

    assert net.fetches == before + 1  # one GET /devices for the whole hub
    assert float(net.state(env_id + "TEMP").state) == 21.5
    assert net.state(env_id + "MAXPM25").state == "999"
    assert net.state(env_id + "MINPM25").state == "1"
    cover = net.state(BLIND_ID)
    assert cover.state == "closed"  # not "closing": it moved while nobody looked
    assert cover.attributes["current_position"] == 0
    assert net.state(BLIND_ID + "BP01").state == "55"

    assert await net.hass.config_entries.async_unload(net.entry.entry_id)
    await net.hass.async_block_till_done()


async def test_unload_ends_the_safety_net(net):
    assert await net.hass.config_entries.async_unload(net.entry.entry_id)
    await net.hass.async_block_till_done()
    before = net.fetches
    await net.interval()
    await net.interval()
    assert net.fetches == before
