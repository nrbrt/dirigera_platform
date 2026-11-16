"""
A light's command and the hub events that follow it.

ikea_bulb used to raise a flag after a PATCH with more than one item
("the hub may echo an old level first"); the listener then skipped the state
write of the next event that did not carry isOn. Three things were wrong:

* The flag was not tied to its command and never expired. After a command
  whose echo carried only isOn, a change made an hour later with a remote was
  written to the model but never reached Home Assistant.
* It was raised after the PATCH returned, so an echo that arrived before that
  was not covered.
* Only the state write was skipped: the stale value itself went into the
  model, over the value that had just been sent.

Now the bulb keeps what it sent (pending_command.py), from before the PATCH
until the hub confirms it or ECHO_WINDOW_S have passed, and an event's value
that differs from it is not applied. Every event gets its state write.

The real ikea_bulb class is lifted out of light.py with ast (LightEntity
stubbed) and wired to the real listener, loaded with the stubs from
test_reconnect_resync.
"""
import ast
import asyncio
import enum
import importlib.util
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional

import pytest
from pydantic import BaseModel

import test_reconnect_resync as t

hel = t.hel
L = hel.hub_event_listener
HUB_KEY = "wss://light-echo.test"
COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
LIGHT = os.path.join(COMPONENT, "light.py")


def _module(name):
    path = os.path.join(COMPONENT, name + ".py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(name + "_echo_uut", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub_writer = _module("hub_writer")
pc = _module("pending_command")


class ColorMode(enum.Enum):
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    HS = "hs"


class LightEntity:
    hass = None
    writes = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1


def _load_bulb():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             or (isinstance(n, ast.ClassDef) and n.name == "ikea_bulb")]
    ns = {
        "ATTR_BRIGHTNESS": "brightness",
        "ATTR_COLOR_TEMP_KELVIN": "color_temp_kelvin",
        "ATTR_HS_COLOR": "hs_color",
        "ColorMode": ColorMode,
        "LightEntity": LightEntity,
        "Light": object,
        "DeviceInfo": dict,
        "HomeAssistantError": Exception,
        "DOMAIN": "dirigera_platform",
        "logger": logging.getLogger(__name__),
        "writer_for": hub_writer.writer_for,
        "hub_event_listener": L,
        "registry_entry": hel.registry_entry,
        "to_snake_case": hel.to_snake_case,
        "PendingCommand": getattr(pc, "PendingCommand", None),
        "asyncio": asyncio,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), LIGHT, "exec"), ns)
    return ns["ikea_bulb"]


ikea_bulb = _load_bulb()

DIMMABLE = ["isOn", "lightLevel"]
DUAL = ["isOn", "lightLevel", "colorTemperature", "colorHue", "colorSaturation"]


class Attrs(BaseModel, validate_assignment=True):
    custom_name: str = "bulb"
    is_on: bool = True
    light_level: Optional[int] = 30
    color_temperature: Optional[int] = None
    color_temperature_min: Optional[int] = None
    color_temperature_max: Optional[int] = None
    color_hue: Optional[float] = None
    color_saturation: Optional[float] = None
    color_mode: Optional[str] = None
    manufacturer: str = "IKEA"
    model: str = "bulb"
    firmware_version: str = "1"


class _Hub:
    websocket_base_url = HUB_KEY

    def __init__(self):
        self.sent = []
        self.gate = None
        self.fail = False

    def patch(self, route, data):
        if self.gate is not None:
            self.gate.wait(5)  # the request is on its way
        if self.fail:
            raise OSError("hub unreachable")
        self.sent.append((route, data))


class _Hass:
    def __init__(self):
        self._pool = ThreadPoolExecutor(2)

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


@pytest.fixture
def setup(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setattr(hub_writer, "_writers", {})
    # The record has its own clock, so no test has to wait for the window.
    now = [1000.0]
    monkeypatch.setattr(pc, "_now", lambda: now[0])
    hub, hass = _Hub(), _Hass()
    hass.loop = loop  # the end of a command's window is a timer on it
    listener = L(hub, hass)
    writer = hub_writer.HubWriter(hass, hub, min_spacing=0)
    writer.start(loop.create_task)

    def bulb(can_receive=DIMMABLE, **attributes):
        entity = ikea_bulb(hub, SimpleNamespace(
            id="b1", relation_id=None, room=None, is_reachable=True, device_set=[],
            attributes=Attrs(**attributes), capabilities=SimpleNamespace(can_receive=can_receive),
        ))
        entity.hass = hass
        return entity

    def says(**attributes):
        """The hub reports a state change of the bulb."""
        listener.on_message(None, json.dumps({
            "type": "deviceStateChanged",
            "data": {"id": "b1", "deviceType": "light", "attributes": attributes},
        }))

    def hub_says(**attributes):
        says(**attributes)
        loop.run_until_complete(asyncio.sleep(0))

    def later(seconds):
        now[0] += seconds

    def run_with_patch_on_its_way(command, during):
        """Run `command` with the PATCH held up in the hub; `during` runs
        while it is, the PATCH returns after it."""
        hub.gate = threading.Event()

        async def scenario():
            task = asyncio.ensure_future(command)
            await asyncio.sleep(0.05)
            for attributes in during:
                says(**attributes)
                await asyncio.sleep(0)
            hub.gate.set()
            try:
                await task
            finally:
                hub.gate = None
            await asyncio.sleep(0)

        loop.run_until_complete(scenario())

    yield SimpleNamespace(loop=loop, hub=hub, bulb=bulb, hub_says=hub_says, later=later, says=says,
                          run=loop.run_until_complete, in_flight=run_with_patch_on_its_way)
    L.unregister_hub(HUB_KEY)
    writer.stop()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def _level(bulb):
    return bulb._json_data.attributes.light_level


# --- the defects of the flag ---------------------------------------------------

def test_external_change_long_after_a_command_reaches_home_assistant(setup):
    # Scenario 2 of the review. The command asks the level the bulb already
    # has, so isOn is the only change the hub has to report.
    bulb = setup.bulb(is_on=False, light_level=50)
    setup.run(bulb.async_turn_on(brightness=128))
    setup.hub_says(isOn=True)
    setup.later(3600)
    before = bulb.writes
    setup.hub_says(lightLevel=10)  # dimmed with a remote
    assert _level(bulb) == 10
    assert bulb.writes == before + 1, "the change never reached Home Assistant"


def test_stale_echo_does_not_overwrite_the_value_that_was_sent(setup):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    assert _level(bulb) == 80
    setup.hub_says(lightLevel=30)  # the hub echoes the old level first
    assert _level(bulb) == 80
    setup.hub_says(lightLevel=80)
    assert _level(bulb) == 80


def test_matching_echo_is_written_and_ends_the_wait(setup):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    before = bulb.writes
    setup.hub_says(lightLevel=80)
    assert bulb.writes == before + 1, "an applied event gets its state write"
    setup.hub_says(lightLevel=10)  # right after it: a remote, not an echo
    assert _level(bulb) == 10
    assert bulb.writes == before + 2


def test_after_the_window_a_differing_value_is_applied_and_written(setup):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    setup.later(pc.ECHO_WINDOW_S + 0.1)
    before = bulb.writes
    setup.hub_says(lightLevel=30)
    assert _level(bulb) == 30
    assert bulb.writes == before + 1


def test_failed_patch_leaves_no_record(setup):
    bulb = setup.bulb(light_level=30)
    setup.hub.fail = True
    with pytest.raises(Exception):
        setup.run(bulb.async_turn_on(brightness=204))
    assert _level(bulb) == 30, "nothing was accepted, nothing is assumed"
    before = bulb.writes
    setup.hub_says(lightLevel=55)
    assert _level(bulb) == 55
    assert bulb.writes == before + 1


def test_cancelled_command_leaves_no_record(setup):
    bulb = setup.bulb(light_level=30)
    setup.hub.gate = threading.Event()

    async def scenario():
        task = asyncio.ensure_future(bulb.async_turn_on(brightness=204))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        setup.hub.gate.set()

    setup.run(scenario())
    setup.hub_says(lightLevel=55)
    assert _level(bulb) == 55


def test_stale_echo_before_the_patch_returns_is_not_applied(setup):
    bulb = setup.bulb(light_level=30)
    setup.in_flight(bulb.async_turn_on(brightness=204), during=[{"lightLevel": 31}])
    assert _level(bulb) == 80


def test_echo_before_the_patch_returns_ends_the_wait(setup):
    bulb = setup.bulb(light_level=30)
    setup.in_flight(bulb.async_turn_on(brightness=204),
                    during=[{"lightLevel": 30}, {"isOn": True, "lightLevel": 80}])
    assert _level(bulb) == 80
    before = bulb.writes
    setup.hub_says(lightLevel=10)  # the echo is in, so this is a new change
    assert _level(bulb) == 10
    assert bulb.writes == before + 1


def test_change_after_an_early_echo_is_not_undone_when_the_patch_returns(setup):
    bulb = setup.bulb(light_level=30)
    setup.in_flight(bulb.async_turn_on(brightness=204),
                    during=[{"isOn": True, "lightLevel": 80}, {"lightLevel": 10}])
    assert _level(bulb) == 10, "the confirmed value was written again over a newer one"


# --- the hub's last value wins when the command is not confirmed ----------------

@pytest.fixture
def short_window(monkeypatch):
    # The window is a timer on the loop; these tests sit it out for real.
    monkeypatch.setattr(pc, "ECHO_WINDOW_S", 0.05)
    monkeypatch.setattr(pc, "_now", pc.time.monotonic)


def test_dimmed_with_a_remote_right_after_a_command_and_no_later_event(setup, short_window):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    setup.hub_says(isOn=True, lightLevel=80)  # the echo
    setup.run(bulb.async_turn_on(brightness=128))
    setup.hub_says(lightLevel=20)  # the remote, inside the window: no echo of 50 follows
    assert _level(bulb) == 50, "inside the window it may still be an old value"
    before = bulb.writes
    setup.run(asyncio.sleep(0.1))
    assert _level(bulb) == 20, "Home Assistant kept the value it sent, the light is at 20"
    assert bulb.writes == before + 1, "the late value never reached Home Assistant"
    setup.hub_says(lightLevel=10)  # nothing is pending any more
    assert _level(bulb) == 10


def test_stale_echo_followed_by_the_confirmation_is_never_applied(setup, short_window):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    setup.hub_says(lightLevel=30)  # the old level first
    setup.hub_says(lightLevel=80)  # the confirmation
    before = bulb.writes
    setup.run(asyncio.sleep(0.1))
    assert _level(bulb) == 80
    assert bulb.writes == before, "the window of a confirmed command wrote state"


def test_value_reported_during_a_failed_patch_is_the_state(setup):
    bulb = setup.bulb(light_level=30)
    setup.hub.fail = True
    with pytest.raises(Exception):
        setup.in_flight(bulb.async_turn_on(brightness=204), during=[{"lightLevel": 55}])
    assert _level(bulb) == 55, "the command failed, the hub's value was dropped"


def test_value_reported_while_the_command_waits_its_turn_is_applied(setup):
    # The record starts when the PATCH goes out. Until then the hub's value
    # is the state of the light, and a slow queue does not keep it back.
    bulb = setup.bulb(light_level=30)
    other = SimpleNamespace(gate=threading.Event())

    async def scenario():
        writer = hub_writer.writer_for(setup.hub)
        ahead = asyncio.ensure_future(writer.run(other.gate.wait, 5))
        await asyncio.sleep(0.02)
        command = asyncio.ensure_future(bulb.async_turn_on(brightness=204))
        await asyncio.sleep(0.02)
        setup.says(lightLevel=20)
        await asyncio.sleep(0)
        level_while_waiting = _level(bulb)
        other.gate.set()
        await asyncio.gather(ahead, command)
        return level_while_waiting

    assert setup.run(scenario()) == 20
    assert _level(bulb) == 80


def test_removed_bulb_has_no_window_left(setup, short_window):
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=204))
    setup.hub_says(lightLevel=20)
    setup.run(bulb.async_will_remove_from_hass())
    bulb.hass = None
    setup.run(asyncio.sleep(0.1))
    assert _level(bulb) == 80


# --- what is sent is what must come back ---------------------------------------

def test_turn_off_holds_back_a_stale_is_on_only(setup):
    bulb = setup.bulb(is_on=True, light_level=30)
    setup.run(bulb.async_turn_off())
    setup.hub_says(isOn=True, lightLevel=45)
    assert bulb.is_on is False
    assert _level(bulb) == 45, "lightLevel was not part of the command"
    setup.hub_says(isOn=False)
    setup.hub_says(isOn=True)  # switched on at the wall, inside the window
    assert bulb.is_on is True


def test_plain_turn_on_holds_back_a_stale_is_on(setup):
    bulb = setup.bulb(is_on=False)
    setup.run(bulb.async_turn_on())
    setup.hub_says(isOn=False)
    assert bulb.is_on is True


def test_rounded_level_is_what_the_echo_must_match(setup):
    # #67: 158 is sent as 62 %, and 62 % is what the hub echoes.
    bulb = setup.bulb(light_level=30)
    setup.run(bulb.async_turn_on(brightness=158))
    setup.hub_says(lightLevel=62)
    assert bulb.brightness == 158
    setup.hub_says(lightLevel=61)  # confirmed, so this is a change
    assert _level(bulb) == 61


def test_clamped_colour_temperature_is_what_the_echo_must_match(setup):
    bulb = setup.bulb(DUAL, color_temperature=2700, color_temperature_min=4000,
                      color_temperature_max=2202, color_mode="temperature")
    setup.run(bulb.async_turn_on(color_temp_kelvin=6500))
    assert bulb.color_temp_kelvin == 4000
    setup.hub_says(colorTemperature=2700)  # stale
    assert bulb.color_temp_kelvin == 4000
    setup.hub_says(colorTemperature=4000)  # the echo of what was sent
    setup.hub_says(colorTemperature=3000)  # confirmed, so this is a change
    assert bulb.color_temp_kelvin == 3000


def test_hubs_own_colour_temperature_is_the_echo_and_the_model_value(setup):
    # The hub stores mireds: 2700 K is reported as 2702 (live hub, 10-11-2026).
    bulb = setup.bulb(DUAL, color_temperature=4000, color_temperature_min=4000,
                      color_temperature_max=2202, color_mode="temperature")
    setup.run(bulb.async_turn_on(color_temp_kelvin=2700))
    assert setup.hub.sent[-1][1][1] == {"attributes": {"colorTemperature": 2700}}
    assert bulb.color_temp_kelvin == 2702, "the model holds a value the hub never has"
    setup.hub_says(colorTemperature=2702)  # the echo
    setup.hub_says(colorTemperature=3003)  # confirmed, so this is a change
    assert bulb.color_temp_kelvin == 3003


def test_hubs_own_hue_and_saturation_are_the_echo(setup):
    bulb = setup.bulb(DUAL, color_hue=10.0, color_saturation=0.2, color_mode="color")
    setup.run(bulb.async_turn_on(hs_color=(30.0, 50.0)))
    a = bulb._json_data.attributes
    assert (a.color_hue, a.color_saturation) == (29.9981689453125, 0.5)
    setup.hub_says(colorHue=29.9981689453125, colorSaturation=0.5)  # the echo
    setup.hub_says(colorHue=200)  # confirmed, so this is a change
    assert a.color_hue == 200.0


def test_brightness_and_colour_together_are_each_held_on_their_own(setup):
    bulb = setup.bulb(DUAL, light_level=30, color_temperature=2700, color_hue=10.0,
                      color_saturation=0.2, color_mode="temperature")
    setup.run(bulb.async_turn_on(brightness=204, hs_color=(120.0, 80.0)))
    assert bulb.color_mode == ColorMode.HS
    # The colour is confirmed, in the hub's own units; the level and the mode
    # are still the old ones.
    hue, saturation = 21845 * 360 / 65536, 203 / 254
    setup.hub_says(colorHue=hue, colorSaturation=saturation, lightLevel=30, colorMode="temperature")
    a = bulb._json_data.attributes
    assert (a.color_hue, a.color_saturation, a.light_level) == (hue, saturation, 80)
    assert bulb.color_mode == ColorMode.HS
    setup.hub_says(colorHue=200)  # confirmed, so this is a change
    assert a.color_hue == 200.0
    setup.hub_says(lightLevel=30)
    assert a.light_level == 80, "the level is not confirmed yet"


def test_newer_command_for_the_same_light_is_the_one_that_counts(setup):
    bulb = setup.bulb(light_level=30)

    async def scenario():
        first = asyncio.ensure_future(bulb.async_turn_on(brightness=128))
        await asyncio.sleep(0)
        await asyncio.gather(first, bulb.async_turn_on(brightness=204))

    setup.run(scenario())
    assert _level(bulb) == 80
    setup.hub_says(lightLevel=50)  # the echo of the first command
    assert _level(bulb) == 80
    setup.hub_says(lightLevel=80)
    assert _level(bulb) == 80


# --- colour mode follows the model ---------------------------------------------

def test_colour_mode_follows_the_hubs_event(setup):
    bulb = setup.bulb(DUAL, color_mode="color")
    assert bulb.color_mode == ColorMode.HS
    setup.hub_says(colorTemperature=2700, colorMode="temperature")
    assert bulb.color_mode == ColorMode.COLOR_TEMP


def test_hubs_colour_mode_wins_over_stale_colour_values_in_the_same_event(setup):
    # The echo of a colour temperature PATCH can still carry hue and saturation.
    bulb = setup.bulb(DUAL, color_mode="color")
    setup.hub_says(colorTemperature=2700, colorHue=30, colorSaturation=0.5, colorMode="temperature")
    assert bulb.color_mode == ColorMode.COLOR_TEMP


def test_event_without_colour_mode_implies_it_from_the_colour_it_carries(setup):
    bulb = setup.bulb(DUAL, color_mode="color")
    setup.hub_says(colorTemperature=2700)
    assert bulb.color_mode == ColorMode.COLOR_TEMP
    setup.hub_says(colorHue=30, colorSaturation=0.5)
    assert bulb.color_mode == ColorMode.HS


def test_colour_keys_never_give_a_mode_the_bulb_does_not_have(setup):
    # A TRADFRI driver is brightness-only, the hub still sends colour keys.
    bulb = setup.bulb(DIMMABLE)
    setup.hub_says(colorHue=30, colorSaturation=0.5)
    assert bulb.color_mode == ColorMode.BRIGHTNESS


def test_command_sets_the_colour_mode_through_the_model(setup):
    bulb = setup.bulb(DUAL, color_mode="color")
    setup.run(bulb.async_turn_on(color_temp_kelvin=2700))
    assert bulb._json_data.attributes.color_mode == "temperature"
    assert bulb.color_mode == ColorMode.COLOR_TEMP


def test_scene_action_changes_the_colour_mode(setup):
    bulb = setup.bulb(DUAL, color_mode="color")
    L(setup.hub, bulb.hass)._apply_scene_actions({"data": {"actions": [
        {"type": "device", "deviceId": "b1", "attributes": {"colorTemperature": 2700}},
    ]}})
    assert bulb.color_mode == ColorMode.COLOR_TEMP


# --- review of the merge with #76 (16-11-2026) --------------------------------

class _FakeSet:
    hass = object()
    enabled = True
    unique_id = "set1"

    def __init__(self):
        self.pushed = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.pushed += 1


@pytest.mark.parametrize("in_ha", [True, False], ids=["light-in-ha", "sets-only"])
def test_the_set_follows_when_a_command_window_ends(setup, monkeypatch, in_ha):
    """A value the hub reported during a command's window stands when the
    window ends unconfirmed. The set that follows the light must show it too;
    under "Sets only" the set is the only entity that does. Found by the
    adversarial review: the set kept the commanded value."""
    monkeypatch.setattr(pc, "ECHO_WINDOW_S", 0.05)
    monkeypatch.setattr(pc, "_now", __import__("time").monotonic)
    bulb = setup.bulb(light_level=30)
    if not in_ha:
        bulb.hass = None
    device_set = _FakeSet()
    L.get_registry_entry(HUB_KEY, "b1").cascade_entity = device_set
    setup.run(bulb.async_turn_on(brightness=128))
    assert _level(bulb) == 50
    setup.hub_says(lightLevel=20)          # a remote, right after the command
    before = device_set.pushed
    setup.run(asyncio.sleep(0.2))          # the window ends, 20 stands
    assert _level(bulb) == 20
    assert device_set.pushed - before == 1
