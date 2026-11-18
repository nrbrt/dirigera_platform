"""
should_poll is derived once, from the device, instead of per entity class.

A device with skip_update=True never fetches from the hub: its async_update
returns at once and everything it knows arrives over the WebSocket. Home
Assistant still polled every companion entity of such a device each 30 s,
except the three outlet sensors that carried their own `_attr_should_poll =
False` (#40). The rule now lives in the shared base classes.

Devices that do fetch (environment sensors, blinds, air purifiers) keep being
polled, and a class can still opt out with `_attr_should_poll = False`, which
the pushed environment values do.
"""
from types import SimpleNamespace

import pytest

from test_throttle_trailing_flush import _load_base_classes


@pytest.fixture(scope="module")
def bc():
    module = _load_base_classes()
    # What Home Assistant's Entity provides; the stub classes are empty.
    for stub in (module.SensorEntity, module.BinarySensorEntity, module.SwitchEntity,
                 module.CoverEntity, module.FanEntity):
        stub._attr_should_poll = True
        stub.should_poll = property(lambda self: self._attr_should_poll)
    return module


def _device(bc, cls, device_id):
    hub = SimpleNamespace(websocket_base_url=f"wss://should-poll.test/{device_id}",
                          get_outlet_by_id=None, get_controller_by_id=None,
                          get_environment_sensor_by_id=None, get_blinds_by_id=None,
                          get_motion_sensor_by_id=None, get_open_close_by_id=None,
                          get_water_sensor_by_id=None, get_light_sensor_by_id=None)
    attributes = SimpleNamespace(dict=lambda: {}, model="x")
    json_data = SimpleNamespace(id=device_id, relation_id=None, room=None, attributes=attributes)
    return cls(None, hub, json_data)


@pytest.mark.parametrize("sensor", [
    "ikea_outlet_switch_sensor",
    "battery_percentage_sensor",
    "current_amps_sensor",
    "current_active_power_sensor",
    "current_voltage_sensor",
    "total_energy_consumed_sensor",
    "energy_consumed_at_last_reset_sensor",
    "time_of_last_energy_reset_sensor",
    "total_energy_consumed_last_updated_sensor",
])
def test_companions_of_a_push_only_device_are_not_polled(bc, sensor):
    outlet = _device(bc, bc.ikea_outlet_device, "plug_1")
    assert outlet.skip_update is True
    assert getattr(bc, sensor)(outlet).should_poll is False


@pytest.mark.parametrize("device, sensor", [
    ("ikea_motion_sensor_device", "ikea_motion_sensor"),
    ("ikea_open_close_device", "ikea_open_close_sensor"),
    ("ikea_water_sensor_device", "ikea_water_sensor"),
    ("ikea_light_sensor_device", "ikea_light_sensor_lux"),
])
def test_push_only_sensors_are_not_polled(bc, device, sensor):
    dev = _device(bc, getattr(bc, device), "sensor_1")
    assert dev.skip_update is True
    assert getattr(bc, sensor)(dev).should_poll is False


def test_controller_is_its_own_entity_and_is_not_polled(bc):
    assert _device(bc, bc.ikea_controller_device, "remote_1").should_poll is False


def test_companions_of_a_fetching_device_keep_polling(bc):
    blinds = _device(bc, bc.ikea_blinds_device, "blind_1")
    assert blinds.skip_update is False
    assert bc.battery_percentage_sensor(blinds).should_poll is True
    assert bc.ikea_blinds_sensor(blinds).should_poll is True


def test_environment_sensor_polls_only_what_the_push_does_not_carry(bc):
    env = _device(bc, bc.ikea_vindstyrka_device, "env_1")
    assert env.skip_update is False
    # Pushed values opt out themselves (#40) ...
    assert bc.ikea_vindstyrka_temperature(env).should_poll is False
    assert bc.ikea_vindstyrka_humidity(env).should_poll is False
    assert bc.ikea_vindstyrka_voc_index(env).should_poll is False
    assert bc.ikea_alpstuga_co2(env).should_poll is False
    assert bc.ikea_vindstyrka_pm25(env, bc.WhichPM25.CURRENT).should_poll is False
    # ... min/max PM2.5 only come with a fetch.
    assert bc.ikea_vindstyrka_pm25(env, bc.WhichPM25.MIN).should_poll is True
    assert bc.ikea_vindstyrka_pm25(env, bc.WhichPM25.MAX).should_poll is True


# --- #207: polling is off unless a device type asks for it ---------------------
#
# Every entity that polls fetches its own device from the hub, every 15-30 s.
# With many devices that load made the hub unresponsive (#207), while nearly
# everything already arrives over the WebSocket. So a device type polls only
# when it says so, with the reason next to it, and a new type does not poll
# by accident.

def test_a_device_type_does_not_poll_unless_it_says_so(bc):
    class new_kind(bc.ikea_base_device):
        def __init__(self, hass, hub, json_data):
            super().__init__(hass, hub, json_data, None)

    device = _device(bc, new_kind, "new_1")
    assert device.skip_update is False  # it could fetch ...
    assert device.should_poll is False  # ... but nobody asked it to poll
    assert bc.battery_percentage_sensor(device).should_poll is False


def test_the_device_types_that_poll(bc):
    # A change here is a decision about the load on the hub, not a detail.
    polling = sorted(
        name for name, cls in vars(bc).items()
        if isinstance(cls, type) and issubclass(cls, bc.ikea_base_device)
        and cls.polls_hub
    )
    assert polling == [
        "ikea_blinds_device",
        "ikea_starkvind_air_purifier_device",
        "ikea_vindstyrka_device",
    ]
