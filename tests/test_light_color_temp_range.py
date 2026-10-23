"""
The colour temperature range a bulb offers in Home Assistant.

ikea_bulb sets colour temperature through the library's
Light.set_color_temperature(), which rejects values outside the range the bulb
reports. So min_color_temp_kelvin / max_color_temp_kelvin must describe exactly
that range, or HA offers values the library then refuses.

Bulbs report the range in either order (usually colorTemperatureMin is the
coldest, i.e. the highest Kelvin value), and some, such as KAJPLATS, report it
in mireds (153..454) while colorTemperature itself is in Kelvin.

As in the other tests, the helpers are lifted out of light.py with ast. The
contract tests need the dirigera library and skip without it.
"""
import ast
import os
import sys
from types import SimpleNamespace

import pytest

LIGHT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "light.py",
)


def _load_helpers():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    wanted = {"_bound_to_kelvin", "_kelvin_range"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    namespace = {}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), LIGHT, "exec"), namespace)
    return namespace["_kelvin_range"]


_kelvin_range = _load_helpers()


def _attributes(reported_min, reported_max):
    return SimpleNamespace(color_temperature_min=reported_min, color_temperature_max=reported_max)


# (colorTemperatureMin, colorTemperatureMax) as reported -> (warmest, coldest) in Kelvin
CASES = {
    "kelvin, usual order": ((4000, 2202), (2202, 4000)),
    "kelvin, ascending": ((2202, 4000), (2202, 4000)),
    "mireds (KAJPLATS)": ((153, 454), (2203, 6536)),
    "mireds, descending": ((454, 153), (2203, 6536)),
}


@pytest.mark.parametrize("reported, expected", CASES.values(), ids=CASES.keys())
def test_kelvin_range(reported, expected):
    assert _kelvin_range(_attributes(*reported)) == expected


def test_missing_bound_gives_no_range():
    assert _kelvin_range(_attributes(None, 2202)) == (None, None)
    assert _kelvin_range(_attributes(4000, None)) == (None, None)


# --- contract with dirigera's Light.set_color_temperature ---------------------

def _import_real_dirigera():
    """Import the real dirigera library, even if another test stubbed it."""
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
needs_dirigera = pytest.mark.skipif(_light_lib is None, reason="dirigera not installed")


def _library_light(reported):
    reported_min, reported_max = reported
    return _light_lib.dict_to_light({
        "id": "light-1", "type": "light", "deviceType": "light",
        "createdAt": "2026-01-01T00:00:00.000Z", "lastSeen": "2026-01-01T00:00:00.000Z",
        "isReachable": True, "customIcon": "lighting_bulb", "isHidden": False,
        "capabilities": {"canSend": [], "canReceive": ["isOn", "colorTemperature"]},
        "room": None, "deviceSet": [], "remoteLinks": [],
        "attributes": {
            "customName": "Bulb", "model": "m", "manufacturer": "IKEA",
            "firmwareVersion": "1", "hardwareVersion": "1", "serialNumber": "1",
            "productCode": "p", "isOn": True, "colorTemperature": 2700,
            "colorTemperatureMin": reported_min, "colorTemperatureMax": reported_max,
        },
    }, _hub_lib.FakeDirigeraHub())


@needs_dirigera
@pytest.mark.parametrize("reported", [c[0] for c in CASES.values()], ids=CASES.keys())
def test_library_accepts_the_whole_range(reported):
    light = _library_light(reported)
    warmest, coldest = _kelvin_range(light.attributes)
    light.set_color_temperature(warmest)
    light.set_color_temperature(coldest)
    assert light.attributes.color_temperature == coldest


@needs_dirigera
@pytest.mark.parametrize("reported", [c[0] for c in CASES.values()], ids=CASES.keys())
def test_library_rejects_just_outside_the_range(reported):
    light = _library_light(reported)
    warmest, coldest = _kelvin_range(light.attributes)
    for value in (warmest - 1, coldest + 1):
        with pytest.raises(ValueError):
            light.set_color_temperature(value)
