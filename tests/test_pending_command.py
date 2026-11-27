"""
The record of what a light was last told (pending_command.py).

It replaces the bulb's one-shot "ignore the next update" flag. That flag was
not tied to its command and never expired; the record holds the values that
were sent, per attribute, and ends by itself: at the hub's confirmation, at a
failed PATCH, or ECHO_WINDOW_S after the hub accepted the command.

pending_command.py has no Home Assistant imports and is loaded directly.
"""
import importlib.util
import os

import pytest

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
_spec = importlib.util.spec_from_file_location("pending_command", os.path.join(COMPONENT, "pending_command.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pc, "_now", lambda: now[0])
    return now


def test_nothing_pending_holds_nothing_back(clock):
    assert pc.PendingCommand().holds_back("light_level", 30) is False


def test_differing_value_is_held_back_while_the_command_is_on_its_way(clock):
    pending = pc.PendingCommand()
    pending.begin({"light_level": 80})
    clock[0] += 60  # no deadline before the hub has answered
    assert pending.holds_back("light_level", 30) is True


def test_differing_value_is_held_back_inside_the_window(clock):
    pending = pc.PendingCommand()
    pending.accepted(pending.begin({"light_level": 80}))
    clock[0] += pc.ECHO_WINDOW_S - 0.01
    assert pending.holds_back("light_level", 30) is True


def test_attribute_that_was_not_sent_is_never_held_back(clock):
    pending = pc.PendingCommand()
    pending.accepted(pending.begin({"light_level": 80}))
    assert pending.holds_back("color_temperature", 2700) is False


def test_matching_value_passes_and_ends_the_wait_for_that_attribute(clock):
    pending = pc.PendingCommand()
    pending.accepted(pending.begin({"is_on": True, "light_level": 80}))
    assert pending.holds_back("light_level", 80) is False
    assert pending.holds_back("light_level", 10) is False, "confirmed, so 10 is a new change"
    assert pending.holds_back("is_on", False) is True, "is_on is not confirmed yet"


def test_after_the_window_everything_passes(clock):
    pending = pc.PendingCommand()
    pending.accepted(pending.begin({"is_on": True, "light_level": 80}))
    clock[0] += pc.ECHO_WINDOW_S
    assert pending.holds_back("light_level", 30) is False
    assert pending.holds_back("is_on", False) is False
    assert not pending


def test_failed_command_leaves_nothing_behind(clock):
    pending = pc.PendingCommand()
    pending.failed(pending.begin({"is_on": True, "light_level": 80}))
    assert not pending
    assert pending.holds_back("light_level", 30) is False


def test_accepted_returns_what_the_hub_has_not_confirmed_yet(clock):
    pending = pc.PendingCommand()
    command = pending.begin({"is_on": True, "light_level": 80})
    pending.holds_back("light_level", 80)  # the echo came before the PATCH returned
    assert pending.accepted(command) == {"is_on": True}


def test_newer_command_takes_over_the_attributes_it_sends(clock):
    pending = pc.PendingCommand()
    first = pending.begin({"is_on": True, "light_level": 50})
    second = pending.begin({"light_level": 80})
    assert pending.accepted(first) == {"is_on": True}, "50 is no longer what the light was told"
    assert pending.holds_back("light_level", 50) is True
    assert pending.accepted(second) == {"light_level": 80}


def test_failure_of_an_older_command_keeps_the_newer_one(clock):
    pending = pc.PendingCommand()
    first = pending.begin({"is_on": True, "light_level": 50})
    pending.begin({"light_level": 80})
    pending.failed(first)
    assert pending.holds_back("light_level", 50) is True
    assert pending.holds_back("is_on", False) is False


def test_int_and_float_of_the_same_value_match(clock):
    pending = pc.PendingCommand()
    pending.accepted(pending.begin({"color_hue": 120.0}))
    assert pending.holds_back("color_hue", 120) is False
    assert not pending


# --- the hub's last value wins when the command is not confirmed ----------------
#
# A differing value inside the window used to be dropped. Dimmed with a remote
# one second after a command from Home Assistant, and with no later event, the
# light kept the value Home Assistant had sent (review of 10-11-2026).

class _Timers:
    """Stands in for loop.call_later."""

    def __init__(self):
        self.armed = []

    def call_later(self, delay, fn, *args):
        handle = type("Handle", (), {"cancelled": False, "delay": delay})()
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        handle.fire = lambda: fn(*args)
        self.armed.append(handle)
        return handle


@pytest.fixture
def timed(clock):
    timers, stood = _Timers(), []
    return pc.PendingCommand(timers.call_later, stood.append), timers, stood


def test_window_is_a_timer_from_the_moment_the_hub_accepted(timed):
    pending, timers, _ = timed
    command = pending.begin({"light_level": 80})
    assert timers.armed == [], "no window before the hub has answered"
    pending.accepted(command)
    assert [t.delay for t in timers.armed] == [pc.ECHO_WINDOW_S]


def test_last_differing_value_stands_when_the_window_ends(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"is_on": True, "light_level": 80}))
    assert pending.holds_back("light_level", 30) is True
    assert pending.holds_back("light_level", 20) is True  # the remote, again
    timers.armed[0].fire()
    assert stood == [{"light_level": 20}], "is_on was never reported, nothing to apply"
    assert not pending


def test_window_without_a_differing_value_ends_silently(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"light_level": 80}))
    timers.armed[0].fire()
    assert stood == []
    assert not pending


def test_confirmation_forgets_what_was_kept_and_cancels_the_timer(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"light_level": 80}))
    pending.holds_back("light_level", 30)  # the stale echo
    assert pending.holds_back("light_level", 80) is False  # the confirmation
    assert timers.armed[0].cancelled, "the record is clear, the timer must go"
    assert stood == []


def test_timer_stays_while_another_attribute_of_the_command_is_pending(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"is_on": True, "light_level": 80}))
    pending.holds_back("light_level", 80)
    assert not timers.armed[0].cancelled
    pending.holds_back("is_on", False)
    timers.armed[0].fire()
    assert stood == [{"is_on": False}]


def test_failed_command_returns_what_the_hub_reported_meanwhile(timed):
    pending, timers, stood = timed
    command = pending.begin({"is_on": True, "light_level": 80})
    pending.holds_back("light_level", 30)
    assert pending.failed(command) == {"light_level": 30}
    assert not pending and stood == []


def test_newer_command_forgets_what_was_kept_for_the_older_one(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"light_level": 50}))
    pending.holds_back("light_level", 30)
    second = pending.begin({"light_level": 80})
    assert timers.armed[0].cancelled, "nothing of the first command is pending"
    pending.accepted(second)
    timers.armed[1].fire()
    assert stood == [], "30 was reported before the second command went out"


def test_window_of_an_older_command_leaves_the_newer_one_alone(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"is_on": True, "light_level": 50}))
    pending.begin({"light_level": 80})
    pending.holds_back("light_level", 50)
    timers.armed[0].fire()  # the first window: is_on only
    assert stood == []
    assert pending.holds_back("light_level", 30) is True


def test_clear_cancels_every_timer(timed):
    pending, timers, stood = timed
    pending.accepted(pending.begin({"light_level": 80}))
    pending.holds_back("light_level", 30)
    pending.clear()
    assert timers.armed[0].cancelled and not pending and stood == []
