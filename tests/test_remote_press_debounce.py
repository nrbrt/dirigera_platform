"""
Issue #65: remotePressEvent raised TypeError on buttons that also have a scene.

controller_trigger_last_time_map is shared by two paths. parse_scene_update
stores the hub's lastTriggered, parsed tz-aware. parse_remote_press_event
stored a naive datetime.now(). When one physical press produced both events
for the same key, the second one subtracted naive from aware. A press after a
scene raised and was dropped; a scene after a press caught the error, skipped
the debounce and fired a second event for the same press.

The listener is loaded standalone with the stubs from test_reconnect_resync.
"""
import datetime
import json
import time
from types import SimpleNamespace

import pytest

import test_reconnect_resync as t

hel = t.hel
L = hel.hub_event_listener


@pytest.fixture
def listener():
    fired = []
    bus = SimpleNamespace(fire=lambda event_type, event_data: fired.append(event_data["type"]))
    entity = SimpleNamespace(registry_entry=SimpleNamespace(device_id="dev1", entity_id="sensor.bilresa"))
    saved = L.device_registry
    L.device_registry = {"hk": {"abc_1": hel.registry_entry(entity)}}
    hel.controller_trigger_last_time_map.clear()
    me = object.__new__(L)
    me._hub_key = "hk"
    me._hass = SimpleNamespace(bus=bus)
    me._apply_scene_actions = lambda msg: None
    yield me, fired
    L.device_registry = saved
    hel.controller_trigger_last_time_map.clear()


def _scene():
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "sceneUpdated", "data": {"lastTriggered": ts, "triggers": [
        {"type": "controller", "trigger": {"controllerType": "shortcutController",
                                           "clickPattern": "singlePress", "deviceId": "abc_2"}}]}})


def _press():
    return json.dumps({"type": "remotePressEvent", "data": {"id": "abc_2", "clickPattern": "singlePress"}})


def _run(me, seq):
    for kind in seq:
        if kind == "wait":
            time.sleep(1.1)
        else:
            L.on_message(me, None, _scene() if kind == "scene" else _press())


@pytest.mark.parametrize("seq", [["scene", "press"], ["press", "scene"]])
def test_one_press_with_scene_fires_once_without_error(listener, seq, caplog):
    me, fired = listener
    _run(me, seq)
    assert fired == ["button2_single_click"]
    assert "offset-naive" not in caplog.text


def test_press_without_scene_still_fires_every_press(listener, caplog):
    me, fired = listener
    _run(me, ["press", "wait", "press"])
    assert fired == ["button2_single_click", "button2_single_click"]
    assert "offset-naive" not in caplog.text


def test_map_only_holds_aware_datetimes(listener):
    me, _ = listener
    _run(me, ["press"])
    assert hel.controller_trigger_last_time_map
    assert all(v.tzinfo is not None for v in hel.controller_trigger_last_time_map.values())
