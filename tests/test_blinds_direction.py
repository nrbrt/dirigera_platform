"""
#207 point 4: which way a blind is moving.

The direction used to be read from the target level: closing only when the
target was 100, opening only when it was 0. That went wrong in three ways:

* a move to any other position got no direction, or the old one;
* a blind moved with its remote changes only blindsCurrentLevel, so the
  stale target from an earlier command decided the direction (a blind going
  down showed "opening");
* a blind that stopped short of its target kept "opening" or "closing" for
  good, also after a restart.

Now the direction comes from what the blind does: a rising current level is
closing (the hub's levels run from 0 = open to 100 = closed), a falling one is
opening. A command sets the direction at once, so the UI reacts before the
first level report. When no level report has arrived for SETTLE_SECONDS, or
the level reached the target, the blind is open or closed again.

A stop opens a window of SETTLE_SECONDS in which the blind may coast: level
reports in it are no movement, and they do not extend it. After it the next
change is a movement again, also one started with the remote.

The device classes are loaded as in test_blinds_stop.py, with the real bodies
and without Home Assistant. Time is a fake clock: nothing here sleeps. The
last section runs a hub event through the real ikea_base_device and the real
hub_event_listener, which test_blinds_stop.py stubs.
"""
import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Optional

import pytest
from pydantic import BaseModel

import test_blinds_stop as blinds
from test_blinds_stop import make_blind

SETTLE = 5


class _Timer:
    def __init__(self, when, fn, args):
        self._when, self.fn, self.args = when, fn, args
        self.cancelled = False

    def when(self):
        return self._when

    def cancel(self):
        self.cancelled = True


class Clock:
    """A loop that only keeps time: call_later timers fire on advance()."""

    def __init__(self):
        self.now = 0.0
        self.timers = []

    def time(self):
        return self.now

    def call_later(self, delay, fn, *args):
        timer = _Timer(self.now + delay, fn, args)
        self.timers.append(timer)
        return timer

    def pending(self):
        return [t for t in self.timers if not t.cancelled]

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.pending() if t.when() <= end]
            if not due:
                break
            timer = min(due, key=_Timer.when)
            self.now = timer.when()
            self.timers.remove(timer)
            timer.fn(*timer.args)
        self.now = end


class ClockHass:
    def __init__(self):
        self.loop = Clock()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _entity(**kwargs):
    blind = make_blind(**kwargs)
    hass = ClockHass()
    device = blinds.Device(hass, SimpleNamespace(get_blinds_by_id=lambda _id: blind), blind)
    return blind, device, blinds.Sensor(device), hass.loop


def _report(device, blind, level, target=None):
    """A hub event with a new current level, as hub_event_listener applies it."""
    blind.attributes.blinds_current_level = level
    if target is not None:
        blind.attributes.blinds_target_level = target
    device.async_schedule_update_ha_state(False)


def _direction(entity):
    return ("opening" if entity.is_opening else "") + ("closing" if entity.is_closing else "")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pushes(monkeypatch):
    """Counts the state writes the device hands to its entities."""
    count = []
    monkeypatch.setattr(blinds._StubDevice, "async_schedule_update_ha_state",
                        lambda self, force_refresh=False: count.append(force_refresh))
    return count


# --- from the level --------------------------------------------------------------

@pytest.mark.parametrize("start, levels, expected", [
    (0, (20, 40), "closing"),   # 0 = open, 100 = closed: rising is closing
    (100, (80, 60), "opening"),
])
def test_direction_follows_the_current_level(start, levels, expected):
    blind, device, entity, _ = _entity(current=start, target=50)
    for level in levels:
        _report(device, blind, level)
    assert _direction(entity) == expected


def test_remote_move_against_a_stale_target_shows_the_real_direction():
    # The last command opened the blind (target 0). Now the remote sends it
    # down: the hub reports only the current level, the target stays 0.
    blind, device, entity, _ = _entity(current=0, target=0)
    for level in (10, 25, 40):
        _report(device, blind, level)
    assert _direction(entity) == "closing"


def test_a_reversal_halfway_shows_the_new_direction():
    blind, device, entity, _ = _entity(current=0, target=0)
    _report(device, blind, 30)
    _report(device, blind, 50)
    assert _direction(entity) == "closing"
    _report(device, blind, 40)
    assert _direction(entity) == "opening"


def test_a_blind_at_rest_is_not_moving_whatever_its_target():
    # Stopped short of its target (remote, obstacle) or after a restart: a
    # target that differs from the current level is not a movement.
    _, _, entity, _ = _entity(current=60, target=100)
    assert _direction(entity) == ""
    assert entity.current_cover_position == 40


def test_a_report_with_the_same_level_is_not_a_movement():
    blind, device, entity, _ = _entity(current=60, target=100)
    _report(device, blind, 60)
    assert _direction(entity) == ""


# --- back to open/closed ---------------------------------------------------------

def test_without_reports_the_blind_settles_and_its_state_is_written(pushes):
    blind, device, entity, clock = _entity(current=0, target=100)
    _report(device, blind, 30)
    assert _direction(entity) == "closing"
    assert [t.when() for t in clock.pending()] == [SETTLE]

    before = len(pushes)
    clock.advance(SETTLE - 0.01)
    assert _direction(entity) == "closing"
    clock.advance(0.01)
    assert _direction(entity) == ""
    assert len(pushes) - before == 1  # the settled state reaches Home Assistant


def test_every_report_restarts_the_wait():
    blind, device, entity, clock = _entity(current=0, target=100)
    _report(device, blind, 10)
    clock.advance(3)
    _report(device, blind, 20)
    assert [t.when() for t in clock.pending()] == [3 + SETTLE]
    clock.advance(3)  # 6 s after the first report, 3 s after the last
    assert _direction(entity) == "closing"


def test_a_report_on_the_target_ends_the_movement_at_once():
    blind, device, entity, clock = _entity(current=80, target=100)
    _report(device, blind, 90)
    _report(device, blind, 100)
    assert _direction(entity) == ""
    assert entity.is_closed is True
    assert clock.pending() == []


# --- commands --------------------------------------------------------------------

@pytest.mark.parametrize("command, kwargs, expected, level", [
    ("async_close_cover", {}, "closing", 100),
    ("async_open_cover", {}, "opening", 0),
    ("async_set_cover_position", {"position": 10}, "closing", 90),
    ("async_set_cover_position", {"position": 80}, "opening", 20),
])
def test_a_command_shows_its_direction_before_the_first_report(command, kwargs, expected, level):
    blind, device, entity, _ = _entity(current=40, target=40)
    run(getattr(entity, command)(**kwargs))
    assert (_direction(entity), blind.moves) == (expected, [level])


def test_a_command_to_where_the_blind_already_is_does_not_move_it():
    blind, device, entity, _ = _entity(current=100, target=100)
    run(entity.async_close_cover())
    assert _direction(entity) == ""


def test_a_failed_command_shows_no_movement():
    blind, device, entity, _ = _entity(current=40, target=40)

    def refuse(level):
        raise RuntimeError("hub unreachable")
    blind.set_target_level = refuse
    with pytest.raises(RuntimeError):
        run(entity.async_close_cover())
    assert _direction(entity) == ""


def test_the_level_reports_of_a_command_keep_it_moving():
    blind, device, entity, clock = _entity(current=40, target=40)
    run(entity.async_close_cover())
    clock.advance(3)
    _report(device, blind, 50)
    clock.advance(3)
    assert _direction(entity) == "closing"


# --- stop ------------------------------------------------------------------------

def test_stop_ends_the_movement_and_a_coasting_report_does_not_restart_it():
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    assert _direction(entity) == ""
    _report(device, blind, 52)  # the blind coasts a little
    assert (_direction(entity), entity.current_cover_position) == ("", 48)


def test_the_stop_window_is_not_extended_by_reports():
    # End review, finding 1: every report in the window used to restart it,
    # so a remote move that began inside it never got a direction (12
    # reports over 24 s). The window now ends SETTLE_SECONDS after the stop.
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    clock.advance(1)
    _report(device, blind, 52)  # coasting, inside the window
    assert (_direction(entity), device.stop_requested) == ("", True)
    level = 52
    for _ in range(12):  # then the remote, a report every 2 s
        clock.advance(2)
        level += 3  # ends at 88, short of the target (100 ends the move)
        _report(device, blind, level)
    assert (_direction(entity), device.stop_requested) == ("closing", False)


def test_a_remote_move_begun_in_the_window_has_a_direction_from_its_end():
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    clock.advance(2)
    _report(device, blind, 55)
    clock.advance(2)
    _report(device, blind, 60)
    assert _direction(entity) == ""
    clock.advance(2)  # 6 s after the stop: the window has ended
    _report(device, blind, 65)
    assert _direction(entity) == "closing"


def test_after_the_stop_window_a_remote_move_has_a_direction_again():
    # Review finding 2: the stop used to hold until a new target or a Home
    # Assistant command, and a remote move brings neither.
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    clock.advance(SETTLE + 1)
    assert device.stop_requested is False
    for level in (60, 70, 80):
        _report(device, blind, level)
    assert _direction(entity) == "closing"


def test_the_level_during_the_stop_window_is_the_start_of_the_next_move():
    # A report in the window is remembered: the next move is measured from it.
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    _report(device, blind, 58)
    clock.advance(SETTLE + 1)
    _report(device, blind, 55)
    assert _direction(entity) == "opening"


def test_after_a_stop_no_return_to_open_closed_is_left_but_the_window(pushes):
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    assert [t.fn.__name__ for t in clock.pending()] == ["_settled"]
    before = len(pushes)
    clock.advance(SETTLE * 3)
    assert len(pushes) - before == 1  # the end of the window, once


def test_the_stop_window_runs_from_the_stop():
    # Not from the last level report before it.
    blind, device, entity, clock = _entity(current=40, target=100)
    _report(device, blind, 50)
    clock.advance(3)
    run(entity.async_stop_cover())
    clock.advance(SETTLE - 1)  # past the wait of the report, inside the window
    assert device.stop_requested is True
    clock.advance(1)
    assert device.stop_requested is False


def test_a_new_target_from_the_hub_after_a_stop_moves_again():
    # Moved again from the IKEA app: the event carries the new target.
    blind, device, entity, _ = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())
    _report(device, blind, 50, target=0)
    assert (device.stop_requested, _direction(entity)) == (False, "opening")


def test_a_refused_command_after_a_stop_keeps_the_stop():
    # Review finding 4: the stop was cleared before the hub answered.
    blind, device, entity, _ = _entity(current=40, target=100)
    _report(device, blind, 50)
    run(entity.async_stop_cover())

    def refuse(level):
        raise RuntimeError("hub unreachable")
    blind.set_target_level = refuse
    with pytest.raises(RuntimeError):
        run(entity.async_open_cover())

    assert device.stop_requested is True
    _report(device, blind, 52)  # still coasting
    assert _direction(entity) == ""


# --- the poll and removal ----------------------------------------------------------

def test_update_entity_after_a_finished_move_shows_no_movement_and_writes(pushes):
    # End review, finding 5a: homeassistant.update_entity (blinds are no
    # longer polled) fetches the blind. A move that ended meanwhile, with the
    # remote, is not a movement now; the fetched level is where the next one
    # starts. Every entity of the blind is written: Home Assistant writes
    # only the one it updated, which may be the battery sensor.
    blind, device, entity, _ = _entity(current=20, target=20)
    device._json_data = make_blind(current=60, target=20)  # what the fetch brought
    before = len(pushes)
    run(device.async_update())
    assert _direction(entity) == ""
    assert len(pushes) - before == 1
    # Measured from the fetched 60, not the 20 before: down to 50 is opening.
    device._json_data.attributes.blinds_current_level = 50
    device.async_schedule_update_ha_state(False)
    assert _direction(entity) == "opening"


def test_removing_the_entity_cancels_the_wait():
    blind, device, entity, clock = _entity(current=0, target=100)
    _report(device, blind, 30)
    run(entity.async_will_remove_from_hass())
    assert clock.pending() == []


# --- through the real base device and hub event listener ----------------------------

HUB_KEY = "wss://blinds-direction.test"


class _BlindAttrs(BaseModel):
    custom_name: str = "blind"
    blinds_current_level: Optional[int] = None
    blinds_target_level: Optional[int] = None
    blinds_state: Optional[str] = None
    battery_percentage: Optional[int] = None

    def dict(self):  # what induce_properties asks for
        return self.model_dump()


class _Cover:
    """The cover entity as the device fans out to it."""

    def __init__(self):
        self.hass = object()
        self.writes = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1


@pytest.fixture
def real():
    from test_throttle_trailing_flush import _load_base_classes
    bc = _load_base_classes()
    listener_cls = sys.modules["dp_under_test.hub_event_listener"].hub_event_listener
    loop = asyncio.new_event_loop()  # the listener's __init__ asks for one
    asyncio.set_event_loop(loop)
    hass = SimpleNamespace(loop=Clock(), async_create_task=lambda coro: coro.close())
    hub = SimpleNamespace(websocket_base_url=HUB_KEY, get_blinds_by_id=None)
    listener = listener_cls(hub, hass, None)
    model = SimpleNamespace(id="blind_1", relation_id=None, room=None, is_reachable=True,
                            capabilities=None,
                            attributes=_BlindAttrs(blinds_current_level=0, blinds_target_level=0))
    device = bc.ikea_blinds_device(hass, hub, model)
    cover = _Cover()
    device.add_listener(cover)

    def event(**attributes):
        listener._apply_message({"type": "deviceStateChanged", "data": {
            "id": "blind_1", "deviceType": "blinds", "attributes": attributes}})

    yield SimpleNamespace(device=device, cover=cover, event=event, listener=listener,
                          clock=hass.loop)
    listener_cls.unregister_hub(HUB_KEY)
    asyncio.set_event_loop(None)
    loop.close()


def test_a_hub_event_reaches_the_direction_and_the_cover(real):
    real.event(blindsCurrentLevel=20)
    real.event(blindsCurrentLevel=40)
    assert real.device.direction == real.device.CLOSING
    assert real.cover.writes == 2
    real.clock.advance(SETTLE)
    assert real.device.direction is None
    assert real.cover.writes == 3


@pytest.mark.parametrize("level, target", [
    (100, 100),  # closed with a command
    (60, 0),     # moved with the remote: the target stays where it was
])
def test_the_reconnect_resync_replays_a_finished_move_without_a_direction(real, level, target):
    # Review finding 3: the blind moved while the WebSocket was down; the
    # resync replays /devices as events.
    real.listener._resyncing = True
    try:
        real.event(blindsCurrentLevel=level, blindsTargetLevel=target)
    finally:
        real.listener._resyncing = False
    assert real.device.direction is None
    assert real.cover.writes == 1
    # The resynced level is where the next move starts.
    real.event(blindsCurrentLevel=level - 10)
    assert real.device.direction == real.device.OPENING
