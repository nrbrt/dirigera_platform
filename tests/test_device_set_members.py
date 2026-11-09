"""
#71: a device set sends each member light its own command.

guaifi found that the hub's /devices/set/{id} PATCH left bulbs wrong or off
whatever the body looked like (6-bulb set: 21 of 24 bulb-runs wrong), and one
member ignored every set command while it worked on its own. Each member now
gets its own async_turn_on/off, the per-light PATCH through the per-hub queue
that went 172 of 172 right on his bench. One failing member must not stop
the others, and the failure must still reach Home Assistant.

Replaces test_device_set_echo.py: the set no longer PATCHes anything itself,
so there is no echo of its own to leave alone; each member writes its own
state through, as a single bulb does.

As in the other light tests, the code is lifted out of light.py with ast, so
it runs without importing homeassistant.
"""
import ast
import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

LIGHT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "light.py",
)


def _methods():
    with open(LIGHT) as fh:
        source = fh.read()
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ikea_bulb_device_set")
    fns = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
           and n.name in ("_each_member", "async_turn_on", "async_turn_off")]
    ns = {"asyncio": asyncio, "HomeAssistantError": RuntimeError, "DOMAIN": "dirigera_platform",
          "logger": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=fns, type_ignores=[])), LIGHT, "exec"), ns)
    return ns, source


NS, SOURCE = _methods()


class _Bulb:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.calls = []

    async def async_turn_on(self, **kwargs):
        self.calls.append(("on", kwargs))
        if self.fail:
            raise RuntimeError("unreachable")

    async def async_turn_off(self, **kwargs):
        self.calls.append(("off", kwargs))
        if self.fail:
            raise RuntimeError("unreachable")


def _set(bulbs):
    entity = SimpleNamespace(name="Living", _device_set=SimpleNamespace(get_lights=lambda: bulbs))
    entity._each_member = lambda method, **kw: NS["_each_member"](entity, method, **kw)
    return entity


def test_turn_on_reaches_every_member_with_the_same_arguments():
    bulbs = [_Bulb("a"), _Bulb("b"), _Bulb("c")]
    kwargs = {"brightness": 178, "color_temp_kelvin": 2700}
    asyncio.run(NS["async_turn_on"](_set(bulbs), **kwargs))
    assert [b.calls for b in bulbs] == [[("on", kwargs)]] * 3


def test_turn_off_reaches_every_member():
    bulbs = [_Bulb("a"), _Bulb("b")]
    asyncio.run(NS["async_turn_off"](_set(bulbs)))
    assert [b.calls for b in bulbs] == [[("off", {})]] * 2


def test_one_failing_member_does_not_stop_the_others_and_is_reported():
    bulbs = [_Bulb("a"), _Bulb("b", fail=True), _Bulb("c")]
    with pytest.raises(RuntimeError, match="b"):
        asyncio.run(NS["async_turn_on"](_set(bulbs), brightness=10))
    assert all(len(b.calls) == 1 for b in bulbs)


def test_the_set_endpoint_is_no_longer_used():
    strings = [n.value for n in ast.walk(ast.parse(SOURCE)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if "/devices/set/" in s]
