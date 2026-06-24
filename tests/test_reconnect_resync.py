"""
TDD for the WebSocket-reconnect state resync (issue #39, the real stall).

Mechanism being fixed: the hub drops the WebSocket every ~5-6 min. On reconnect
the listener only waits for new events -- it never re-pulls current device state
-- so any change during the gap is lost and entities silently go stale. A config
reload fixes it temporarily because a reload re-fetches all states; this makes
that happen automatically on every reconnect.

Approach: on a *reconnect* open (not the first), fetch /devices once and replay
each device through the existing on_message() update path, with discovery
suppressed so unknown devices don't trigger a discovery storm on every reconnect.

hub_event_listener.py is loaded standalone (not via the package __init__, which
pulls the full HA stack). We stub the module-level third-party imports so the
real listener logic runs unchanged.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types


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
_spec = importlib.util.spec_from_file_location("hub_event_listener_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener


class FakeHub:
    def __init__(self, devices=None):
        self._devices = devices or []
        self.get_calls = []

    def get(self, path):
        self.get_calls.append(path)
        return self._devices


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make(devices=None):
    # __init__ calls asyncio.get_event_loop(); in real HA a loop is always
    # running. asyncio.run() in a prior test closes the loop, so give each
    # construction a fresh current loop.
    asyncio.set_event_loop(asyncio.new_event_loop())
    return hub_event_listener(FakeHub(devices), FakeHass())


def test_resync_fetches_devices_once_and_replays_each_through_on_message():
    devices = [
        {"id": "light-a_1", "deviceType": "light", "attributes": {"isOn": True}},
        {"id": "plug-b_1", "deviceType": "outlet", "attributes": {"isOn": False}},
        {"id": "door-c_1", "deviceType": "openCloseSensor", "attributes": {"isOpen": True}},
    ]
    listener = _make(devices)

    seen = []
    # spy on_message; also capture whether discovery is suppressed *during* the call
    listener.on_message = lambda ws, msg: seen.append((json.loads(msg), listener._resyncing))

    asyncio.run(listener._resync_all_states())

    # exactly one /devices fetch (no per-device hammering)
    assert listener._hub.get_calls == ["/devices"]
    # every device replayed as a deviceStateChanged through on_message
    assert len(seen) == 3
    assert [m["data"]["id"] for m, _ in seen] == ["light-a_1", "plug-b_1", "door-c_1"]
    assert all(m["type"] == "deviceStateChanged" for m, _ in seen)
    # discovery suppressed for the whole replay, then reset
    assert all(suppressed is True for _, suppressed in seen)
    assert listener._resyncing is False


def test_on_open_triggers_resync_only_on_reconnect_not_first_open():
    listener = _make()
    listener._start_keepalive = lambda: None  # avoid real threading.Timer

    scheduled = []
    listener._loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda cb, *a: scheduled.append(cb)
    )

    listener._on_open(None)  # first open = initial setup, no resync
    assert scheduled == [], "first open must not schedule a resync"

    listener._on_open(None)  # reconnect = catch up missed state
    assert len(scheduled) == 1, "reconnect must schedule exactly one resync"
