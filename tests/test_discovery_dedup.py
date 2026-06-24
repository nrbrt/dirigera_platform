"""
Reproduction + regression test for the runtime re-discovery loop (issue #39).

Context (a HYPOTHESIS, not a proven root cause of Semir's status-stall):
the hub emits deviceStateChanged events for environment-sensor sub-devices
(`<uuid>_2`) that aren't registered as entities. The listener can't route them
to a parent, so it calls discover_device(). For device types whose entity
creation is a stub (environmentSensor, controller) `_create_entity` returns
None, the device is NEVER added to `_known_device_ids`, and so the *next* event
re-runs the whole thing -- including a blocking `GET /devices/{id}` against the
hub. With chatty env sensors that is hundreds of redundant hub fetches.

This test pins the *caching contract* the fix introduces: a device whose
discovery yields no entity must not be re-fetched on every subsequent event.

The module is loaded standalone (not via the package __init__, which imports
the full Home Assistant stack). device_discovery.py's only module-level HA
dependency is `from homeassistant import core`, used solely for a type hint --
we stub that boundary so the real coordinator logic runs unchanged.
"""
import asyncio
import importlib.util
import os
import sys
import types

# --- stub the homeassistant.core boundary (type-hint only) ---------------
_ha = types.ModuleType("homeassistant")
_ha_core = types.ModuleType("homeassistant.core")
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha.core = _ha_core
sys.modules.setdefault("homeassistant", _ha)
sys.modules.setdefault("homeassistant.core", _ha_core)

# --- load the real device_discovery module standalone --------------------
_MODULE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "dirigera_platform",
    "device_discovery.py",
)
_spec = importlib.util.spec_from_file_location("device_discovery_under_test", _MODULE_PATH)
device_discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(device_discovery)
DeviceDiscoveryCoordinator = device_discovery.DeviceDiscoveryCoordinator


class FakeHub:
    """Records every /devices fetch so we can count redundant hub calls."""

    def __init__(self):
        self.get_calls = []

    def get(self, path):
        self.get_calls.append(path)
        # shape mirrors a real device payload enough for discover_device
        return {"id": path.rsplit("/", 1)[-1], "deviceType": "environmentSensor", "attributes": {}}


class FakeHass:
    """Runs executor jobs inline so the test stays synchronous."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_coordinator():
    hub = FakeHub()
    coord = DeviceDiscoveryCoordinator(FakeHass(), hub)
    coord.register_platform_callback("sensor", lambda entities: None)

    # Scenario injection: an unsupported device type -> entity creation yields
    # None. This is exactly what the environmentSensor branch does today
    # (device_discovery.py: "Environment sensor discovery not yet implemented").
    async def _no_entity(device_type, device_data):
        return None

    coord._create_entity = _no_entity
    return coord, hub


def test_unsupported_device_is_not_refetched_on_every_event():
    coord, hub = _make_coordinator()
    dev_id = "5ac3e411-5369-4b65-ba88-97c0f864df9c_2"

    asyncio.run(coord.discover_device(dev_id, "environmentSensor"))
    asyncio.run(coord.discover_device(dev_id, "environmentSensor"))
    asyncio.run(coord.discover_device(dev_id, "environmentSensor"))

    assert len(hub.get_calls) == 1, (
        f"expected the failed discovery to be cached (1 hub fetch), "
        f"got {len(hub.get_calls)} fetches: {hub.get_calls}"
    )


def test_successful_device_is_discovered_once_and_not_marked_unsupported():
    """Guard: the unsupported-cache must not swallow devices that DO create."""
    hub = FakeHub()
    coord = DeviceDiscoveryCoordinator(FakeHass(), hub)
    coord.register_platform_callback("sensor", lambda entities: None)

    async def _real_entity(device_type, device_data):
        return object()  # a non-None "entity"

    coord._create_entity = _real_entity

    dev_id = "abc-123_1"
    first = asyncio.run(coord.discover_device(dev_id, "lightSensor"))
    second = asyncio.run(coord.discover_device(dev_id, "lightSensor"))

    assert first is True, "first discovery of a creatable device should succeed"
    assert second is False, "second call should short-circuit via the known-cache"
    assert len(hub.get_calls) == 1, "a known device must not be re-fetched"
    assert dev_id in coord._known_device_ids
    assert dev_id not in coord._unsupported_device_ids, (
        "a successfully created device must never be cached as unsupported"
    )
