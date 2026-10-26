"""
A device set's state is its first bulb's. ikea_bulb_device_set.async_turn_on
used to set that bulb's _ignore_update without writing the new values
through, so the hub's echo was applied to the model but never pushed: the
set and the bulb kept showing their old brightness and white until some
later event. The set must leave the echo alone.

As in test_light_color_mode.py, the method is lifted out of the source with
ast, so it runs without importing homeassistant.
"""
import ast
import asyncio
import logging
import os
from types import SimpleNamespace

LIGHT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "light.py",
)


def _turn_on():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ikea_bulb_device_set")
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_turn_on")
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "brightness_to_hub_level")
    ns = {
        "ATTR_BRIGHTNESS": "brightness",
        "ATTR_COLOR_TEMP_KELVIN": "color_temp_kelvin",
        "ATTR_HS_COLOR": "hs_color",
        "ColorMode": SimpleNamespace(COLOR_TEMP="color_temp", HS="hs"),
        "HomeAssistantError": Exception,
        "DOMAIN": "dirigera_platform",
        "logger": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[helper, fn], type_ignores=[])), LIGHT, "exec"), ns)
    return ns["async_turn_on"]


class _Controller:
    supported_color_modes = ["color_temp", "hs"]

    def __init__(self):
        self._ignore_update = False

    def reset_ignore_update(self):
        self._ignore_update = False


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_turn_on_leaves_the_first_bulbs_echo_alone():
    sent = []
    entity = SimpleNamespace(_controller=_Controller(), hass=_Hass(), patch_command=sent.append)
    for kwargs in ({"brightness": 178}, {"color_temp_kelvin": 2700}, {"hs_color": (30, 50)}):
        asyncio.run(_turn_on()(entity, **kwargs))
        assert entity._controller._ignore_update is False, kwargs
    assert {"lightLevel": 70} in sent and {"colorTemperature": 2700} in sent
