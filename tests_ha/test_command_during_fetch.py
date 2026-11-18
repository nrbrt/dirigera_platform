"""A command during the safety net's fetch is not undone by its snapshot.

The safety net (#207) fetches /devices every few minutes. The snapshot is as
old as the moment the hub built it; a command the hub accepted while the
fetch was on its way is newer. Here the fetch is held inside
_resync_all_states, a command runs, and then the fetch returns the hub's
older row. The entity must keep what the command set, for an outlet (which
goes through ikea_base_device._command) and a light (ikea_bulb._send, with
its own pending record for the hub's echo).
"""
from __future__ import annotations

import threading

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pins import ENTRY_DATA, HUB_IP

from conftest import FIXTURES
from custom_components.dirigera_platform import pending_command
from custom_components.dirigera_platform.const import DOMAIN

FIXTURE = sorted(FIXTURES.glob("*.json"))[0].stem
OUTLET = "device-08_1"
LIGHT = "device-01_1"


class Held:
    """GET /devices answers with the hub as it was when asked, but only
    once the test lets it go."""

    def __init__(self, hub):
        self.asked, self.release = threading.Event(), threading.Event()
        self.holding = False
        real_get = hub.get

        def get(route):
            answer = real_get(route)  # the snapshot is built now
            if self.holding and route.strip("/") == "devices":
                self.asked.set()
                assert self.release.wait(10), "the test never released the fetch"
            return answer
        hub.get = get


@pytest.fixture
async def setup(hass, fake_hub, captured_listener):
    hub = fake_hub(FIXTURE)
    light = next(d for d in hub.devices if d["id"] == LIGHT)
    light["attributes"]["isOn"] = False
    held = Held(hub)
    entry = MockConfigEntry(domain=DOMAIN, title="command during fetch", data=dict(ENTRY_DATA),
                            unique_id=HUB_IP)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    held.holding = True
    yield hass, entry, held, captured_listener[0]
    held.release.set()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass, entry, domain, unique_id):
    return next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.domain == domain and e.unique_id == unique_id)


async def _command_during_fetch(hass, held, listener, domain, entity_id, monkeypatch=None):
    run = hass.async_create_task(listener.run_safety_net())
    assert await hass.async_add_executor_job(held.asked.wait, 10)
    await hass.services.async_call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
    if monkeypatch is not None:
        # The hub never echoes here, so the light's pending record would keep
        # the old isOn out for its window: move past it, as a fetch slower
        # than ECHO_WINDOW_S would.
        later = pending_command._now() + 10 * pending_command.ECHO_WINDOW_S
        monkeypatch.setattr(pending_command, "_now", lambda: later)
    held.release.set()
    await run
    await hass.async_block_till_done()


async def test_an_outlet_switched_on_during_the_fetch_stays_on(setup):
    hass, entry, held, listener = setup
    entity_id = _entity_id(hass, entry, "switch", OUTLET)
    assert hass.states.get(entity_id).state == "off"
    await _command_during_fetch(hass, held, listener, "switch", entity_id)
    assert hass.states.get(entity_id).state == "on"


async def test_a_light_switched_on_during_a_slow_fetch_stays_on(setup, monkeypatch):
    hass, entry, held, listener = setup
    entity_id = _entity_id(hass, entry, "light", LIGHT)
    assert hass.states.get(entity_id).state == "off"
    await _command_during_fetch(hass, held, listener, "light", entity_id, monkeypatch)
    assert hass.states.get(entity_id).state == "on"
