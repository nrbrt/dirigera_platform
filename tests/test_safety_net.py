"""
#207: the safety net, one GET /devices for all devices every few minutes.

Polling per entity is off for every device whose values arrive over the
WebSocket. What still could go missing (an event the hub never sent, or one
lost without the WebSocket dropping, which the reconnect resync covers) is
caught by a periodic resync: one /devices fetch, applied through the same
path as the reconnect resync, so a blind that moved meanwhile shows no
direction (state_resynced).

What is guarded here:

1. A due safety net runs the resync, and a missed state comes back.
2. It never runs next to a resync that is still going (a reconnect, or the
   previous one on a slow hub), and the next one is planned either way.
3. A failing fetch is logged and the next interval tries again.
4. Stopping it, and stopping the listener, leaves no timer that fires.

The listener is loaded as in test_reconnect_resync.py. Time is a fake clock.
"""
import asyncio
import logging

import pytest

from test_reconnect_resync import FakeHub, hel, hub_event_listener


class _Timer:
    def __init__(self, when, fn, args):
        self._when, self.fn, self.args = when, fn, args
        self.cancelled = False

    def when(self):
        return self._when

    def cancel(self):
        self.cancelled = True


class Clock:
    """The listener's loop as far as timers go; tasks run on a real loop."""

    def __init__(self, loop):
        self.real = loop
        self.now = 0.0
        self.timers = []

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


class Hass:
    def __init__(self):
        self.tasks = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class FailingHub(FakeHub):
    def __init__(self):
        super().__init__([])
        self.fail = True

    def get(self, path):
        self.get_calls.append(path)
        if self.fail:
            raise ConnectionError("hub does not answer")
        return [{"id": "plug-a_1", "deviceType": "outlet", "attributes": {"isOn": True}}]


INTERVAL = hel.SAFETY_NET_INTERVAL


@pytest.fixture
def net():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = Hass()

    def make(hub):
        listener = hub_event_listener(hub, hass)
        clock = Clock(loop)
        listener._loop = clock
        applied = []
        listener._apply_message = lambda msg: applied.append((msg, listener._resyncing))
        return listener, clock, applied

    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    yield make, (lambda: loop.run_until_complete(settle())), hass
    for task in hass.tasks:
        task.cancel()
    asyncio.set_event_loop(None)
    loop.close()


def test_the_interval_is_minutes_not_seconds():
    # One GET /devices for the whole hub; the reason is next to the constant.
    assert 60 <= INTERVAL <= 15 * 60


def test_a_due_safety_net_brings_back_a_missed_state(net):
    make, settle, _ = net
    row = {"id": "plug-a_1", "deviceType": "outlet", "attributes": {"isOn": True}}
    listener, clock, applied = make(FakeHub([row]))
    listener.start_safety_net()

    clock.advance(INTERVAL - 1)
    settle()
    assert listener._hub.get_calls == []

    clock.advance(1)
    settle()
    assert listener._hub.get_calls == ["/devices"]
    # Through the resync path: as an event, with _resyncing set (blinds then
    # take the levels as a start, not as a movement).
    assert applied == [({"type": "deviceStateChanged", "data": row}, True)]
    # And the next one is planned.
    assert [t.when() for t in clock.pending()] == [2 * INTERVAL]


def test_no_safety_net_next_to_a_resync_that_is_still_going(net):
    make, settle, _ = net
    listener, clock, _ = make(FakeHub([]))
    listener.start_safety_net()

    async def hold():
        await listener._resync_lock.acquire()
    asyncio.get_event_loop().run_until_complete(hold())
    try:
        clock.advance(INTERVAL)
        settle()
    finally:
        listener._resync_lock.release()
    assert listener._hub.get_calls == []
    assert [t.when() for t in clock.pending()] == [2 * INTERVAL]

    clock.advance(INTERVAL)
    settle()
    assert listener._hub.get_calls == ["/devices"]


def test_a_failing_fetch_is_logged_and_the_next_interval_tries_again(net, caplog):
    make, settle, _ = net
    hub = FailingHub()
    listener, clock, applied = make(hub)
    listener.start_safety_net()

    with caplog.at_level(logging.WARNING, logger="custom_components.dirigera_platform"):
        clock.advance(INTERVAL)
        settle()
    assert hub.get_calls == ["/devices"]
    assert applied == []
    assert "hub does not answer" in caplog.text

    hub.fail = False
    clock.advance(INTERVAL)
    settle()
    assert hub.get_calls == ["/devices", "/devices"]
    assert len(applied) == 1


def test_stopping_the_safety_net_leaves_no_timer(net):
    make, settle, _ = net
    listener, clock, _ = make(FakeHub([]))
    listener.start_safety_net()
    listener.stop_safety_net()
    assert clock.pending() == []
    clock.advance(INTERVAL * 3)
    settle()
    assert listener._hub.get_calls == []


def test_a_stopped_listener_plans_no_further_safety_net(net):
    # stop() runs in the executor; a timer that fires after it does nothing
    # and plans nothing.
    make, settle, _ = net
    listener, clock, _ = make(FakeHub([]))
    listener.start_safety_net()
    listener._request_to_stop = True
    clock.advance(INTERVAL)
    settle()
    assert listener._hub.get_calls == []
    assert clock.pending() == []
