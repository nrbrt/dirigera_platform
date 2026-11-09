"""
#71: a light is turned on with one PATCH, and a hub gets one command at a time.

guaifi measured that three separate PATCHes per light, sent in parallel for
every light of an area, often left bulbs on the wrong level or colour
temperature. One PATCH per light holding a list with one attribute per item,
sent through a per-hub queue, left none wrong. Tekno74 found that isOn and
lightLevel in ONE item come back 202 with lightLevel dropped, so the items
must stay separate.

As in the other light tests, the code is lifted out of light.py with ast, so
it runs without importing homeassistant. command_queue.py has no Home
Assistant imports and is loaded directly.
"""
import ast
import asyncio
import importlib.util
import logging
import os
import time
from types import SimpleNamespace

import pytest

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
LIGHT = os.path.join(COMPONENT, "light.py")

_spec = importlib.util.spec_from_file_location("command_queue", os.path.join(COMPONENT, "command_queue.py"))
command_queue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(command_queue)


class ColorMode:
    COLOR_TEMP = "color_temp"
    HS = "hs"


def _load():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in ("brightness_to_hub_level", "light_turn_on_patch")]
    bulb = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ikea_bulb")
    methods = [n for n in bulb.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name in ("async_turn_on", "async_turn_off")]
    ns = {
        "ATTR_BRIGHTNESS": "brightness",
        "ATTR_COLOR_TEMP_KELVIN": "color_temp_kelvin",
        "ATTR_HS_COLOR": "hs_color",
        "ColorMode": ColorMode,
        "HomeAssistantError": Exception,
        "DOMAIN": "dirigera_platform",
        "logger": logging.getLogger(__name__),
        "queue_for": command_queue.queue_for,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=funcs + methods, type_ignores=[])), LIGHT, "exec"), ns)
    return ns


NS = _load()
patch_for = NS["light_turn_on_patch"]

DUAL = ["isOn", "lightLevel", "colorTemperature", "colorHue", "colorSaturation"]
DIMMABLE = ["isOn", "lightLevel"]
RANGE = (2202, 4000)


def _items(data):
    return [item["attributes"] for item in data]


# --- the PATCH body ---------------------------------------------------------

def test_on_only():
    data, updates = patch_for(DUAL, {}, RANGE)
    assert _items(data) == [{"isOn": True}]
    assert updates == {"is_on": True}


def test_each_attribute_is_its_own_item():
    data, _ = patch_for(DUAL, {"brightness": 115, "color_temp_kelvin": 2700}, RANGE)
    assert _items(data) == [{"isOn": True}, {"colorTemperature": 2700}, {"lightLevel": 45}]
    assert all(len(item) == 1 for item in _items(data))


def test_light_level_comes_after_the_colour():
    # A colorTemperature after lightLevel left TRADFRI bulbs part-way to the
    # level (6 of 6 runs on a live hub); with the colour first, 6 of 6 right.
    for kwargs in ({"brightness": 200, "color_temp_kelvin": 3500},
                   {"brightness": 200, "hs_color": (30.0, 50.0)}):
        keys = [next(iter(item)) for item in _items(patch_for(DUAL, kwargs, RANGE)[0])]
        assert keys[0] == "isOn" and keys[-1] == "lightLevel", kwargs


def test_hue_and_saturation_stay_together():
    data, updates = patch_for(DUAL, {"hs_color": (30.0, 50.0)}, RANGE)
    assert _items(data) == [{"isOn": True}, {"colorHue": 30.0, "colorSaturation": 0.5}]
    assert updates["color_mode"] == "color"


def test_colour_temperature_is_kept_inside_the_range():
    data, updates = patch_for(DUAL, {"color_temp_kelvin": 6500}, RANGE)
    assert _items(data)[1] == {"colorTemperature": 4000}
    assert updates["color_temperature"] == 4000 and updates["color_mode"] == "temperature"


def test_unknown_range_sends_the_requested_value():
    data, _ = patch_for(DUAL, {"color_temp_kelvin": 2700}, (None, None))
    assert _items(data)[-1] == {"colorTemperature": 2700}


def test_what_the_bulb_cannot_receive_is_left_out():
    data, updates = patch_for(DIMMABLE, {"brightness": 255, "color_temp_kelvin": 2700, "hs_color": (1, 1)}, RANGE)
    assert _items(data) == [{"isOn": True}, {"lightLevel": 100}]
    assert "color_mode" not in updates


# --- ikea_bulb.async_turn_on / async_turn_off --------------------------------

class _Hub:
    websocket_base_url = None

    def __init__(self):
        self.sent = []

    def patch(self, route, data):
        self.sent.append((route, data))


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _bulb(hub):
    attributes = SimpleNamespace(is_on=False, light_level=10, color_temperature=4000,
                                 color_hue=None, color_saturation=None, color_mode="temperature")
    bulb = SimpleNamespace(
        _hub=hub, hass=_Hass(), _ignore_update=False, _color_mode=ColorMode.COLOR_TEMP,
        _json_data=SimpleNamespace(id="bulb-1", attributes=attributes,
                                   capabilities=SimpleNamespace(can_receive=DUAL)),
        min_color_temp_kelvin=RANGE[0], max_color_temp_kelvin=RANGE[1],
        async_schedule_update_ha_state=lambda force: None,
    )
    bulb.reset_ignore_update = lambda: setattr(bulb, "_ignore_update", False)
    return bulb


def test_turn_on_sends_one_patch_and_updates_the_model(monkeypatch):
    monkeypatch.setattr(command_queue, "_queues", {})
    hub = _Hub()
    hub.websocket_base_url = "wss://test-1"
    bulb = _bulb(hub)
    asyncio.run(NS["async_turn_on"](bulb, brightness=115, hs_color=(120.0, 80.0)))
    assert hub.sent == [("/devices/bulb-1", [
        {"attributes": {"isOn": True}},
        {"attributes": {"colorHue": 120.0, "colorSaturation": 0.8}},
        {"attributes": {"lightLevel": 45}},
    ])]
    a = bulb._json_data.attributes
    assert (a.is_on, a.light_level, a.color_hue, a.color_saturation, a.color_mode) == (True, 45, 120.0, 0.8, "color")
    assert bulb._color_mode == ColorMode.HS
    assert bulb._ignore_update is True


def test_plain_turn_on_does_not_ignore_the_echo(monkeypatch):
    monkeypatch.setattr(command_queue, "_queues", {})
    hub = _Hub()
    hub.websocket_base_url = "wss://test-2"
    bulb = _bulb(hub)
    asyncio.run(NS["async_turn_on"](bulb))
    assert hub.sent == [("/devices/bulb-1", [{"attributes": {"isOn": True}}])]
    assert bulb._ignore_update is False


def test_turn_off(monkeypatch):
    monkeypatch.setattr(command_queue, "_queues", {})
    hub = _Hub()
    hub.websocket_base_url = "wss://test-3"
    bulb = _bulb(hub)
    bulb._json_data.attributes.is_on = True
    asyncio.run(NS["async_turn_off"](bulb))
    assert hub.sent == [("/devices/bulb-1", [{"attributes": {"isOn": False}}])]
    assert bulb._json_data.attributes.is_on is False


# --- the queue ----------------------------------------------------------------

class _ThreadHass:
    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def test_queue_runs_one_command_at_a_time_and_spaces_them():
    spacing = 0.05
    log = []

    def command(name):
        log.append(("start", name, time.monotonic()))
        time.sleep(0.01)
        log.append(("end", name, time.monotonic()))
        return name

    async def main():
        queue = command_queue.HubCommandQueue(min_spacing=spacing)
        return await asyncio.gather(*(queue.run(_ThreadHass(), command, n) for n in range(5)))

    assert asyncio.run(main()) == list(range(5))
    kinds = [k for k, _, _ in log]
    assert kinds == ["start", "end"] * 5, "commands overlapped"
    starts = [t for k, _, t in log if k == "start"]
    assert all(b - a >= spacing * 0.95 for a, b in zip(starts, starts[1:]))


def test_queue_survives_a_failing_command():
    def boom():
        raise RuntimeError("hub said no")

    async def main():
        queue = command_queue.HubCommandQueue(min_spacing=0)
        with pytest.raises(RuntimeError):
            await queue.run(_Hass(), boom)
        return await queue.run(_Hass(), lambda: "next")

    assert asyncio.run(main()) == "next"


def test_one_queue_per_hub(monkeypatch):
    monkeypatch.setattr(command_queue, "_queues", {})
    a = SimpleNamespace(websocket_base_url="wss://a")
    b = SimpleNamespace(websocket_base_url="wss://b")
    assert command_queue.queue_for(a) is command_queue.queue_for(SimpleNamespace(websocket_base_url="wss://a"))
    assert command_queue.queue_for(a) is not command_queue.queue_for(b)
