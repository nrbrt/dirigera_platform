"""
Regression test: a plug paired while HA runs must get its power/energy sensors.

Runtime discovery only built the switch for an outlet. The power/energy sensors
were built once at startup in sensor.py, so a freshly paired GRILLPLATS showed
no energy until the next restart, even though the #31 merge had already put the
electricalSensor attributes on the outlet.

The fix lets sensor.py register a companion factory for "outlet"; discovery
calls it with the new device's wrapper and adds the result through the sensor
callback. The module is loaded standalone, like test_discovery_dedup.py.
"""
import asyncio
import importlib.util
import os
import sys
import types

_ha = types.ModuleType("homeassistant")
_ha_core = types.ModuleType("homeassistant.core")
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha.core = _ha_core
sys.modules.setdefault("homeassistant", _ha)
sys.modules.setdefault("homeassistant.core", _ha_core)

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "dirigera_platform", "device_discovery.py"
)
_spec = importlib.util.spec_from_file_location("device_discovery_companions", _MODULE_PATH)
device_discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(device_discovery)
DeviceDiscoveryCoordinator = device_discovery.DeviceDiscoveryCoordinator


class FakeHub:
    def get(self, path):
        return {"id": path.rsplit("/", 1)[-1], "deviceType": "outlet", "attributes": {}}

    def is_excluded_matter_device(self, device):
        return False


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeSwitch:
    def __init__(self, wrapper):
        self._device = wrapper


def _make(register_factory=True):
    coord = DeviceDiscoveryCoordinator(FakeHass(), FakeHub())
    added = {"switch": [], "sensor": []}
    coord.register_platform_callback("switch", added["switch"].extend)
    coord.register_platform_callback("sensor", added["sensor"].extend)
    wrapper = object()

    async def _switch(device_type, device_data):
        return FakeSwitch(wrapper)

    coord._create_entity = _switch
    seen = []
    if register_factory:
        def factory(w):
            seen.append(w)
            return ["power", "energy"]
        coord.register_companion_factory("outlet", factory)
    return coord, added, wrapper, seen


def test_runtime_outlet_gets_its_energy_sensors():
    coord, added, wrapper, seen = _make()
    assert asyncio.run(coord.discover_device("ee7c919f_1", "outlet")) is True
    assert len(added["switch"]) == 1
    assert added["sensor"] == ["power", "energy"]
    assert seen == [wrapper], "the factory must get the new device's own wrapper"


def test_no_factory_means_switch_only():
    """Guard: without a registered factory discovery behaves as before."""
    coord, added, _, _ = _make(register_factory=False)
    assert asyncio.run(coord.discover_device("ee7c919f_1", "outlet")) is True
    assert len(added["switch"]) == 1
    assert added["sensor"] == []


def test_factory_is_per_device_type():
    coord, added, _, seen = _make()
    asyncio.run(coord.discover_device("abc_1", "light"))
    assert seen == [] and added["sensor"] == []
