"""
TDD for per-hub device registry isolation (issue #39, the real multi-hub bug).

Root cause: hub_event_listener.device_registry is a single class-level dict
shared by every hub, and stop() calls device_registry.clear(). With two hubs,
unloading/reloading one config entry wipes the other hub's registrations too, so
afterwards every event for the other hub's real devices (lights, outlets, motion)
becomes "Unknown device detected" and its state is never applied to the entity.
That is the "none of them update, not device-specific" symptom.

Fix: key the registry per hub (by the hub's websocket_base_url, which both the
entity hub and the listener hub share because both are built from the same entry
IP). Stopping one hub removes only its own sub-registry.

The module is loaded standalone with the third-party imports stubbed, so the real
register/get_registry_entry/unregister logic runs unchanged.
"""
import asyncio
import importlib.util
import os
import sys
import types


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
_ha_light = _stub("homeassistant.components.light", ColorMode=type("ColorMode", (), {}))
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

asyncio.set_event_loop(asyncio.new_event_loop())
_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "dirigera_platform", "hub_event_listener.py"
)
_spec = importlib.util.spec_from_file_location("hel_registry_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener
registry_entry = hel.registry_entry


class _Ent:
    def __init__(self, uid):
        self.unique_id = uid


def _fresh():
    hub_event_listener.device_registry.clear()


def test_registry_is_isolated_per_hub_key():
    _fresh()
    eA = registry_entry(_Ent("dev1"))
    eB = registry_entry(_Ent("dev2"))
    hub_event_listener.register("wss://hubA/v1", "dev1", eA)
    hub_event_listener.register("wss://hubB/v1", "dev2", eB)

    assert hub_event_listener.get_registry_entry("wss://hubA/v1", "dev1") is eA
    assert hub_event_listener.get_registry_entry("wss://hubB/v1", "dev2") is eB
    # a device of hub A must not be resolvable under hub B's key (and vice versa)
    assert hub_event_listener.get_registry_entry("wss://hubB/v1", "dev1") is None
    assert hub_event_listener.get_registry_entry("wss://hubA/v1", "dev2") is None


def test_unregister_one_hub_leaves_other_hub_intact():
    """The actual bug: stopping/unloading one hub must not wipe the other's devices."""
    _fresh()
    eA = registry_entry(_Ent("dev1"))
    eB = registry_entry(_Ent("dev2"))
    hub_event_listener.register("wss://hubA/v1", "dev1", eA)
    hub_event_listener.register("wss://hubB/v1", "dev2", eB)

    hub_event_listener.unregister_hub("wss://hubA/v1")

    assert hub_event_listener.get_registry_entry("wss://hubA/v1", "dev1") is None
    # hub B's device must still resolve, so its events keep updating its entity
    assert hub_event_listener.get_registry_entry("wss://hubB/v1", "dev2") is eB
