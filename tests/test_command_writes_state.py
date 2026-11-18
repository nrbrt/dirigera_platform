"""
A command writes the entity's state itself, without waiting for a poll or an echo.

Since should_poll follows the device's skip_update (f7f0687), Home Assistant no
longer refreshes a push-driven entity after a service call
(helpers/service.py: `if entity.should_poll: await entity.async_update_ha_state(True)`).
That refresh was what published the state a command had just set in the model:
the outlet, blinds and air purifier commands wrote the model (the library's
setters do) and left the state write to the poll. An outlet switched on from
Home Assistant stayed "off" until the hub's event arrived, or for good when it
did not.

Every command of a device goes through ikea_base_device._command now: through
the hub's writer, and once the hub accepted it a state write for every entity
of the device. A command the hub refused writes nothing.

base_classes.py is loaded standalone with the real hub_writer next to it, as in
test_throttle_trailing_flush.py.
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from test_throttle_trailing_flush import _load_base_classes

HUB_KEY = "wss://command-writes-state.test"


class _Hass:
    @property
    def loop(self):
        # Home Assistant's loop: here the one the test runs on. A blind
        # starts its wait for the next level report on it (#207).
        return asyncio.get_running_loop()

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class _Hub:
    websocket_base_url = HUB_KEY

    def __init__(self):
        self.error = None
        self.sent = []

    def patch(self, device_id, attribute, value):
        if self.error is not None:
            raise self.error
        self.sent.append((device_id, attribute, value))


def _model(hub, device_id, **attributes):
    """A library model: a setter PATCHes and then writes its own attribute."""
    model = SimpleNamespace(
        id=device_id, relation_id=None, room=None, is_reachable=True,
        capabilities=SimpleNamespace(can_receive=["blindsState"]),
        attributes=SimpleNamespace(custom_name=device_id, dict=lambda: {}, **attributes))

    def setter(hub_name, field):
        def set_value(value):
            hub.patch(device_id, hub_name, getattr(value, "value", value))
            setattr(model.attributes, field, value)
        return set_value

    model.set_on = setter("isOn", "is_on")
    model.set_target_level = setter("blindsTargetLevel", "blinds_target_level")
    model.set_state = setter("blindsState", "blinds_state")
    model.set_motor_state = setter("motorState", "motor_state")
    model.set_fan_mode = setter("fanMode", "fan_mode")
    model.set_child_lock = setter("childLock", "child_lock")
    model.set_status_light = setter("statusLight", "status_light")
    return model


def _entity(device, read):
    """Stands in for an entity of the device: records the state Home Assistant
    would get on every write."""
    entity = SimpleNamespace(hass=_Hass(), states=[])
    entity.async_schedule_update_ha_state = lambda force=False: entity.states.append(read())
    device.add_listener(entity)
    return entity


@pytest.fixture
def env():
    bc = _load_base_classes()
    hub_writer = sys.modules["dp_under_test.hub_writer"]
    hub = _Hub()
    hass = _Hass()
    finder = SimpleNamespace(websocket_base_url=HUB_KEY, get_outlet_by_id=None,
                             get_blinds_by_id=None, get_air_purifier_by_id=None)

    def run(command):
        async def main():
            writer = hub_writer.HubWriter(hass, hub, min_spacing=0)
            writer.start()
            try:
                return await command()
            finally:
                writer.stop()
        return asyncio.run(main())

    yield SimpleNamespace(bc=bc, hub=hub, hass=hass, finder=finder, run=run)
    bc.hub_event_listener.unregister_hub(HUB_KEY)


def test_switch_turn_on_leaves_the_state_on_without_an_echo(env):
    device = env.bc.ikea_outlet_device(env.hass, env.finder, _model(env.hub, "outlet_1", is_on=False))
    assert device.should_poll is False  # nothing else will write the state
    switch = _entity(device, lambda: device._json_data.attributes.is_on)
    power = _entity(device, lambda: "power")

    env.run(device.async_turn_on)
    assert env.hub.sent == [("outlet_1", "isOn", True)]
    assert switch.states == [True], "the switch was not written after turn_on"
    assert power.states == ["power"], "every entity of the device is written"

    env.run(device.async_turn_off)
    assert switch.states == [True, False]


def test_refused_command_writes_no_state(env):
    device = env.bc.ikea_outlet_device(env.hass, env.finder, _model(env.hub, "outlet_1", is_on=False))
    switch = _entity(device, lambda: device._json_data.attributes.is_on)
    env.hub.error = OSError("hub says no")
    with pytest.raises(Exception):
        env.run(device.async_turn_on)
    assert switch.states == []


def test_blinds_commands_write_state(env):
    device = env.bc.ikea_blinds_device(env.hass, env.finder, _model(
        env.hub, "blind_1", blinds_target_level=0, blinds_current_level=0, blinds_state="up"))
    cover = _entity(device, lambda: (device._json_data.attributes.blinds_target_level,
                                     device._json_data.attributes.blinds_state))
    env.run(device.async_close_cover)
    env.run(device.async_open_cover)
    env.run(lambda: device.async_set_cover_position(30))
    env.run(device.async_stop_cover)
    # The stop is in the state that is written, not set after it.
    assert cover.states == [(100, "up"), (0, "up"), (70, "up"), (70, "stopped")]


def test_air_purifier_commands_write_state(env):
    device = env.bc.ikea_starkvind_air_purifier_device(env.hass, env.finder, _model(
        env.hub, "purifier_1", motor_state=0, fan_mode=None, child_lock=False, status_light=True))
    attributes = device._json_data.attributes
    fan = _entity(device, lambda: (attributes.motor_state, attributes.child_lock, attributes.status_light))

    env.run(lambda: device.async_set_percentage(50))
    env.run(lambda: device.async_set_child_lock(True))
    env.run(lambda: device.async_set_status_light(False))
    assert fan.states == [(25, False, True), (25, True, True), (25, True, False)]

    env.run(lambda: device.async_set_preset_mode("auto"))
    env.run(lambda: device.async_turn_off())
    assert len(fan.states) == 5
    assert [sent[1] for sent in env.hub.sent[-2:]] == ["fanMode", "motorState"]


# --- #207 end review, finding 4: a command during a resync fetch -------------
#
# The safety net fetches /devices every five minutes. A command accepted while
# that fetch is on its way is newer than the snapshot; the snapshot must not
# put the old value back. live_writes knew only what hub events wrote.

@pytest.mark.parametrize("make, command, row_attributes, kept", [
    (lambda env: env.bc.ikea_blinds_device(env.hass, env.finder, _model(
        env.hub, "blind_1", blinds_target_level=0, blinds_current_level=0, blinds_state="up")),
     lambda device: device.async_close_cover,
     {"blindsTargetLevel": 0, "batteryPercentage": 80}, {"batteryPercentage": 80}),
    (lambda env: env.bc.ikea_blinds_device(env.hass, env.finder, _model(
        env.hub, "blind_1", blinds_target_level=100, blinds_current_level=40, blinds_state="down")),
     lambda device: device.async_stop_cover,
     {"blindsState": "down", "blindsCurrentLevel": 41}, {"blindsCurrentLevel": 41}),
    (lambda env: env.bc.ikea_outlet_device(env.hass, env.finder, _model(env.hub, "outlet_1", is_on=False)),
     lambda device: device.async_turn_on,
     {"isOn": False}, {}),
])
def test_a_command_during_a_resync_fetch_is_not_undone_by_the_snapshot(env, make, command, row_attributes, kept):
    hel = sys.modules["dp_under_test.hub_event_listener"]
    device = make(env)
    with hel.live_writes() as newer:  # what the resync opens while it fetches
        env.run(command(device))
    asyncio.set_event_loop(asyncio.new_event_loop())  # the listener asks for one
    listener = hel.hub_event_listener(SimpleNamespace(websocket_base_url=HUB_KEY), env.hass)
    row = {"id": device._json_data.id, "deviceType": "x", "attributes": dict(row_attributes)}
    assert listener._without_newer(row, newer)["attributes"] == kept


def test_a_command_outside_a_fetch_notes_nothing(env):
    hel = sys.modules["dp_under_test.hub_event_listener"]
    device = env.bc.ikea_outlet_device(env.hass, env.finder, _model(env.hub, "outlet_1", is_on=False))
    env.run(device.async_turn_on)
    with hel.live_writes() as newer:
        pass
    assert newer.attributes(device) == frozenset()
