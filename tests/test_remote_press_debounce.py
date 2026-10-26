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


# Second part of #65: the hub sends two sceneUpdated per press. The first one
# still carries the previous lastTriggered, the per-trigger triggeredAt is
# already current in both. Messages below follow the capture in the issue.
PRESSED = "abc_1"
OTHER = "xyz_1"


@pytest.fixture
def two_remotes(listener):
    me, fired = listener
    other = SimpleNamespace(registry_entry=SimpleNamespace(device_id="dev2", entity_id="sensor.other"))
    L.device_registry["hk"]["xyz_1"] = hel.registry_entry(other)
    bus = SimpleNamespace(fire=lambda event_type, event_data: fired.append(
        (event_data["device_id"], event_data["type"])))
    me._hass = SimpleNamespace(bus=bus)
    return me, fired


def _ts(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _scene_msg(msg_time, last_triggered, pressed_at, other_at):
    def trig(dev, at):
        return {"type": "controller", "triggeredAt": _ts(at),
                "trigger": {"controllerType": "shortcutController", "clickPattern": "singlePress",
                            "buttonIndex": 0, "deviceId": dev}}
    return json.dumps({"type": "sceneUpdated", "time": _ts(msg_time), "data": {
        "lastTriggered": _ts(last_triggered),
        "triggers": [{"type": "app"}, trig(PRESSED, pressed_at), trig(OTHER, other_at)]}})


def _press_once(me, t0, previous, include_press=True):
    ms = datetime.timedelta(milliseconds=1)
    other_at = t0 - datetime.timedelta(days=3)
    if include_press:
        L.on_message(me, None, json.dumps({"type": "remotePressEvent", "time": _ts(t0),
                                           "data": {"id": PRESSED, "clickPattern": "singlePress"}}))
    L.on_message(me, None, _scene_msg(t0 + 85 * ms, previous, t0 + 54 * ms, other_at))
    L.on_message(me, None, _scene_msg(t0 + 496 * ms, t0 + 79 * ms, t0 + 54 * ms, other_at))


def test_two_scene_updates_fire_once_for_the_pressed_remote_only(two_remotes):
    me, fired = two_remotes
    t0 = datetime.datetime.now(datetime.timezone.utc)
    _press_once(me, t0, t0 - datetime.timedelta(minutes=4), include_press=False)
    assert fired == [("dev1", "button1_single_click")]


def test_press_event_plus_two_scene_updates_fire_once(two_remotes):
    me, fired = two_remotes
    t0 = datetime.datetime.now(datetime.timezone.utc)
    _press_once(me, t0, t0 - datetime.timedelta(minutes=4))
    assert fired == [("dev1", "button1_single_click")]


def test_next_press_after_a_second_fires_again(two_remotes):
    me, fired = two_remotes
    t0 = datetime.datetime.now(datetime.timezone.utc)
    _press_once(me, t0, t0 - datetime.timedelta(minutes=4), include_press=False)
    t1 = t0 + datetime.timedelta(seconds=3)
    _press_once(me, t1, t0 + datetime.timedelta(milliseconds=79), include_press=False)
    assert fired == [("dev1", "button1_single_click"), ("dev1", "button1_single_click")]


def test_scene_actions_still_applied_on_a_debounced_update(two_remotes):
    me, fired = two_remotes
    applied = []
    me._apply_scene_actions = lambda msg: applied.append(msg)
    t0 = datetime.datetime.now(datetime.timezone.utc)
    _press_once(me, t0, t0 - datetime.timedelta(minutes=4), include_press=False)
    assert len(applied) == 2


def test_map_never_moves_back_in_time(two_remotes):
    me, fired = two_remotes
    t0 = datetime.datetime.now(datetime.timezone.utc)
    _press_once(me, t0, t0 - datetime.timedelta(minutes=4))
    stored = hel.controller_trigger_last_time_map["dev1_button1_single_click"]
    assert stored >= t0


@pytest.mark.parametrize("triggered_at", ["absent", None])
def test_scene_without_any_trigger_time_still_fires(two_remotes, triggered_at):
    me, fired = two_remotes
    trig = {"type": "controller", "trigger": {"controllerType": "shortcutController",
                                              "clickPattern": "singlePress", "deviceId": PRESSED}}
    if triggered_at != "absent":
        trig["triggeredAt"] = triggered_at
    L.on_message(me, None, json.dumps({"type": "sceneUpdated", "data": {"triggers": [trig]}}))
    assert fired == [("dev1", "button1_single_click")]
