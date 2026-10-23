"""
Issue #50: a dual-mode bulb must report the hub's colour mode after a restart.

The hub sends "colorMode": "temperature" | "color" on every light. Before this
fix the model dropped the key, and set_state() picked hs for every
bulb that supports hs. A bulb the hub held in colour-temperature mode then
reported color_mode hs and color_temp_kelvin None until its next colour event.

Two things are guarded here.

1. The model. dirigera >= 1.6.0 keeps colorMode as attributes.color_mode on
   its own Light, which replaced this integration's LightX subclass. Startup,
   async_update() and runtime discovery all build that Light.

2. set_state(). It must follow color_mode when present, and keep the old
   hs-first rule when it is None: not every bulb reports colorMode, so the
   fallback is load-bearing, not dead code.

As in test_matter_filtering.py, the code under test is lifted out of the
source with ast, so the real bodies run without importing homeassistant.
The model tests need the dirigera library and skip without it.
"""
import ast
import enum
import logging
import os
import sys
from types import SimpleNamespace

import pytest

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
LIGHT = os.path.join(COMPONENT, "light.py")


def _parse(path):
    with open(path) as fh:
        return ast.parse(fh.read())


def _class(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _method(tree, class_name, name):
    nodes = [n for n in _class(tree, class_name).body
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert nodes, f"{class_name}.{name} not found"
    return nodes[0]


def _exec(nodes, path, namespace):
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace


# --- set_state() ------------------------------------------------------------

class ColorMode(enum.Enum):
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    HS = "hs"


SET_STATE = _exec(
    [_method(_parse(LIGHT), "ikea_bulb", "set_state")],
    LIGHT,
    {"ColorMode": ColorMode, "logger": logging.getLogger(__name__)},
)["set_state"]

DUAL_MODE = ["isOn", "lightLevel", "colorTemperature", "colorHue", "colorSaturation"]
CT_ONLY = ["isOn", "lightLevel", "colorTemperature"]
HS_ONLY = ["isOn", "lightLevel", "colorHue", "colorSaturation"]


def _bulb(can_receive, **attributes):
    bulb = SimpleNamespace(_json_data=SimpleNamespace(
        capabilities=SimpleNamespace(can_receive=can_receive),
        attributes=SimpleNamespace(**attributes),
    ))
    SET_STATE(bulb)
    return bulb


def test_temperature_mode_is_reported_as_color_temp():
    assert _bulb(DUAL_MODE, color_mode="temperature")._color_mode == ColorMode.COLOR_TEMP


def test_color_mode_is_reported_as_hs():
    assert _bulb(DUAL_MODE, color_mode="color")._color_mode == ColorMode.HS


def test_model_without_the_field_keeps_hs_first():
    assert _bulb(DUAL_MODE)._color_mode == ColorMode.HS


def test_unknown_value_keeps_hs_first():
    assert _bulb(DUAL_MODE, color_mode=None)._color_mode == ColorMode.HS


def test_unrecognised_string_keeps_hs_first():
    assert _bulb(DUAL_MODE, color_mode="xy")._color_mode == ColorMode.HS


def test_color_on_a_ct_only_bulb_is_ignored():
    assert _bulb(CT_ONLY, color_mode="color")._color_mode == ColorMode.COLOR_TEMP


def test_temperature_on_an_hs_only_bulb_is_ignored():
    assert _bulb(HS_ONLY, color_mode="temperature")._color_mode == ColorMode.HS


def test_supported_modes_are_unchanged():
    bulb = _bulb(DUAL_MODE, color_mode="temperature")
    assert bulb._supported_color_modes == [ColorMode.COLOR_TEMP, ColorMode.HS]


# --- the library Light ------------------------------------------------------

def _device(color_mode):
    return {
        "id": "light-1", "type": "light", "deviceType": "light",
        "createdAt": "2026-01-01T00:00:00.000Z", "lastSeen": "2026-01-01T00:00:00.000Z",
        "isReachable": True, "customIcon": "lighting_bulb", "isHidden": False,
        "capabilities": {"canSend": [], "canReceive": DUAL_MODE},
        "room": None, "deviceSet": [], "remoteLinks": [],
        "attributes": {
            "customName": "Bulb", "model": "m", "manufacturer": "IKEA",
            "firmwareVersion": "1", "hardwareVersion": "1", "serialNumber": "1",
            "productCode": "p", "isOn": True, "lightLevel": 50,
            "colorTemperature": 2700, "colorTemperatureMin": 4000,
            "colorTemperatureMax": 2202, "colorHue": 30.0,
            "colorSaturation": 0.5, "colorMode": color_mode,
        },
    }


def _import_real_dirigera():
    """Import the real dirigera library, whatever the test order.

    test_reconnect_resync.py and test_registry_per_hub.py put a stub
    "dirigera" into sys.modules. Hide any stub during the import, then put
    the previous entries back, so those files keep their stubs.
    """
    def ours(name):
        return name == "dirigera" or name.startswith("dirigera.")

    saved = {name: sys.modules.pop(name) for name in list(sys.modules) if ours(name)}
    try:
        from dirigera.devices import light
        from dirigera.hub import abstract_smart_home_hub
        return light, abstract_smart_home_hub
    except ImportError:
        return None, None
    finally:
        for name in [name for name in sys.modules if ours(name)]:
            del sys.modules[name]
        sys.modules.update(saved)


_light_lib, _hub_lib = _import_real_dirigera()
if _light_lib is not None:
    dict_to_light = _light_lib.dict_to_light
    FakeDirigeraHub = _hub_lib.FakeDirigeraHub
else:
    FakeDirigeraHub = None

needs_dirigera = pytest.mark.skipif(FakeDirigeraHub is None, reason="dirigera not installed")


@needs_dirigera
@pytest.mark.parametrize("mode", ["temperature", "color"])
def test_library_light_keeps_color_mode(mode):
    light = dict_to_light(_device(mode), FakeDirigeraHub())
    assert light.attributes.color_mode == mode


@needs_dirigera
def test_light_without_color_mode_reports_none():
    data = _device("temperature")
    del data["attributes"]["colorMode"]
    light = dict_to_light(data, FakeDirigeraHub())
    assert light.attributes.color_mode is None


@needs_dirigera
def test_color_mode_field_accepts_listener_writes():
    # hub_event_listener stores a live colorMode with a plain attribute write.
    light = dict_to_light(_device("temperature"), FakeDirigeraHub())
    light.attributes.color_mode = "color"
    assert light.attributes.color_mode == "color"
