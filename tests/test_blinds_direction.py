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
first level report. When no level report has arrived for a short while
(SETTLE_SECONDS), the blind is open or closed again.

The classes are loaded as in test_blinds_stop.py: the real bodies, without
Home Assistant.
"""
import asyncio

import pytest

import test_blinds_stop as blinds
from test_blinds_stop import FakeClient, make_blind, make_entity

# Short enough for a fast suite, long enough that two reports in a row fall
# inside one window on a loaded test runner.
SETTLE = 0.2


def _entity(**kwargs):
    blind = make_blind(**kwargs)
    device, entity = make_entity(blind)
    device.SETTLE_SECONDS = SETTLE
    return blind, device, entity


def _report(device, blind, level):
    """A hub event with a new current level, as hub_event_listener applies it."""
    blind.attributes.blinds_current_level = level
    device.async_schedule_update_ha_state(False)


def _direction(entity):
    return ("opening" if entity.is_opening else "") + ("closing" if entity.is_closing else "")


@pytest.fixture
def pushes(monkeypatch):
    """Counts the state writes the device hands to its entities."""
    count = []
    monkeypatch.setattr(blinds._StubDevice, "async_schedule_update_ha_state",
                        lambda self, force_refresh=False: count.append(force_refresh))
    return count


# --- from the level --------------------------------------------------------------

@pytest.mark.parametrize("levels, expected", [
    ((20, 40), "closing"),   # 0 = open, 100 = closed: rising is closing
    ((80, 60), "opening"),
])
def test_direction_follows_the_current_level(levels, expected):
    async def go():
        blind, device, entity = _entity(current=levels[0] - (levels[1] - levels[0]), target=50)
        for level in levels:
            _report(device, blind, level)
        return _direction(entity)

    assert asyncio.run(go()) == expected


def test_remote_move_against_a_stale_target_shows_the_real_direction():
    # The last command opened the blind (target 0). Now the remote sends it
    # down: the hub reports only the current level, the target stays 0.
    async def go():
        blind, device, entity = _entity(current=0, target=0)
        for level in (10, 25, 40):
            _report(device, blind, level)
        return _direction(entity)

    assert asyncio.run(go()) == "closing"


def test_move_to_a_middle_position_has_a_direction():
    # The old rule only knew targets 0 and 100.
    async def go():
        blind, device, entity = _entity(current=100, target=100)
        await entity.async_set_cover_position(position=50)  # hub level 50
        return _direction(entity), blind.moves

    assert asyncio.run(go()) == ("opening", [50])


def test_a_blind_at_rest_is_not_moving_whatever_its_target():
    # Stopped short of its target (remote, obstacle) or after a restart: a
    # target that differs from the current level is not a movement.
    _, _, entity = _entity(current=60, target=100)
    assert _direction(entity) == ""
    assert entity.current_cover_position == 40


def test_a_report_with_the_same_level_is_not_a_movement():
    async def go():
        blind, device, entity = _entity(current=60, target=100)
        _report(device, blind, 60)
        return _direction(entity)

    assert asyncio.run(go()) == ""


# --- back to open/closed ---------------------------------------------------------

def test_without_reports_the_blind_settles_and_its_state_is_written(pushes):
    async def go():
        blind, device, entity = _entity(current=0, target=100)
        _report(device, blind, 30)
        moving = _direction(entity)
        before = len(pushes)
        await asyncio.sleep(SETTLE * 2)
        return moving, _direction(entity), len(pushes) - before

    moving, after, written = asyncio.run(go())
    assert moving == "closing"
    assert after == ""
    assert written == 1  # the settled state reaches Home Assistant


def test_every_report_restarts_the_wait():
    async def go():
        blind, device, entity = _entity(current=0, target=100)
        _report(device, blind, 10)
        await asyncio.sleep(SETTLE * 0.6)
        _report(device, blind, 20)
        await asyncio.sleep(SETTLE * 0.6)  # 1.2 x SETTLE after the first report
        return _direction(entity)

    assert asyncio.run(go()) == "closing"


def test_closed_after_settling_at_the_bottom():
    async def go():
        blind, device, entity = _entity(current=80, target=100)
        _report(device, blind, 100)
        await asyncio.sleep(SETTLE * 2)
        return _direction(entity), entity.is_closed

    assert asyncio.run(go()) == ("", True)


# --- commands --------------------------------------------------------------------

@pytest.mark.parametrize("command, kwargs, expected", [
    ("async_close_cover", {}, "closing"),
    ("async_open_cover", {}, "opening"),
    ("async_set_cover_position", {"position": 10}, "closing"),  # hub level 90
])
def test_a_command_shows_its_direction_before_the_first_report(command, kwargs, expected):
    async def go():
        blind, device, entity = _entity(current=40, target=40)
        await getattr(entity, command)(**kwargs)
        return _direction(entity)

    assert asyncio.run(go()) == expected


def test_a_command_to_where_the_blind_already_is_does_not_move_it():
    async def go():
        blind, device, entity = _entity(current=100, target=100)
        await entity.async_close_cover()
        return _direction(entity)

    assert asyncio.run(go()) == ""


def test_a_failed_command_shows_no_movement():
    async def go():
        blind, device, entity = _entity(current=40, target=40)

        def refuse(level):
            raise RuntimeError("hub unreachable")
        blind.set_target_level = refuse
        with pytest.raises(RuntimeError):
            await entity.async_close_cover()
        return _direction(entity)

    assert asyncio.run(go()) == ""


def test_the_level_reports_of_a_command_keep_it_moving():
    async def go():
        blind, device, entity = _entity(current=40, target=40)
        await entity.async_close_cover()
        await asyncio.sleep(SETTLE * 0.6)
        _report(device, blind, 50)
        await asyncio.sleep(SETTLE * 0.6)
        return _direction(entity)

    assert asyncio.run(go()) == "closing"


# --- stop ------------------------------------------------------------------------

def test_stop_ends_the_movement_and_a_coasting_report_does_not_restart_it():
    async def go():
        blind, device, entity = _entity(current=40, target=100)
        _report(device, blind, 50)
        await entity.async_stop_cover()
        stopped = _direction(entity)
        _report(device, blind, 52)  # the blind coasts a little
        return stopped, _direction(entity), entity.current_cover_position

    assert asyncio.run(go()) == ("", "", 48)


def test_a_new_target_from_the_hub_after_a_stop_moves_again():
    # Moved again from the IKEA app: the event carries the new target.
    async def go():
        blind, device, entity = _entity(current=40, target=100)
        _report(device, blind, 50)
        await entity.async_stop_cover()
        blind.attributes.blinds_target_level = 0
        device.async_schedule_update_ha_state(False)
        return device.stop_requested, _direction(entity)

    assert asyncio.run(go()) == (False, "opening")


# --- the poll and removal ----------------------------------------------------------

def test_a_level_change_seen_by_the_poll_is_a_movement():
    async def go():
        blind, device, entity = _entity(current=20, target=20)
        device._json_data = make_blind(current=35, target=100)  # what the poll fetched
        await device.async_update()
        return _direction(entity)

    assert asyncio.run(go()) == "closing"


def test_removing_the_entity_cancels_the_wait(pushes):
    async def go():
        blind, device, entity = _entity(current=0, target=100)
        _report(device, blind, 30)
        await entity.async_will_remove_from_hass()
        before = len(pushes)
        await asyncio.sleep(SETTLE * 2)
        return len(pushes) - before

    assert asyncio.run(go()) == 0
