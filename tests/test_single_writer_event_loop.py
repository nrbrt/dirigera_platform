"""
One writer: hub events are applied on the event loop, never on the WebSocket thread.

on_message used to run on the WebSocket thread and write the entity models that
the event loop reads and writes too. Two things followed from that:

* The reconnect resync (#39) fetches /devices and replays it. A live event that
  arrived while the fetch was on its way was applied first and then overwritten
  by the older snapshot: the hub said 90, the model ended on 30.
* ikea_bulb.async_update replaces the model with a fetched one after an await,
  with the same loss.

The WebSocket thread now only parses and hands the message to the loop. While a
snapshot of device state is being fetched, state events are held and applied
after it, in arrival order.

The listener is loaded standalone with the stubs from test_reconnect_resync.
"""
import ast
import asyncio
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
HUB_KEY = "wss://single-writer.test"
LIGHT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "light.py",
)


class Attrs(BaseModel, validate_assignment=True):
    custom_name: str = "bulb"
    is_on: bool = True
    light_level: Optional[int] = 30


class _Light:
    should_ignore_update = False

    def __init__(self, device_id, level=30):
        self._json_data = SimpleNamespace(
            id=device_id, relation_id=None, room=None, is_reachable=True,
            attributes=Attrs(light_level=level),
        )
        self.unique_id = device_id
        self.writes = 0

    def reset_ignore_update(self):
        pass

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1

    # The pre-refactor code pushed state with the thread-safe variant.
    schedule_update_ha_state = async_schedule_update_ha_state

    @property
    def level(self):
        return self._json_data.attributes.light_level


class _Hass:
    def __init__(self):
        self._pool = ThreadPoolExecutor(2)

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


def _event(device_id, **attributes):
    return json.dumps({"type": "deviceStateChanged",
                       "data": {"id": device_id, "deviceType": "light", "attributes": attributes}})


def _snapshot(device_id, **attributes):
    return {"id": device_id, "deviceType": "light", "isReachable": True, "attributes": attributes}


@pytest.fixture
def setup():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hub = SimpleNamespace(websocket_base_url=HUB_KEY)
    listener = L(hub, _Hass())
    lights = {}

    def add(device_id, level=30):
        lights[device_id] = _Light(device_id, level)
        L.device_registry.setdefault(HUB_KEY, {})[device_id] = hel.registry_entry(lights[device_id])
        return lights[device_id]

    yield SimpleNamespace(loop=loop, hub=hub, listener=listener, add=add)
    L.unregister_hub(HUB_KEY)
    loop.close()


# --- the hand-off -------------------------------------------------------------

def test_on_message_from_another_thread_mutates_nothing_until_the_loop_runs(setup):
    bulb = setup.add("b1")
    thread = threading.Thread(target=setup.listener.on_message, args=(None, _event("b1", lightLevel=90)))
    thread.start()
    thread.join()

    assert bulb.level == 30, "the WebSocket thread wrote the model"
    assert bulb.writes == 0, "the WebSocket thread pushed state"

    setup.loop.run_until_complete(asyncio.sleep(0))
    assert bulb.level == 90
    assert bulb.writes == 1


def test_closed_loop_drops_the_event_without_noise(setup, caplog):
    # #42: during shutdown the WebSocket thread can outlive the event loop.
    bulb = setup.add("b1")
    closed = asyncio.new_event_loop()
    closed.close()
    setup.listener._loop = closed
    with caplog.at_level(logging.WARNING):
        setup.listener.on_message(None, _event("b1", lightLevel=90))
    assert bulb.level == 30
    assert caplog.text == ""


def test_event_handed_over_before_stop_is_not_applied_after_it(setup):
    bulb = setup.add("b1")
    setup.listener.on_message(None, _event("b1", lightLevel=90))
    setup.listener._request_to_stop = True
    setup.loop.run_until_complete(asyncio.sleep(0))
    assert bulb.level == 30
    assert bulb.writes == 0


# --- reconnect resync ---------------------------------------------------------

def _gated_get(snapshot):
    gate = threading.Event()

    def get(route):
        gate.wait(5)  # the hub built its answer; it is still on its way
        return snapshot

    return gate, get


def test_resync_snapshot_older_than_a_live_event_keeps_the_live_value(setup):
    bulb = setup.add("b6", level=30)
    gate, setup.hub.get = _gated_get([_snapshot("b6", isOn=True, lightLevel=30)])

    async def scenario():
        resync = asyncio.ensure_future(setup.listener._resync_all_states())
        await asyncio.sleep(0.05)
        setup.listener.on_message(None, _event("b6", lightLevel=90))  # newer than the snapshot
        await asyncio.sleep(0)
        gate.set()
        await resync
        await asyncio.sleep(0)

    setup.loop.run_until_complete(scenario())
    assert bulb.level == 90, "the older snapshot overwrote the live value"


def test_events_held_during_a_resync_are_applied_in_arrival_order(setup):
    bulb = setup.add("b6", level=30)
    other = setup.add("b7", level=10)
    gate, setup.hub.get = _gated_get([_snapshot("b6", lightLevel=30), _snapshot("b7", lightLevel=55)])

    async def scenario():
        resync = asyncio.ensure_future(setup.listener._resync_all_states())
        await asyncio.sleep(0.05)
        for level in (60, 90, 75):
            setup.listener.on_message(None, _event("b6", lightLevel=level))
        await asyncio.sleep(0)
        assert bulb.level == 30, "a state event was applied while the snapshot was in flight"
        gate.set()
        await resync
        await asyncio.sleep(0)

    setup.loop.run_until_complete(scenario())
    assert bulb.level == 75
    assert other.level == 55, "the resync must still catch up what no live event covered (#39)"


def test_events_flow_again_after_a_failed_resync(setup):
    bulb = setup.add("b6", level=30)

    def failing_get(route):
        raise OSError("hub unreachable")

    setup.hub.get = failing_get

    async def scenario():
        resync = asyncio.ensure_future(setup.listener._resync_all_states())
        await asyncio.sleep(0)
        setup.listener.on_message(None, _event("b6", lightLevel=90))
        await resync
        await asyncio.sleep(0)
        setup.listener.on_message(None, _event("b6", lightLevel=40))
        await asyncio.sleep(0)

    setup.loop.run_until_complete(scenario())
    assert bulb.level == 40


# --- ikea_bulb.async_update ---------------------------------------------------

def _bulb_async_update():
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    bulb = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ikea_bulb")
    method = next(n for n in bulb.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_update")
    ns = {
        "hub_event_listener": L,
        "HomeAssistantError": Exception,
        "DOMAIN": "dirigera_platform",
        "logger": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), LIGHT, "exec"), ns)
    return ns["async_update"]


def test_bulb_update_does_not_lose_an_event_that_arrives_during_the_fetch(setup):
    bulb = setup.add("b8", level=30)
    fetched = SimpleNamespace(id="b8", relation_id=None, room=None, is_reachable=True,
                              attributes=Attrs(light_level=30))
    gate, get = _gated_get(fetched)
    bulb._hub = SimpleNamespace(websocket_base_url=HUB_KEY, get_light_by_id=get)
    bulb.hass = setup.listener._hass
    bulb.name = "b8"
    bulb.set_state = lambda: None
    async_update = _bulb_async_update()

    async def scenario():
        update = asyncio.ensure_future(async_update(bulb))
        await asyncio.sleep(0.05)
        setup.listener.on_message(None, _event("b8", lightLevel=90))  # newer than the fetched model
        await asyncio.sleep(0)
        gate.set()
        await update
        await asyncio.sleep(0)

    setup.loop.run_until_complete(scenario())
    assert bulb._json_data is fetched
    assert bulb.level == 90, "the fetched model replaced a newer live value"
