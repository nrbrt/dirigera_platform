"""
#207: the safety net, one GET /devices for all devices every few minutes.

Polling per entity is off for every device whose values arrive over the
WebSocket. What still could go missing (an event the hub never sent, or one
lost without the WebSocket dropping, which the reconnect resync covers) is
caught by a periodic resync: one /devices fetch, applied through the same
path as the reconnect resync, so a blind that moved meanwhile shows no
direction (state_resynced).

Setup schedules a run with Home Assistant's async_track_time_interval and
unload cancels it; that, and what a run restores in real entities, is tested
in tests_ha/test_safety_net.py. What is guarded here, on the listener:

1. A run fetches /devices once and replays it through the resync path.
2. It never runs next to a resync that is still going (a reconnect, or the
   previous run on a slow hub).
3. A failing fetch is logged and the next run tries again.
4. A run after stop() does nothing.
5. A run logs at debug: an info line every five minutes is noise.

The listener is loaded as in test_reconnect_resync.py.
"""
import asyncio
import logging

import pytest

from test_reconnect_resync import FakeHub, hel, hub_event_listener


class Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FailingHub(FakeHub):
    def __init__(self):
        super().__init__([])
        self.fail = True

    def get(self, path):
        self.get_calls.append(path)
        if self.fail:
            raise ConnectionError("hub does not answer")
        return [{"id": "plug-a_1", "deviceType": "outlet", "attributes": {"isOn": True}}]


ROW = {"id": "plug-a_1", "deviceType": "outlet", "attributes": {"isOn": True}}


@pytest.fixture
def make():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def make(hub):
        listener = hub_event_listener(hub, Hass())
        applied = []
        listener._apply_message = lambda msg: applied.append((msg, listener._resyncing))
        return listener, applied

    yield make, loop.run_until_complete
    asyncio.set_event_loop(None)
    loop.close()


def test_the_interval_is_minutes_not_seconds():
    # One GET /devices for the whole hub; the reason is next to the constant.
    assert 60 <= hel.SAFETY_NET_INTERVAL <= 15 * 60


def test_a_run_replays_devices_through_the_resync_path(make):
    make, run = make
    listener, applied = make(FakeHub([ROW]))
    run(listener.run_safety_net())
    assert listener._hub.get_calls == ["/devices"]
    # As an event, with _resyncing set (blinds take the levels as a start).
    assert applied == [({"type": "deviceStateChanged", "data": ROW}, True)]


def test_no_run_next_to_a_resync_that_is_still_going(make):
    make, run = make
    listener, _ = make(FakeHub([ROW]))

    async def during_a_resync():
        async with listener._resync_lock:
            await listener.run_safety_net()
    run(during_a_resync())
    assert listener._hub.get_calls == []

    run(listener.run_safety_net())
    assert listener._hub.get_calls == ["/devices"]


def test_a_failing_fetch_is_logged_and_the_next_run_tries_again(make, caplog):
    make, run = make
    hub = FailingHub()
    listener, applied = make(hub)

    with caplog.at_level(logging.WARNING, logger="custom_components.dirigera_platform"):
        run(listener.run_safety_net())
    assert applied == []
    assert "hub does not answer" in caplog.text

    hub.fail = False
    run(listener.run_safety_net())
    assert hub.get_calls == ["/devices", "/devices"]
    assert len(applied) == 1


def test_a_run_after_stop_does_nothing(make):
    make, run = make
    listener, applied = make(FakeHub([ROW]))
    listener._request_to_stop = True
    run(listener.run_safety_net())
    assert listener._hub.get_calls == []


def test_a_run_logs_at_debug_and_a_reconnect_at_info(make, caplog):
    make, run = make
    listener, _ = make(FakeHub([ROW]))
    with caplog.at_level(logging.DEBUG, logger="custom_components.dirigera_platform"):
        run(listener.run_safety_net())
        run(listener._resync_all_states())
    applied = [(r.levelname, r.getMessage()) for r in caplog.records if "applied for" in r.getMessage()]
    assert applied == [("DEBUG", "State resync (safety net) applied for 1 devices"),
                       ("INFO", "State resync (reconnect) applied for 1 devices")]


def test_a_cancelled_run_leaves_no_resync_state_behind(make):
    # Unload cancels the run as a background task of the entry; CancelledError
    # is no Exception, so the old reset in "except Exception" missed it.
    make, run = make
    listener, applied = make(FakeHub([ROW]))

    async def cancelled(func, *args):
        raise asyncio.CancelledError
    listener._hass.async_add_executor_job = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(listener.run_safety_net())
    assert listener._membership_live is None
    assert listener._resyncing is False
    assert not listener._resync_lock.locked()
