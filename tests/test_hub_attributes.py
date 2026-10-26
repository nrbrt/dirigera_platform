"""
Hub events update every attribute the entity's model has a field for.

Which attributes an event could change used to be a fixed list per deviceType
(process_events_from). Remotes (lightController, genericSwitch) were not on it,
so their battery level only changed on a reload (upstream #167); air purifiers
were not on it either (upstream #153). The list is gone: the entity's own
pydantic attributes model decides, and values go through that field's
validation, so datetimes and enums get their real type.

The remote cases and the split-device naming rule come from PR #66 by
Florian Hibler (fhibler).

hub_event_listener.py is loaded standalone, as in test_reconnect_resync.py. The
attribute models below follow the dirigera library's BaseIkeaModel.
"""
import asyncio
import datetime
import enum
import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace
from typing import Optional

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
_spec = importlib.util.spec_from_file_location("hub_event_listener_attrs_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener

HUB_KEY = "wss://hub.test:8443/v1"


def _to_camel(string):
    head, *rest = string.split("_")
    return head + "".join(word.capitalize() for word in rest)


class _Model(BaseModel, arbitrary_types_allowed=True, alias_generator=_to_camel):
    pass


def _make(model, **fields):
    """Build a model the way the library does: from the hub's camelCase keys."""
    return model(**{_to_camel(k): v for k, v in fields.items()})


class StartupEnum(enum.Enum):
    START_ON = "startOn"
    START_OFF = "startOff"


class FanModeEnum(enum.Enum):
    AUTO = "auto"
    LOW = "low"


class ControllerAttributes(_Model):
    custom_name: str
    is_on: Optional[bool] = None
    battery_percentage: Optional[int] = None


class OutletAttributes(_Model):
    custom_name: str
    is_on: bool
    startup_on_off: Optional[StartupEnum] = None
    total_energy_consumed: Optional[float] = None
    time_of_last_energy_reset: Optional[datetime.datetime] = None


class AirPurifierAttributes(_Model):
    custom_name: str
    fan_mode: FanModeEnum
    motor_state: int


class _Entity:
    def __init__(self, device_id, attributes, relation_id=None):
        self._json_data = SimpleNamespace(
            id=device_id, relation_id=relation_id, room=None, attributes=attributes
        )
        self.unique_id = device_id
        self.pushes = 0

    def schedule_update_ha_state(self, force_refresh=False):
        self.pushes += 1


def _listener(discovered=None):
    asyncio.set_event_loop(asyncio.new_event_loop())
    coordinator = None
    if discovered is not None:
        coordinator = SimpleNamespace(discover_device=lambda i, t: discovered.append((i, t)))
    listener = hub_event_listener(SimpleNamespace(websocket_base_url=HUB_KEY), SimpleNamespace(), coordinator)
    listener._loop = SimpleNamespace(call_soon_threadsafe=lambda f: f(), is_closed=lambda: False)
    listener._hass = SimpleNamespace(async_create_task=lambda result: result)
    listener._update_device_name = lambda device_id, name: None
    listener._update_device_area = lambda device_id, room: None
    return listener


def _event(device_id, device_type, type_=None, **attributes):
    data = {"id": device_id, "deviceType": device_type, "attributes": attributes}
    if type_:
        data["type"] = type_
    return json.dumps({"type": "deviceStateChanged", "data": data})


def _register(entity, *ids):
    hub_event_listener.unregister_hub(HUB_KEY)
    for device_id in ids:
        hub_event_listener.device_registry.setdefault(HUB_KEY, {})[device_id] = hel.registry_entry(entity)


def _remote(name="Hal remote", battery=1):
    return _Entity("remote_1", _make(ControllerAttributes, custom_name=name, battery_percentage=battery))


def test_light_controller_battery_event_reaches_the_entity():
    remote = _remote()
    _register(remote, "remote_1")
    _listener().on_message(None, _event("remote_1", "lightController", batteryPercentage=90))
    assert remote._json_data.attributes.battery_percentage == 90
    assert remote.pushes == 1


def test_generic_switch_battery_event_reaches_the_entity():
    remote = _remote(battery=40)
    _register(remote, "remote_1")
    _listener().on_message(None, _event("remote_1", "genericSwitch", batteryPercentage=35))
    assert remote._json_data.attributes.battery_percentage == 35
    assert remote.pushes == 1


def test_other_half_updates_battery_but_not_the_name():
    # BILRESA: the _2 half is routed to the primary's entity (sensor.py) but
    # keeps its own name on the hub.
    remote = _remote(name="Keuken remote", battery=100)
    _register(remote, "remote_1", "remote_2")
    _listener().on_message(
        None, _event("remote_2", "genericSwitch", batteryPercentage=95, customName="Keuken scroll")
    )
    assert remote._json_data.attributes.battery_percentage == 95
    assert remote._json_data.attributes.custom_name == "Keuken remote"


def test_own_event_still_renames():
    remote = _remote(name="Old")
    _register(remote, "remote_1")
    listener = _listener()
    renamed = []
    listener._update_device_name = lambda device_id, name: renamed.append((device_id, name))
    listener.on_message(None, _event("remote_1", "lightController", customName="New"))
    assert remote._json_data.attributes.custom_name == "New"
    assert renamed == [("remote_1", "New")]


def test_air_purifier_event_updates_the_model():
    purifier = _Entity("purifier_1", _make(AirPurifierAttributes, custom_name="P", fan_mode="auto", motor_state=0))
    _register(purifier, "purifier_1")
    _listener().on_message(None, _event("purifier_1", "airPurifier", fanMode="low", motorState=20))
    assert purifier._json_data.attributes.fan_mode is FanModeEnum.LOW
    assert purifier._json_data.attributes.motor_state == 20
    assert purifier.pushes == 1


def test_values_get_the_type_of_their_field():
    plug = _Entity("plug_1", _make(OutletAttributes, custom_name="P", is_on=False))
    _register(plug, "plug_1")
    _listener().on_message(None, _event(
        "plug_1", "outlet",
        startupOnOff="startOn", timeOfLastEnergyReset="2026-10-01T10:00:00.000Z",
    ))
    attrs = plug._json_data.attributes
    assert attrs.startup_on_off is StartupEnum.START_ON
    assert attrs.time_of_last_energy_reset == datetime.datetime(2026, 10, 1, 10, tzinfo=datetime.timezone.utc)


def test_attribute_the_model_lacks_is_skipped():
    remote = _remote()
    _register(remote, "remote_1")
    _listener().on_message(None, _event("remote_1", "lightController", circadianPresets=[], batteryPercentage=50))
    assert remote._json_data.attributes.battery_percentage == 50
    assert not hasattr(remote._json_data.attributes, "circadian_presets")


def test_value_that_does_not_fit_keeps_the_old_one():
    plug = _Entity("plug_1", _make(OutletAttributes, custom_name="P", is_on=True))
    _register(plug, "plug_1")
    _listener().on_message(None, _event("plug_1", "outlet", isOn=None, customName="Q"))
    assert plug._json_data.attributes.is_on is True
    assert plug._json_data.attributes.custom_name == "Q"


def test_energy_decrease_is_still_held_back():
    plug = _Entity("plug_1", _make(OutletAttributes, custom_name="P", is_on=True, total_energy_consumed=5.9))
    _register(plug, "plug_1")
    _listener().on_message(None, _event("plug_1", "outlet", totalEnergyConsumed=5.0))
    assert plug._json_data.attributes.total_energy_consumed == 5.9


def test_unknown_remote_goes_to_discovery_as_controller():
    hub_event_listener.unregister_hub(HUB_KEY)
    discovered = []
    _listener(discovered).on_message(
        None, _event("new_1", "lightController", type_="controller", batteryPercentage=80)
    )
    assert discovered == [("new_1", "controller")]


def test_scene_action_uses_the_same_path():
    plug = _Entity("plug_1", _make(OutletAttributes, custom_name="P", is_on=False))
    _register(plug, "plug_1")
    _listener()._apply_scene_actions({"data": {"actions": [
        {"type": "device", "deviceId": "plug_1", "attributes": {"isOn": True}},
    ]}})
    assert plug._json_data.attributes.is_on is True
    assert plug.pushes == 1


def test_other_half_does_not_erase_the_battery():
    # Review of the refactor: a half replaying batteryPercentage null wiped the
    # primary's level (80 -> None) once the type filter no longer stopped it.
    remote = _remote(battery=80)
    _register(remote, "remote_1", "remote_2")
    _listener().on_message(None, _event("remote_2", "genericSwitch", batteryPercentage=None))
    assert remote._json_data.attributes.battery_percentage == 80


def test_own_null_is_still_applied():
    remote = _remote(battery=80)
    _register(remote, "remote_1")
    _listener().on_message(None, _event("remote_1", "lightController", batteryPercentage=None))
    assert remote._json_data.attributes.battery_percentage is None


def _room_event(device_id, room):
    return json.dumps({"type": "deviceStateChanged", "data": {
        "id": device_id, "deviceType": "genericSwitch", "room": room}})


def test_other_half_does_not_clear_the_room():
    # The hub keeps the room on one part only (seen on a live hub for BILRESA,
    # GRILLPLATS and MYGGSPRAY).
    remote = _remote()
    remote._json_data.room = "Hal"
    _register(remote, "remote_1", "remote_2")
    _listener().on_message(None, _room_event("remote_2", None))
    assert remote._json_data.room == "Hal"


def test_own_part_clears_the_room():
    remote = _remote()
    remote._json_data.room = "Hal"
    _register(remote, "remote_1")
    _listener().on_message(None, _room_event("remote_1", None))
    assert remote._json_data.room is None
