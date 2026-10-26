"""
TIMMERFLOTTE: the merged split sensor must not depend on the hub's list order.

TIMMERFLOTTE reports as two environmentSensor devices sharing a relationId:
<rel>_1 carries the temperature, the user's name and the room, <rel>_2 carries
the humidity under the generic name "TIMMERFLOTTE temp/hmd sensor" and no room.
get_environment_sensors() merges the pair into one sensor.

The merge used to keep whichever half the hub listed first. On a hub that
lists _2 first the merged sensor took the humidity half's id, name and (empty)
room, and get_environment_sensor_by_id(<rel>_1) raised ValueError on refresh.
The merge also wrote into the /devices response through a shallow copy.

The fixture is shaped after a real /devices response from a DIRIGERA on
firmware 2.975.3 (five TIMMERFLOTTE on fw 1.0.21, ids replaced). The real hub
listed _1 first; the reversed order is the case being guarded.

As in test_matter_filtering.py, the functions under test are lifted out of
the source with ast and executed against a stub, so the real bodies run
without importing dirigera or homeassistant.
"""
import __future__
import ast
import copy
import logging
import os

import pytest

PATCH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "dirigera_lib_patch.py",
)

with open(PATCH) as fh:
    TREE = ast.parse(fh.read())


class _Sensor(dict):
    id = property(lambda self: self["id"])


def _lift(names, class_name=None):
    """Compile the named functions from dirigera_lib_patch.py into one namespace."""
    body = TREE.body
    if class_name is not None:
        body = next(n for n in TREE.body
                    if isinstance(n, ast.ClassDef) and n.name == class_name).body
    nodes = [n for n in body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names), f"missing one of {names} in dirigera_lib_patch.py"
    namespace = {
        "copy": copy,
        "logger": logging.getLogger("test"),
        # The real one builds a dirigera EnvironmentSensor; a dict with .id is enough.
        "dict_to_environment_sensor": lambda data, hub: _Sensor(data),
    }
    # The source uses `from __future__ import annotations`; keep that so the
    # dirigera types in the signatures are never evaluated.
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 PATCH, "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


RANK = _lift(["_split_sensor_primary_rank"])["_split_sensor_primary_rank"]
METHODS = _lift(["get_environment_sensors", "get_environment_sensor_by_id"], "HubX")
METHODS["get_environment_sensors"].__globals__["_split_sensor_primary_rank"] = RANK


class StubHub:
    def __init__(self, devices):
        self._devices = devices

    def get(self, path):
        assert path == "/devices"
        return self._devices

    get_environment_sensors = METHODS["get_environment_sensors"]
    get_environment_sensor_by_id = METHODS["get_environment_sensor_by_id"]


def _timmerflotte(rel, name, room, temperature, humidity, battery):
    common = {"model": "TIMMERFLOTTE temp/hmd sensor", "batteryPercentage": battery}
    return [
        {"id": f"{rel}_1", "relationId": rel, "deviceType": "environmentSensor",
         "room": {"id": f"room-{rel}", "name": room},
         "attributes": {**common, "customName": name, "currentTemperature": temperature}},
        {"id": f"{rel}_2", "relationId": rel, "deviceType": "environmentSensor",
         "attributes": {**common, "customName": "TIMMERFLOTTE temp/hmd sensor",
                        "currentRH": humidity}},
    ]


@pytest.fixture
def devices():
    return [
        *_timmerflotte("aaaa", "Garage", "Garage", 19.48, 47, 71),
        *_timmerflotte("bbbb", "Bedroom", "Bedroom", 19.85, 51, 49),
        {"id": "vind", "deviceType": "environmentSensor", "room": {"id": "r", "name": "Office"},
         "attributes": {"model": "VINDSTYRKA", "customName": "Office air",
                        "currentTemperature": 21.0, "currentRH": 40}},
        {"id": "lamp", "deviceType": "light", "attributes": {"customName": "Lamp"}},
    ]


@pytest.mark.parametrize("order", ["hub", "reversed"])
def test_merge_keeps_the_primary_half(devices, order):
    if order == "reversed":
        devices = list(reversed(devices))
    sensors = {s["id"]: s for s in StubHub(devices).get_environment_sensors()}

    assert set(sensors) == {"aaaa_1", "bbbb_1", "vind"}
    garage = sensors["aaaa_1"]
    assert garage["room"]["name"] == "Garage"
    assert garage["attributes"]["customName"] == "Garage"
    assert garage["attributes"]["currentTemperature"] == 19.48
    assert garage["attributes"]["currentRH"] == 47


@pytest.mark.parametrize("order", ["hub", "reversed"])
def test_lookup_by_primary_id(devices, order):
    if order == "reversed":
        devices = list(reversed(devices))
    sensor = StubHub(devices).get_environment_sensor_by_id("bbbb_1")
    assert sensor["attributes"]["currentRH"] == 51


def test_merge_does_not_write_into_the_response(devices):
    snapshot = copy.deepcopy(devices)
    StubHub(devices).get_environment_sensors()
    assert devices == snapshot


def test_room_is_inherited_when_only_the_secondary_has_one():
    devices = _timmerflotte("cccc", "Hall", "Hall", 20.0, 50, 90)
    devices[1]["room"] = devices[0].pop("room")
    (sensor,) = StubHub(devices).get_environment_sensors()
    assert sensor["id"] == "cccc_1"
    assert sensor["room"]["name"] == "Hall"
