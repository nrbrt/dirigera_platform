"""
A device refresh does not lose a hub event that arrives while it fetches.

ikea_base_device.async_update and _throttled_update fetch the device in the
executor and then replace the model hub events write to. The fetched model is
as old as the hub's answer; an event that arrived while the fetch was on its
way was applied to the old model and thrown away with it. Environment sensors,
blinds and air purifiers refresh this way every poll.

ikea_bulb.async_update had the same loss and holds the hub's state events for
the length of the fetch (hub_event_listener.hold_state_events, see
test_single_writer_event_loop.py). The base device does the same now.

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

    yield SimpleNamespace(bc=bc, loop=loop, device=device, listener=listener, answer=answer,
                          scenario=refresh_with_event_during_fetch)
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
