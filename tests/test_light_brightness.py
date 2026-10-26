"""
Brightness conversion between Home Assistant (0-255) and the hub (1-100).

Both directions truncated with int(), so a scene setting brightness 158 sent
61 % (61.96 truncated) and the bulb then showed 155 in HA; 140 came back as
137. The device-set path already rounded. All paths now share one rounding
pair, so a brightness that came from a hub level survives the round trip.

The helpers are lifted out of light.py with ast, as in the other light tests.
"""
import ast
import os

import pytest

LIGHT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "light.py",
)


def _load():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    wanted = {"brightness_to_hub_level", "hub_level_to_brightness"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), LIGHT, "exec"), namespace)
    return namespace["brightness_to_hub_level"], namespace["hub_level_to_brightness"]


to_hub, to_ha = _load()


@pytest.mark.parametrize("brightness", [158, 140])
def test_scene_brightness_survives_the_round_trip(brightness):
    assert to_ha(to_hub(brightness)) == brightness


def test_round_trip_is_off_by_at_most_one():
    # 255 HA values onto 100 hub levels cannot all come back exactly; with
    # rounding the error is at most 1 (truncating lost up to 3).
    assert all(abs(to_ha(to_hub(b)) - b) <= 1 for b in range(3, 256))


def test_every_hub_level_survives_the_round_trip():
    assert all(to_hub(to_ha(level)) == level for level in range(1, 101))


@pytest.mark.parametrize("brightness, level", [(0, 1), (1, 1), (2, 1), (255, 100), (300, 100)])
def test_level_stays_in_the_hub_range(brightness, level):
    assert to_hub(brightness) == level


def test_unknown_level_gives_unknown_brightness():
    assert to_ha(None) is None
