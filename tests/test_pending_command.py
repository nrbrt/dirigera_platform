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
