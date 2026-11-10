"""
A device refresh does not lose a hub event that arrives while it fetches.

ikea_base_device.async_update and _throttled_update fetch the device in the
executor and then replace the model hub events write to. The fetched model is
as old as the hub's answer; an event that arrived while the fetch was on its
way was applied to the old model and thrown away with it. Environment sensors,
blinds and air purifiers refresh this way every poll.

No event waits for a fetch: a slow hub must not delay a motion sensor, or the
device's own events, for the length of a poll. Events are applied as they
come, and what they wrote to the refreshing entity while the fetch was on its
way (hub_event_listener.live_writes) is carried over to the fetched model,
including what another part of a split device that is routed to it wrote.
ikea_bulb.async_update and the reconnect resync follow the same rule (see
test_single_writer_event_loop.py).

base_classes.py is loaded standalone with the real hub_event_listener next to
it, as in test_throttle_trailing_flush.py.
"""
import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional

import pytest
from pydantic import BaseModel

from test_throttle_trailing_flush import _load_base_classes

HUB_KEY = "wss://fetch-keeps-events.test"


class Attrs(BaseModel):
    custom_name: str = "sensor"
    current_temperature: Optional[float] = 20.0

    def dict(self):  # what induce_properties asks for
        return self.model_dump()


def _model(device_id, temperature):
    return SimpleNamespace(id=device_id, relation_id=None, room=None, is_reachable=True,
                           attributes=Attrs(current_temperature=temperature))


class _Hass:
    def __init__(self):
        self._pool = ThreadPoolExecutor(2)

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


@pytest.fixture
def setup():
    bc = _load_base_classes()
    listener_cls = sys.modules["dp_under_test.hub_event_listener"].hub_event_listener
    assert bc.hub_event_listener is listener_cls
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = _Hass()
    hub = SimpleNamespace(websocket_base_url=HUB_KEY)
    listener = listener_cls(hub, hass, None)

    in_fetch, release = threading.Event(), threading.Event()
    answer = {}

    def get_by_id(device_id):
        # The hub built its answer here; what happens from now on is newer.
        in_fetch.set()
        assert release.wait(5)
        if "error" in answer:
            raise answer["error"]
        return answer["model"]

    device = bc.ikea_base_device(hass, hub, _model("env_1", 20.0), get_by_id)
    device.writes = 0
    device.async_schedule_update_ha_state = lambda force=False: setattr(
        device, "writes", device.writes + 1)

    async def refresh_with_event_during_fetch(refresh, temperature):
        update = asyncio.ensure_future(refresh())
        await loop.run_in_executor(None, in_fetch.wait, 5)
        listener.on_message(None, json.dumps({"type": "deviceStateChanged", "data": {
            "id": "env_1", "deviceType": "environmentSensor",
            "attributes": {"currentTemperature": temperature}}}))
        await asyncio.sleep(0)
        release.set()
        try:
            await update
        finally:
            await asyncio.sleep(0)

    def other_device(device_id):
        other = bc.ikea_base_device(hass, hub, _model(device_id, 20.0), get_by_id)
        other.async_schedule_update_ha_state = lambda force=False: None
        return other

    def send(device_id, temperature):
        listener.on_message(None, json.dumps({"type": "deviceStateChanged", "data": {
            "id": device_id, "deviceType": "environmentSensor",
            "attributes": {"currentTemperature": temperature}}}))

    yield SimpleNamespace(bc=bc, loop=loop, device=device, listener=listener, answer=answer,
                          scenario=refresh_with_event_during_fetch, hass=hass,
                          listener_cls=listener_cls, other_device=other_device, send=send,
                          in_fetch=in_fetch, release=release)
    listener_cls.unregister_hub(HUB_KEY)
    hass._pool.shutdown(wait=False)
    loop.close()


@pytest.mark.parametrize("refresh", ["async_update", "_throttled_update"])
def test_event_during_the_fetch_survives_the_new_model(setup, refresh):
    fetched = _model("env_1", 20.0)  # what the hub knew when it answered
    setup.answer["model"] = fetched
    setup.loop.run_until_complete(setup.scenario(getattr(setup.device, refresh), 25.5))

    assert setup.device._json_data is fetched
    assert setup.device._json_data.attributes.current_temperature == 25.5, \
        "the fetched model replaced a newer live value"
    assert setup.device.writes == 1


@pytest.mark.parametrize("refresh", ["async_update", "_throttled_update"])
def test_event_during_a_failed_fetch_is_still_applied(setup, refresh):
    old = setup.device._json_data
    setup.answer["error"] = OSError("hub does not respond")
    with pytest.raises(Exception):
        setup.loop.run_until_complete(setup.scenario(getattr(setup.device, refresh), 25.5))

    assert setup.device._json_data is old
    assert old.attributes.current_temperature == 25.5
    assert setup.device.writes == 1


def test_events_flow_again_after_the_refresh(setup):
    setup.answer["model"] = _model("env_1", 20.0)
    setup.loop.run_until_complete(setup.scenario(setup.device.async_update, 25.5))

    async def later():
        setup.listener.on_message(None, json.dumps({"type": "deviceStateChanged", "data": {
            "id": "env_1", "deviceType": "environmentSensor",
            "attributes": {"currentTemperature": 26.0}}}))
        await asyncio.sleep(0)

    setup.loop.run_until_complete(later())
    assert setup.device._json_data.attributes.current_temperature == 26.0


# --- nothing waits for a fetch --------------------------------------------------
#
# A refresh used to hold the events of its device until the fetch was back, up
# to the hub's timeout. Events are applied as they come now; the fetch learns
# what they wrote (hub_event_listener.live_writes) and keeps it.

@pytest.mark.parametrize("refresh", ["async_update", "_throttled_update"])
def test_events_are_applied_while_a_refresh_is_on_its_way(setup, refresh):
    motion = setup.other_device("motion_1")
    setup.answer["model"] = fetched = _model("env_1", 20.0)
    seen = {}

    async def scenario():
        update = asyncio.ensure_future(getattr(setup.device, refresh)())
        await setup.loop.run_in_executor(None, setup.in_fetch.wait, 5)
        setup.send("motion_1", 30.0)
        setup.send("env_1", 25.5)
        await asyncio.sleep(0)
        # The fetch of env_1 is still on its way.
        seen["other"] = motion._json_data.attributes.current_temperature
        seen["own"] = setup.device._json_data.attributes.current_temperature
        setup.release.set()
        await update
        await asyncio.sleep(0)

    setup.loop.run_until_complete(scenario())
    assert seen == {"other": 30.0, "own": 25.5}, "an event waited for the fetch"
    assert setup.device._json_data is fetched
    assert fetched.attributes.current_temperature == 25.5


def test_refresh_keeps_what_the_other_part_routed_to_the_device_wrote(setup):
    # env_2 has no entity of its own; the listener routes it to env_1.
    setup.answer["model"] = fetched = _model("env_1", 20.0)

    async def scenario():
        update = asyncio.ensure_future(setup.device.async_update())
        await setup.loop.run_in_executor(None, setup.in_fetch.wait, 5)
        setup.send("env_2", 25.5)
        await asyncio.sleep(0)
        setup.release.set()
        await update

    setup.loop.run_until_complete(scenario())
    assert setup.device._json_data is fetched
    assert fetched.attributes.current_temperature == 25.5


def test_refresh_takes_from_the_hub_what_no_event_wrote(setup):
    fetched = _model("env_1", 23.0)
    fetched.attributes.custom_name = "renamed"
    setup.answer["model"] = fetched

    async def scenario():
        update = asyncio.ensure_future(setup.device.async_update())
        await setup.loop.run_in_executor(None, setup.in_fetch.wait, 5)
        setup.send("motion_1", 30.0)  # another device: nothing of env_1 is newer
        await asyncio.sleep(0)
        setup.release.set()
        await update

    setup.other_device("motion_1")
    setup.loop.run_until_complete(scenario())
    assert fetched.attributes.current_temperature == 23.0
    assert fetched.attributes.custom_name == "renamed"


def test_last_of_several_events_during_the_fetch_is_kept(setup):
    setup.answer["model"] = fetched = _model("env_1", 20.0)

    async def scenario():
        update = asyncio.ensure_future(setup.device.async_update())
        await setup.loop.run_in_executor(None, setup.in_fetch.wait, 5)
        for value in (21.0, 22.0, 23.0):
            setup.send("env_1", value)
        await asyncio.sleep(0)
        setup.release.set()
        await update

    setup.loop.run_until_complete(scenario())
    assert fetched.attributes.current_temperature == 23.0


def test_event_before_the_fetch_began_does_not_count_as_newer(setup):
    setup.answer["model"] = fetched = _model("env_1", 23.0)

    async def scenario():
        setup.send("env_1", 25.5)
        await asyncio.sleep(0)
        update = asyncio.ensure_future(setup.device.async_update())
        await setup.loop.run_in_executor(None, setup.in_fetch.wait, 5)
        setup.release.set()
        await update

    setup.loop.run_until_complete(scenario())
    assert fetched.attributes.current_temperature == 23.0
