"""
A removal reconciliation runs once per burst of deviceRemoved events.

The Dirigera hub can deliver the same deviceRemoved event more than once
(seen on a user's hub: two identical "Device removed event received" lines
~20 ms apart, each firing its own stale-device check, so one removal gave two
/devices fetches and two warnings). The listener now coalesces a burst into a
single reconciliation with a short trailing-edge debounce.

hub_event_listener.py is loaded standalone, as in test_hub_attributes.py.
"""
import asyncio
import importlib.util
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
# Load the module as a submodule of a fake package, so its
# `from .stale_devices import async_remove_stale_devices` resolves to a fake
# and the real _run_coalesced_stale_check runs (clearing the pending flag).
# Calling that coroutine function is what a reconciliation does;
# async_create_task below counts those.
_PKG = "dpkg_coalesce"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = []
sys.modules[_PKG] = _pkg
_sd = types.ModuleType(f"{_PKG}.stale_devices")


async def _fake_async_remove_stale_devices(hass, entry_id, hub, dry_run=True):
    return []


_sd.async_remove_stale_devices = _fake_async_remove_stale_devices
sys.modules[f"{_PKG}.stale_devices"] = _sd

_spec = importlib.util.spec_from_file_location(f"{_PKG}.hub_event_listener_coalesce_uut", _PATH)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener

HUB_KEY = "wss://hub.test:8443/v1"
REMOVED = '{"type":"deviceRemoved","data":{"id":"d688ed69_1"}}'


def _listener(debounce=0.05):
    asyncio.set_event_loop(asyncio.new_event_loop())
    calls = {"n": 0}

    def _async_create_task(coro):
        calls["n"] += 1
        coro.close()  # don't actually run it; we only count reconciliations

    hass = SimpleNamespace(async_create_task=_async_create_task)
    listener = hub_event_listener(SimpleNamespace(websocket_base_url=HUB_KEY), hass, None)
    listener._config_entry_id = "e1"
    listener.STALE_RECHECK_DEBOUNCE = debounce
    return listener, calls


def _drain(loop, seconds):
    loop.run_until_complete(asyncio.sleep(seconds))


def test_burst_of_device_removed_events_runs_one_recheck():
    listener, calls = _listener(debounce=0.05)
    for _ in range(3):  # the hub double- or triple-delivers the same removal
        listener.on_message(None, REMOVED)
    _drain(listener._loop, 0.2)
    assert calls["n"] == 1


def test_single_device_removed_still_runs_one_recheck():
    listener, calls = _listener(debounce=0.05)
    listener.on_message(None, REMOVED)
    _drain(listener._loop, 0.2)
    assert calls["n"] == 1


def test_a_later_removal_runs_its_own_recheck():
    # Coalescing is time-boxed, not permanent: a genuine removal after the
    # window still reconciles.
    listener, calls = _listener(debounce=0.05)
    listener.on_message(None, REMOVED)
    _drain(listener._loop, 0.2)
    listener.on_message(None, REMOVED)
    _drain(listener._loop, 0.2)
    assert calls["n"] == 2


def test_stop_requested_skips_the_recheck():
    listener, calls = _listener(debounce=0.05)
    listener._request_to_stop = True
    listener.on_message(None, REMOVED)
    _drain(listener._loop, 0.2)
    assert calls["n"] == 0
