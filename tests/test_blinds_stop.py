"""
Issue #58: let Home Assistant stop a moving blind (cover.stop_cover).

The hub accepts blindsState = "stopped". ikea_blinds_device sends it through
the library's Blind.set_state(BlindsStateEnum.STOPPED) (dirigera >= 1.3.0), and
ikea_blinds_sensor advertises CoverEntityFeature.STOP so one button can start
and stop a blind.

What is guarded here:

1. The stop request: right route and payload, run in the executor, refused with
   a HomeAssistantError when the blind does not list blindsState in canReceive.
2. The STOP feature: advertised only for blinds that can actually be stopped.
3. The state after a stop. Push events only refresh blindsCurrentLevel, so a
   stop halfway through a move leaves current != target in the cache. Without
   the stop_requested override is_closing / is_opening would report a movement
   that no longer happens.

As in test_light_color_mode.py, the classes under test are lifted out of
base_classes.py with ast, so the real bodies run without homeassistant.
"""
import ast
import asyncio
import enum
import os
from types import SimpleNamespace

import pytest

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
BASE_CLASSES = os.path.join(COMPONENT, "base_classes.py")


class CoverEntityFeature(enum.IntFlag):
    OPEN = 1
    CLOSE = 2
    SET_POSITION = 4
    STOP = 8


class HomeAssistantError(Exception):
    pass


class BlindsStateEnum(enum.Enum):
    UP = "up"
    DOWN = "down"
    STOPPED = "stopped"


class _StubDevice:
    """Stands in for ikea_base_device: keeps what the blinds classes rely on."""

    def __init__(self, hass, hub, json_data, get_by_id_fx):
        self._hass = hass
        self._hub = hub
        self._json_data = json_data

    # induce_properties() exposes the model attributes like this at runtime.
    @property
    def blinds_current_level(self):
        return self._json_data.attributes.blinds_current_level

    @property
    def blinds_target_level(self):
        return self._json_data.attributes.blinds_target_level

    @property
    def name(self):
        return "Test blind"


class _StubSensor:
    def __init__(self, device, **kwargs):
        self._device = device


def _load_classes():
    with open(BASE_CLASSES) as fh:
        tree = ast.parse(fh.read())
    wanted = {"ikea_blinds_device", "ikea_blinds_sensor"}
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    namespace = {
        "core": SimpleNamespace(HomeAssistant=object),
        "Hub": object,
        "Blind": object,
        "BlindsStateEnum": BlindsStateEnum,
        "CoverEntity": object,
        "CoverDeviceClass": SimpleNamespace(BLIND="blind"),
        "CoverEntityFeature": CoverEntityFeature,
        "HomeAssistantError": HomeAssistantError,
        "ikea_base_device": _StubDevice,
        "ikea_base_device_sensor": _StubSensor,
        "logger": SimpleNamespace(debug=lambda *a, **k: None),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), BASE_CLASSES, "exec"), namespace)
    return namespace["ikea_blinds_device"], namespace["ikea_blinds_sensor"]


Device, Sensor = _load_classes()


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def patch(self, route, data):
        if self.fail:
            raise RuntimeError("hub unreachable")
        self.calls.append((route, data))


def make_blind(can_receive=("blindsTargetLevel", "blindsState"), client=None,
               current=60, target=100, state="down"):
    """A blind in the hub's own terms: 0 = open, 100 = closed.

    Default: closing, currently 60 closed (HA position 40), heading for 100."""
    client = client or FakeClient()
    blind = SimpleNamespace(
        id="blind-1",
        capabilities=SimpleNamespace(can_receive=list(can_receive), can_send=[]),
        attributes=SimpleNamespace(
            blinds_current_level=current,
            blinds_target_level=target,
            blinds_state=state,
        ),
        dirigera_client=client,
        moves=[],
    )
    blind.set_target_level = lambda level: (
        blind.moves.append(level),
        setattr(blind.attributes, "blinds_target_level", level),
    )

    def set_state(state):
        # Same order as dirigera's Blind.set_state: PATCH first, then the
        # local model, so a failed request leaves blinds_state alone.
        client.patch(f"/devices/{blind.id}", [{"attributes": {"blindsState": state.value}}])
        blind.states.append(state)
        blind.attributes.blinds_state = state.value

    blind.states = []
    blind.set_state = set_state
    return blind


def make_entity(blind):
    device = Device(FakeHass(), SimpleNamespace(get_blinds_by_id=lambda _id: blind), blind)
    return device, Sensor(device)


def run(coro):
    return asyncio.run(coro)


# --- 1. the stop request -----------------------------------------------------

def test_stop_patches_hub_with_blinds_state_stopped():
    client = FakeClient()
    blind = make_blind(client=client)
    _, entity = make_entity(blind)

    run(entity.async_stop_cover())

    assert blind.states == [BlindsStateEnum.STOPPED]
    assert client.calls == [
        ("/devices/blind-1", [{"attributes": {"blindsState": "stopped"}}])
    ]
    assert blind.attributes.blinds_state == "stopped"


def test_stop_refused_when_blind_cannot_receive_blinds_state():
    client = FakeClient()
    blind = make_blind(can_receive=("blindsTargetLevel",), client=client)
    _, entity = make_entity(blind)

    with pytest.raises(HomeAssistantError):
        run(entity.async_stop_cover())

    assert client.calls == []


def test_failed_stop_request_propagates_and_leaves_state_alone():
    blind = make_blind(client=FakeClient(fail=True))
    device, entity = make_entity(blind)

    with pytest.raises(RuntimeError):
        run(entity.async_stop_cover())

    assert device.stop_requested is False
    assert blind.attributes.blinds_state == "down"
    assert entity.is_closing is True


# --- 2. the STOP feature -----------------------------------------------------

def test_stop_feature_advertised_when_blind_supports_it():
    _, entity = make_entity(make_blind())
    assert entity.supported_features & CoverEntityFeature.STOP
    # the existing features are untouched
    assert entity.supported_features & CoverEntityFeature.OPEN
    assert entity.supported_features & CoverEntityFeature.CLOSE
    assert entity.supported_features & CoverEntityFeature.SET_POSITION


def test_stop_feature_hidden_when_blind_does_not_support_it():
    _, entity = make_entity(make_blind(can_receive=("blindsTargetLevel",)))
    assert not entity.supported_features & CoverEntityFeature.STOP
    assert entity.supported_features & CoverEntityFeature.SET_POSITION


# --- 3. state after a stop ---------------------------------------------------

def test_control_without_stop_a_moving_blind_reports_closing():
    _, entity = make_entity(make_blind())
    assert entity.current_cover_position == 40
    assert entity.target_cover_position == 0
    assert entity.is_closing is True
    assert entity.is_opening is False


def test_stopped_closing_blind_no_longer_reports_closing():
    blind = make_blind()  # closing, target still 100 in the cache after the stop
    _, entity = make_entity(blind)

    run(entity.async_stop_cover())

    assert entity.is_closing is False
    assert entity.is_opening is False


def test_stopped_opening_blind_no_longer_reports_opening():
    # opening: heading for 0 (fully open), currently 30 closed -> HA position 70
    blind = make_blind(current=30, target=0, state="up")
    _, entity = make_entity(blind)
    assert entity.is_opening is True

    run(entity.async_stop_cover())

    assert entity.is_opening is False
    assert entity.is_closing is False


def test_position_still_tracks_pushes_after_a_stop():
    blind = make_blind()
    _, entity = make_entity(blind)
    run(entity.async_stop_cover())

    # the blind coasts a little, the hub pushes the final blindsCurrentLevel
    blind.attributes.blinds_current_level = 63

    assert entity.current_cover_position == 37
    assert entity.is_closing is False


def test_move_command_after_a_stop_resumes_movement_reporting():
    blind = make_blind()
    device, entity = make_entity(blind)
    run(entity.async_stop_cover())
    assert entity.is_closing is False

    run(entity.async_close_cover())

    assert blind.moves == [100]
    assert device.stop_requested is False
    assert entity.is_closing is True


@pytest.mark.parametrize("command, kwargs", [
    ("async_open_cover", {}),
    ("async_close_cover", {}),
    ("async_set_cover_position", {"position": 25}),
])
def test_every_move_command_clears_the_stop(command, kwargs):
    blind = make_blind()
    device, entity = make_entity(blind)
    run(entity.async_stop_cover())
    assert device.stop_requested is True

    run(getattr(entity, command)(**kwargs))

    assert device.stop_requested is False


def test_new_target_from_the_hub_clears_the_stop():
    # e.g. the blind is moved again from the IKEA app or a remote
    blind = make_blind()
    device, entity = make_entity(blind)
    run(entity.async_stop_cover())
    assert entity.is_closing is False

    blind.attributes.blinds_target_level = 0  # now opening

    assert device.stop_requested is False
    assert entity.is_opening is True


def test_refreshed_model_with_unchanged_target_stays_stopped():
    # async_update() swaps in a fresh model from the hub; if the hub keeps the
    # last commanded target, the stop must survive the refresh.
    blind = make_blind()
    device, entity = make_entity(blind)
    run(entity.async_stop_cover())

    device._json_data = make_blind(current=63, target=100, state="stopped")

    assert entity.is_closing is False
