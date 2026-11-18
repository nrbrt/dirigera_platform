"""replace_model keeps the attributes a command wrote during the fetch.

A single-device refresh (the air purifier's poll, update_entity) replaces the
entity's model with the fetched one. When a command wrote to the entity
while that fetch was on its way (note_command), its whole attributes object
is newer and stays. Run here, not in tests/, because it needs the real
dirigera models: the assignment of one model's attributes to another is
what pydantic has to accept.
"""
from __future__ import annotations

import json

from dirigera.devices.outlet import dict_to_outlet
from dirigera.hub.hub import Hub

from conftest import FIXTURES
from custom_components.dirigera_platform.hub_event_listener import live_writes, note_command, replace_model

FIXTURE = sorted(FIXTURES.glob("*.json"))[0]
OUTLET = "device-08_1"


class _Entity:
    """What replace_model and live_writes need of an entity: its model."""

    def __init__(self, model):
        self._json_data = model


def _outlet():
    row = next(d for d in json.loads(FIXTURE.read_text(encoding="utf-8"))["devices"] if d["id"] == OUTLET)
    row["attributes"]["isOn"] = False
    hub = Hub("token", "192.0.2.1")
    return dict_to_outlet(row, hub), dict_to_outlet(row, hub)


def test_a_commanded_entity_keeps_its_attributes_over_the_fetched_ones():
    old, fetched = _outlet()
    entity = _Entity(old)
    with live_writes() as newer:
        old.attributes.is_on = True  # what the command's setter wrote
        note_command(entity)
    fetched.is_reachable = False      # a field of the device itself still applies
    replace_model(entity, fetched, newer)
    assert entity._json_data is fetched
    assert entity._json_data.attributes.is_on is True
    assert entity._json_data.is_reachable is False


def test_without_a_command_the_fetched_attributes_win():
    old, fetched = _outlet()
    entity = _Entity(old)
    with live_writes() as newer:
        old.attributes.is_on = True   # before the fetch, not by a command in it
    replace_model(entity, fetched, newer)
    assert entity._json_data.attributes.is_on is False
