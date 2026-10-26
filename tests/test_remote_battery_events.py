"""
Remote battery levels only changed on a config-entry reload.

A remote's HA entity (ikea_controller_device) does not poll: skip_update is set
and async_update is a no-op, so the hub's deviceStateChanged push is its only
update path. on_message drops any event whose device type is not a key of
process_events_from, and the remote types lightController (STYRBAR, RODRET,
SOMRIG, TRADFRI remotes) and genericSwitch (BILRESA) were not listed. Every
battery update for a remote was therefore discarded, and the level stayed at
whatever the last reload fetched.

Only batteryPercentage is taken for remotes. A multi-button controller is
several hub devices sharing a relationId, and sensor.py routes every half's
events to the one primary entity. The halves keep their own customName on the
hub, so applying customName would rename the HA device to whichever half
reported last.

hub_event_listener.py is loaded standalone, as in test_reconnect_resync.py.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# --- stub module-level imports of hub_event_listener.py ------------------
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

# --- load the real module standalone -------------------------------------
asyncio.set_event_loop(asyncio.new_event_loop())  # __init__ calls get_event_loop()
_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "dirigera_platform", "hub_event_listener.py"
)
_spec = importlib.util.spec_from_file_location("hub_event_listener_remote_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener

HUB_KEY = "wss://hub.test:8443/v1"


class _Remote:
    """Stands in for ikea_controller_device: its own HA entity."""

    def __init__(self, custom_name, battery):
        self._json_data = SimpleNamespace(
            id="remote_1",
            relation_id=None,
            room=None,
            attributes=SimpleNamespace(custom_name=custom_name, battery_percentage=battery),
        )
        self.unique_id = "remote_1"
        self.pushes = 0

    def schedule_update_ha_state(self, force_refresh=False):
        self.pushes += 1


def _listener():
    asyncio.set_event_loop(asyncio.new_event_loop())
    return hub_event_listener(SimpleNamespace(websocket_base_url=HUB_KEY), SimpleNamespace())


def _event(device_id, device_type, **attributes):
    return json.dumps({
        "type": "deviceStateChanged",
        "data": {"id": device_id, "deviceType": device_type, "attributes": attributes},
    })


def _register(remote, *ids):
    hub_event_listener.unregister_hub(HUB_KEY)
    for device_id in ids:
        hub_event_listener.device_registry.setdefault(HUB_KEY, {})[device_id] = hel.registry_entry(remote)


def test_light_controller_battery_event_reaches_the_entity():
    remote = _Remote("Flur Remote", 1)
    _register(remote, "remote_1")

    _listener().on_message(None, _event("remote_1", "lightController", batteryPercentage=90))

    assert remote._json_data.attributes.battery_percentage == 90
    assert remote.pushes == 1


def test_generic_switch_battery_event_reaches_the_entity():
    remote = _Remote("Küche Remote", 40)
    _register(remote, "remote_1")

    _listener().on_message(None, _event("remote_1", "genericSwitch", batteryPercentage=35))

    assert remote._json_data.attributes.battery_percentage == 35
    assert remote.pushes == 1


def test_secondary_half_does_not_rename_the_primary():
    # BILRESA: the _2 half is routed to the primary's entity (sensor.py) but
    # still carries its own name on the hub.
    remote = _Remote("Küche Remote", 100)
    _register(remote, "remote_1", "remote_2")

    _listener().on_message(
        None, _event("remote_2", "genericSwitch", batteryPercentage=95, customName="Küche Fernbedienung")
    )

    assert remote._json_data.attributes.battery_percentage == 95
    assert remote._json_data.attributes.custom_name == "Küche Remote"
