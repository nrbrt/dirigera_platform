"""
The top-level fields of a hub event go through the device's model, like its attributes.

`isReachable` and `room` were applied by hand in the listener: one branch per
field, a Room built key by key, and a room compared on its id only. A room
renamed on the hub (same id, new name) therefore never reached the model, the
other top-level fields an event carries (lastSeen, isHidden) were dropped, and
the Home Assistant area was written again for every event that carried a room,
changed or not, so every reconnect resync moved every device back to its IKEA
room.

The device's own pydantic model decides now: a top-level field is applied when
the model has it, through that field's validation. What follows from a change
(the area, the state write) is derived from the device before and after.

`deviceSet` is not applied from events, as before. Another part of a split
device adds what the parts share (reachability, a room) and never erases.

hub_event_listener.py is loaded standalone, as in test_hub_attributes.py. The
models below follow the dirigera library's Device and Room.
"""
import asyncio
import datetime
import importlib.util
import json
import logging
import os
import sys
import types
from types import SimpleNamespace
from typing import List, Optional

from pydantic import BaseModel


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_stub("websocket", WebSocketApp=object)
_dir = _stub("dirigera", Hub=object)
_dir_dev = _stub("dirigera.devices")
_dir_dev_device = _stub("dirigera.devices.device", Room=object)
_dir.devices = _dir_dev
_dir_dev.device = _dir_dev_device

_ha = _stub("homeassistant")
_ha_const = _stub("homeassistant.const", ATTR_ENTITY_ID="entity_id")
_ha_comp = _stub("homeassistant.components")
_ColorMode = type("ColorMode", (), {"HS": "hs", "COLOR_TEMP": "color_temp"})
_ha_light = _stub("homeassistant.components.light", ColorMode=_ColorMode)
_ha_helpers = _stub(
    "homeassistant.helpers",
    device_registry=types.ModuleType("dr"),
    entity_registry=types.ModuleType("er"),
    area_registry=types.ModuleType("ar"),
)
_ha.const = _ha_const
_ha.components = _ha_comp
_ha_comp.light = _ha_light
_ha.helpers = _ha_helpers

asyncio.set_event_loop(asyncio.new_event_loop())  # __init__ calls get_event_loop()
_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "dirigera_platform", "hub_event_listener.py"
)
_spec = importlib.util.spec_from_file_location("hub_event_listener_fields_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener

HUB_KEY = "wss://hub.test:8443/v1"


def _to_camel(string):
    head, *rest = string.split("_")
    return head + "".join(word.capitalize() for word in rest)


class _Model(BaseModel, arbitrary_types_allowed=True, alias_generator=_to_camel):
    pass


class Attributes(_Model):
    custom_name: str
    is_on: Optional[bool] = None
    battery_percentage: Optional[int] = None


class Capabilities(_Model):
    can_send: List[str]
    can_receive: List[str]


class Room(_Model):
    id: str
    name: str
    color: str
    icon: str


class Device(_Model):
    id: str
    relation_id: Optional[str] = None
    type: str
    device_type: str
    created_at: datetime.datetime
    is_reachable: bool
    last_seen: datetime.datetime
    attributes: Attributes
    capabilities: Capabilities
    room: Optional[Room] = None
    device_set: List
    remote_links: List[str]
    is_hidden: Optional[bool] = None


HAL = {"id": "r1", "name": "Hal", "color": "ikea_green_no_65", "icon": "rooms_hall"}
KEUKEN = {"id": "r2", "name": "Keuken", "color": "ikea_yellow_no_24", "icon": "rooms_cutlery"}
SEEN = "2026-11-10T08:00:00.000Z"


def _device(device_id="d_1", relation_id=None, room=HAL, reachable=True):
    return Device(**{
        "id": device_id, "relationId": relation_id, "type": "controller",
        "deviceType": "lightController", "createdAt": "2026-01-01T00:00:00.000Z",
        "isReachable": reachable, "lastSeen": SEEN,
        "attributes": {"customName": "Knop", "batteryPercentage": 80},
        "capabilities": {"canSend": ["isOn"], "canReceive": ["customName"]},
        "room": room, "deviceSet": [{"id": "set1", "name": "Plafond", "icon": "lighting_chandelier"}],
        "remoteLinks": [],
    })


class _Entity:
    def __init__(self, json_data):
        self._json_data = json_data
        self.unique_id = json_data.id
        self.writes = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1


def _setup(*entities, also=None):
    """A listener with the entities registered; `also` maps an unregistered
    part's id to nothing, it is routed by the listener itself."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    hub_event_listener.device_registry.pop(HUB_KEY, None)
    for entity in entities:
        hub_event_listener.register(HUB_KEY, entity._json_data.id, hel.registry_entry(entity))
    listener = hub_event_listener(SimpleNamespace(websocket_base_url=HUB_KEY), SimpleNamespace(), None)
    listener._loop = SimpleNamespace(call_soon_threadsafe=lambda f, *args: f(*args))
    listener._hass = SimpleNamespace(async_create_task=lambda result: result)
    areas, names = [], []
    listener._update_device_area = lambda device_id, room: areas.append((device_id, room))
    listener._update_device_name = lambda device_id, name: names.append((device_id, name))
    return SimpleNamespace(listener=listener, areas=areas, names=names)


def _send(listener, device_id, device_type="lightController", **fields):
    listener.on_message(None, json.dumps({"type": "deviceStateChanged", "data": {
        "id": device_id, "type": "controller", "deviceType": device_type, **fields}}))


# --- the model decides ---------------------------------------------------------

def test_reachability_goes_through_the_model():
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", isReachable=False)
    assert entity._json_data.is_reachable is False
    assert entity.writes == 1


def test_a_value_that_does_not_fit_the_field_keeps_the_old_one(caplog):
    entity = _Entity(_device())
    s = _setup(entity)
    with caplog.at_level(logging.WARNING):
        _send(s.listener, "d_1", isReachable="perhaps")
    assert entity._json_data.is_reachable is True
    assert "is_reachable" in caplog.text


def test_other_fields_of_the_model_are_applied_with_their_type():
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", lastSeen="2026-11-10T09:30:00.000Z", isHidden=True)
    assert entity._json_data.last_seen == datetime.datetime(
        2026, 11, 10, 9, 30, tzinfo=datetime.timezone.utc)
    assert entity._json_data.is_hidden is True


def test_a_key_the_model_does_not_have_is_ignored():
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", somethingNew={"a": 1}, isReachable=False)
    assert entity._json_data.is_reachable is False
    assert not hasattr(entity._json_data, "something_new")


def test_the_room_becomes_a_room_of_the_model():
    entity = _Entity(_device(room=None))
    s = _setup(entity)
    _send(s.listener, "d_1", room=KEUKEN)
    assert entity._json_data.room == Room(**KEUKEN)


def test_attributes_are_not_replaced_by_the_generic_path():
    # `attributes` in an event is partial; it has its own path.
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", attributes={"batteryPercentage": 55})
    assert isinstance(entity._json_data.attributes, Attributes)
    assert entity._json_data.attributes.battery_percentage == 55
    assert entity._json_data.attributes.custom_name == "Knop"


def test_device_set_is_left_alone():
    # Not applied from events, as before (sets are handled elsewhere).
    entity = _Entity(_device())
    before = list(entity._json_data.device_set)
    s = _setup(entity)
    _send(s.listener, "d_1", deviceSet=[], isReachable=True)
    assert entity._json_data.device_set == before


# --- what follows from a change is derived from before and after ----------------

def test_room_change_moves_the_device_to_that_area():
    entity = _Entity(_device(room=HAL))
    s = _setup(entity)
    _send(s.listener, "d_1", room=KEUKEN)
    assert s.areas == [("d_1", "Keuken")]
    assert entity.writes == 1


def test_room_renamed_on_the_hub_reaches_the_model_and_is_no_room_change():
    # Same room id, new name: the model takes the new name (an id-only
    # comparison of the fields kept the old Room), but it is the same room,
    # so this is not the room-change path. Which devices follow a rename is
    # decided against the last known room name; tests_ha/test_areas.py.
    entity = _Entity(_device(room=HAL))
    s = _setup(entity)
    _send(s.listener, "d_1", room={**HAL, "name": "Gang"})
    assert entity._json_data.room.name == "Gang"
    assert s.areas == []


def test_unchanged_room_does_not_touch_the_area():
    # Every reconnect resync replays every device with its room; that must not
    # move a device the user put in another Home Assistant area.
    entity = _Entity(_device(room=HAL))
    s = _setup(entity)
    _send(s.listener, "d_1", room=HAL, isReachable=True)
    assert s.areas == []


def test_room_removed_clears_the_area():
    entity = _Entity(_device(room=HAL))
    s = _setup(entity)
    _send(s.listener, "d_1", room=None)
    assert entity._json_data.room is None
    assert s.areas == [("d_1", "")]


def test_area_is_that_of_the_physical_device():
    entity = _Entity(_device("rel_1", relation_id="rel", room=HAL))
    s = _setup(entity)
    _send(s.listener, "rel_1", room=KEUKEN)
    assert s.areas == [("rel", "Keuken")]


def test_an_event_that_changes_nothing_writes_no_state():
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", isReachable=True, lastSeen=SEEN, room=HAL)
    assert entity.writes == 0


def test_an_event_with_attributes_always_writes_state():
    entity = _Entity(_device())
    s = _setup(entity)
    _send(s.listener, "d_1", isReachable=True, attributes={"batteryPercentage": 80})
    assert entity.writes == 1


# --- another part of a split device adds, never erases or renames ---------------

def _split():
    """<rel>_1 is registered; events of <rel>_2 are routed to it."""
    entity = _Entity(_device("rel_1", relation_id="rel", room=HAL))
    entity._json_data.device_type = "outlet"
    return entity


def test_other_part_does_not_clear_the_room():
    entity = _split()
    s = _setup(entity)
    _send(s.listener, "rel_2", device_type="electricalSensor", room=None)
    assert entity._json_data.room == Room(**HAL)
    assert s.areas == []


def test_other_part_can_set_the_room():
    entity = _split()
    entity._json_data.room = None
    s = _setup(entity)
    _send(s.listener, "rel_2", device_type="electricalSensor", room=KEUKEN)
    assert entity._json_data.room == Room(**KEUKEN)
    assert s.areas == [("rel", "Keuken")]


def test_other_part_reports_reachability():
    entity = _split()
    s = _setup(entity)
    _send(s.listener, "rel_2", device_type="electricalSensor", isReachable=False)
    assert entity._json_data.is_reachable is False


def test_other_part_does_not_overwrite_what_describes_this_part():
    # The resync replays the whole device of every part: capabilities, links
    # and the hidden flag of <rel>_2 are its own, like its customName.
    entity = _split()
    s = _setup(entity)
    _send(s.listener, "rel_2", device_type="electricalSensor",
          capabilities={"canSend": [], "canReceive": []},
          remoteLinks=["x"], isHidden=True, lastSeen="2026-11-10T09:30:00.000Z",
          createdAt="2026-02-02T00:00:00.000Z")
    data = entity._json_data
    assert data.id == "rel_1"
    assert data.device_type == "outlet"
    assert data.capabilities.can_receive == ["customName"]
    assert data.remote_links == []
    assert data.is_hidden is None
    assert data.created_at.year == 2026 and data.created_at.month == 1
