"""
One write path per hub: a worker task that sends one command at a time.

The queue of #71 was an asyncio.Lock held by the caller. Three things followed
from that (review of 10-11-2026):

* a cancelled caller (a script in mode: restart) released the lock while its
  request was still running in the executor, so the next command overlapped it;
* with the hub gone every queued command waited for its own 10 s timeout, one
  after another;
* only lights used it. Outlets, blinds, the air purifier and scenes called the
  library straight from the executor.

Now a hub has one worker, owned by the config entry, and every command of every
device kind goes through it.

hub_writer.py has no Home Assistant imports and is loaded directly; the device
classes are lifted out of their modules with ast, as in the other tests.
"""
import ast
import asyncio
import enum
import importlib.util
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest
import requests

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)


def _module(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(COMPONENT, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub_writer = _module("hub_writer")
pending_command = _module("pending_command")
HubWriter = hub_writer.HubWriter


class _Hass:
    """Runs jobs in threads, as Home Assistant's executor does."""

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class _Hub:
    """Records what reaches the hub, and how many requests ran at once."""

    def __init__(self, url="wss://hub-1", takes=0.01):
        self.websocket_base_url = url
        self.takes = takes
        self.log = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate = None
        self._lock = threading.Lock()

    def request(self, what):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.log.append(("start", what, time.monotonic()))
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.takes)
        with self._lock:
            self.in_flight -= 1
            self.log.append(("end", what, time.monotonic()))
        return what

    def patch(self, route, data):
        return self.request(("PATCH", route, data))

    def post(self, route, data=None):
        return self.request(("POST", route))

    def sent(self):
        return [what for kind, what, _ in self.log if kind == "start"]


@pytest.fixture(autouse=True)
def no_writers_left(monkeypatch):
    monkeypatch.setattr(hub_writer, "_writers", {})


def _writer(hub, min_spacing=0.0):
    writer = HubWriter(_Hass(), hub, min_spacing=min_spacing)
    writer.start()
    return writer


# --- one at a time --------------------------------------------------------------

def test_runs_one_command_at_a_time_and_spaces_the_starts():
    spacing = 0.05
    hub = _Hub()

    async def main():
        writer = _writer(hub, spacing)
        try:
            return await asyncio.gather(*(writer.run(hub.request, n) for n in range(5)))
        finally:
            writer.stop()

    assert asyncio.run(main()) == list(range(5))
    assert [kind for kind, _, _ in hub.log] == ["start", "end"] * 5, "commands overlapped"
    starts = [t for kind, _, t in hub.log if kind == "start"]
    assert all(b - a >= spacing * 0.95 for a, b in zip(starts, starts[1:]))


def test_cancelled_caller_does_not_let_the_next_command_overlap():
    hub = _Hub(takes=0.1)

    async def main():
        writer = _writer(hub)
        first = asyncio.ensure_future(writer.run(hub.request, "first"))
        second = asyncio.ensure_future(writer.run(hub.request, "second"))
        await asyncio.sleep(0.03)  # "first" is with the hub now
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "second"
        writer.stop()

    asyncio.run(main())
    assert hub.max_in_flight == 1
    # The work in flight was not cancelled, and the next one waited for it.
    assert [(kind, what) for kind, what, _ in hub.log] == [
        ("start", "first"), ("end", "first"), ("start", "second"), ("end", "second")]


def test_command_cancelled_while_it_waits_is_not_sent():
    hub = _Hub(takes=0.05)

    async def main():
        writer = _writer(hub)
        first = asyncio.ensure_future(writer.run(hub.request, "first"))
        second = asyncio.ensure_future(writer.run(hub.request, "second"))
        third = asyncio.ensure_future(writer.run(hub.request, "third"))
        await asyncio.sleep(0.01)
        second.cancel()
        await asyncio.gather(first, third)
        writer.stop()

    asyncio.run(main())
    assert hub.sent() == ["first", "third"]


# --- failures -------------------------------------------------------------------

def test_exception_reaches_its_caller_and_the_worker_goes_on():
    hub = _Hub()

    def refused():
        raise requests.HTTPError("400 Bad Request")

    async def main():
        writer = _writer(hub)
        try:
            failing = asyncio.ensure_future(writer.run(refused))
            queued = asyncio.ensure_future(writer.run(hub.request, "queued behind it"))
            with pytest.raises(requests.HTTPError):
                await failing
            # A refusal says nothing about the hub being there: the rest is sent.
            assert await queued == "queued behind it"
            return await writer.run(hub.request, "next")
        finally:
            writer.stop()

    assert asyncio.run(main()) == "next"


@pytest.mark.parametrize("error", [requests.ConnectTimeout, requests.ConnectionError])
def test_dead_hub_fails_the_rest_of_the_queue_at_once(error):
    timeout = 0.2
    hub = _Hub()
    attempts = []

    def unreachable():
        attempts.append(time.monotonic())
        time.sleep(timeout)
        raise error("no answer")

    async def main():
        writer = _writer(hub)
        try:
            started = time.monotonic()
            results = await asyncio.gather(
                *(writer.run(unreachable) for _ in range(10)), return_exceptions=True)
            elapsed = time.monotonic() - started
            # The hub is tried again for a command that comes after.
            after = await writer.run(hub.request, "back")
            return results, elapsed, after
        finally:
            writer.stop()

    results, elapsed, after = asyncio.run(main())
    assert isinstance(results[0], error), "the one that was sent gets the library's own error"
    assert all(isinstance(r, hub_writer.HubUnreachable) for r in results[1:]), results
    assert len(attempts) == 1, "every queued command waited for its own timeout"
    assert elapsed < 2 * timeout, f"took {elapsed:.2f} s, 10 timeouts would be {10 * timeout} s"
    assert after == "back"


@pytest.mark.parametrize("error", [requests.ReadTimeout, requests.Timeout, requests.HTTPError])
def test_one_slow_or_refused_answer_does_not_fail_the_queue(error):
    # A ReadTimeout is a hub that was reached and took too long over this one
    # request (and may have carried it out). The next command is for the hub
    # to answer, not to be thrown away with the nine behind it.
    hub = _Hub()

    def slow():
        raise error("no answer in time")

    async def main():
        writer = _writer(hub)
        try:
            return await asyncio.gather(
                writer.run(slow), *(writer.run(hub.request, n) for n in range(3)),
                return_exceptions=True)
        finally:
            writer.stop()

    results = asyncio.run(main())
    assert isinstance(results[0], error)
    assert results[1:] == [0, 1, 2], "the queue behind one slow answer was failed"
    assert hub.sent() == [0, 1, 2]


# --- the worker lives as long as the config entry --------------------------------

def test_stop_fails_what_is_queued_and_ends_the_worker():
    hub = _Hub()
    hub.gate = threading.Event()

    async def main():
        writer = _writer(hub)
        calls = [asyncio.ensure_future(writer.run(hub.request, n)) for n in range(3)]
        await asyncio.sleep(0.03)  # 0 is with the hub, 1 and 2 wait
        writer.stop()
        results = await asyncio.gather(*calls, return_exceptions=True)
        await asyncio.sleep(0)
        assert writer._task.done(), "the worker outlived the config entry"
        with pytest.raises(hub_writer.HubWriterStopped):
            await writer.run(hub.request, "late")
        with pytest.raises(hub_writer.HubWriterStopped):
            hub_writer.writer_for(hub)
        hub.gate.set()
        await asyncio.sleep(0.05)
        return results

    results = asyncio.run(main())
    assert all(isinstance(r, hub_writer.HubWriterStopped) for r in results), results
    assert hub.sent() == [0], "a queued command was still sent after the unload"


def test_worker_cancelled_by_its_owner_fails_what_is_queued():
    # Home Assistant cancels an entry's background tasks itself on unload.
    hub = _Hub()
    hub.gate = threading.Event()

    async def main():
        writer = HubWriter(_Hass(), hub, min_spacing=0)
        tasks = []
        writer.start(lambda work: tasks.append(asyncio.ensure_future(work)) or tasks[-1])
        calls = [asyncio.ensure_future(writer.run(hub.request, n)) for n in range(3)]
        await asyncio.sleep(0.03)
        tasks[0].cancel()
        results = await asyncio.gather(*calls, return_exceptions=True)
        hub.gate.set()
        await asyncio.sleep(0.05)
        return results

    results = asyncio.run(main())
    assert all(isinstance(r, hub_writer.HubWriterStopped) for r in results), results
    assert hub.sent() == [0]


def test_writer_stopped_before_it_ran_fails_what_is_queued():
    hub = _Hub()

    async def main():
        writer = _writer(hub)  # the task has not had a turn yet
        call = asyncio.ensure_future(writer.run(hub.request, "x"))
        writer.stop()
        with pytest.raises(hub_writer.HubWriterStopped):
            await call

    asyncio.run(main())
    assert hub.sent() == []


def test_one_writer_per_hub():
    async def main():
        a, b = _Hub("wss://a"), _Hub("wss://b")
        writer_a, writer_b = _writer(a), _writer(b)
        # Entities hold their own hub object for the same hub.
        assert hub_writer.writer_for(SimpleNamespace(websocket_base_url="wss://a")) is writer_a
        assert hub_writer.writer_for(b) is writer_b
        writer_a.stop()
        writer_b.stop()

    asyncio.run(main())


def test_second_entry_for_the_same_hub_keeps_a_writer_when_one_is_unloaded():
    # Entries that were added twice for one hub before the duplicate guard (#39).
    async def main():
        hub = _Hub()
        first, second = _writer(hub), _writer(hub)
        assert hub_writer.writer_for(hub) is second
        second.stop()
        assert hub_writer.writer_for(hub) is first
        first.stop()

    asyncio.run(main())


def test_no_writer_for_a_hub_that_is_not_set_up():
    with pytest.raises(hub_writer.HubWriterStopped):
        hub_writer.writer_for(_Hub("wss://nobody"))


# --- every device kind writes through it -----------------------------------------

class _NoExecutor:
    """The hass the devices get: a write that does not go through the hub's
    writer would run here."""

    async def async_add_executor_job(self, fn, *args):
        raise AssertionError(f"{getattr(fn, '__name__', fn)} went past the hub writer")

    @property
    def loop(self):
        # A blind starts its wait for the next level report on it (#207).
        return asyncio.get_running_loop()


class _StubDevice:
    def __init__(self, hass, hub, json_data, get_by_id_fx):
        self._hass = hass
        self._hub = hub
        self._json_data = json_data

    name = "device"
    preset_mode = None
    percentage = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        pass


class FanModeEnum(enum.Enum):
    OFF = "off"
    ON = "on"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    AUTO = "auto"


def _lift(filename, classes, namespace):
    path = os.path.join(COMPONENT, filename)
    with open(path) as fh:
        tree = ast.parse(fh.read())
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in classes]
    assert {n.name for n in nodes} == set(classes)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), path, "exec"), namespace)
    return [namespace[name] for name in classes]


def _load_devices():
    import math
    namespace = {
        "core": SimpleNamespace(HomeAssistant=object), "Hub": object, "Blind": object,
        "BlindsStateEnum": SimpleNamespace(STOPPED=SimpleNamespace(value="stopped")),
        "CoverDeviceClass": SimpleNamespace(BLIND="blind"),
        "FanEntityFeature": SimpleNamespace(PRESET_MODE=1, SET_SPEED=2, TURN_OFF=4, TURN_ON=8),
        "FanModeEnum": FanModeEnum,
        "HomeAssistantError": Exception, "DOMAIN": "dirigera_platform",
        "ikea_base_device": _StubDevice, "math": math,
        "logger": logging.getLogger(__name__),
        "writer_for": hub_writer.writer_for,
        # Noting what a command wrote for a fetch on its way is tested in
        # test_command_writes_state.py; nothing is fetching here.
        "attribute_values": lambda entity: {},
        "note_command_writes": lambda entity, before: None,
    }
    devices = _lift("base_classes.py",
                    ["ikea_outlet_device", "ikea_blinds_device", "ikea_starkvind_air_purifier_device"],
                    namespace)
    # The real command path of the base device, on the stub.
    with open(os.path.join(COMPONENT, "base_classes.py")) as fh:
        base = next(n for n in ast.parse(fh.read()).body
                    if isinstance(n, ast.ClassDef) and n.name == "ikea_base_device")
    command = next(n for n in base.body if getattr(n, "name", None) == "_command")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[command], type_ignores=[])),
                 "base_classes.py", "exec"), namespace)
    _StubDevice._command = namespace["_command"]
    return devices


def _load_scene():
    return _lift("scene.py", ["ikea_scene"], {
        "Scene": object, "HubX": object, "HackScene": object, "Any": object,
        "HomeAssistantError": Exception, "logger": logging.getLogger(__name__),
        "ikea_to_hass_icon": lambda icon: icon,
        "writer_for": hub_writer.writer_for,
    })[0]


def _load_bulb_send():
    path = os.path.join(COMPONENT, "light.py")
    with open(path) as fh:
        tree = ast.parse(fh.read())
    bulb = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ikea_bulb")
    methods = [n for n in bulb.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name in ("_send", "async_turn_off")]
    namespace = {"HomeAssistantError": Exception, "DOMAIN": "dirigera_platform",
                 "logger": logging.getLogger(__name__), "writer_for": hub_writer.writer_for,
                 # _send also writes the device set that follows the light (merge with #76)
                 "hub_event_listener": SimpleNamespace(get_registry_entry=lambda hub_key, light_id: None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), path, "exec"), namespace)
    return namespace


def _model(hub, device_id):
    """A library model: its setters PATCH through the hub client, blocking."""
    def setter(attribute):
        return lambda value: hub.patch(
            f"/devices/{device_id}", [{"attributes": {attribute: getattr(value, "value", value)}}])
    return SimpleNamespace(
        id=device_id,
        capabilities=SimpleNamespace(can_receive=["blindsState", "blindsTargetLevel", "isOn"]),
        attributes=SimpleNamespace(blinds_target_level=0, blinds_state="up"),
        set_on=setter("isOn"), set_target_level=setter("blindsTargetLevel"),
        set_state=setter("blindsState"), set_motor_state=setter("motorState"),
        set_fan_mode=setter("fanMode"), set_child_lock=setter("childLock"),
        set_status_light=setter("statusLight"),
    )


def test_every_device_kind_writes_through_the_one_worker_of_its_hub():
    Outlet, Blinds, Purifier = _load_devices()
    Scene = _load_scene()
    light = _load_bulb_send()
    hub = _Hub()
    hass = _NoExecutor()
    finder = SimpleNamespace(websocket_base_url=hub.websocket_base_url,
                             get_outlet_by_id=None, get_blinds_by_id=None, get_air_purifier_by_id=None)

    outlet = Outlet(hass, finder, _model(hub, "outlet-1"))
    blind = Blinds(hass, finder, _model(hub, "blind-1"))
    purifier = Purifier(hass, finder, _model(hub, "purifier-1"))
    scene = Scene(finder, SimpleNamespace(id="scene-1", name="Evening", icon=None,
                                          trigger=lambda: hub.post("/scenes/scene-1/trigger")))
    scene.hass = hass
    bulb = SimpleNamespace(
        _hub=finder, hass=hass, name="bulb-1", _pending=pending_command.PendingCommand(),
        _command_seq=0, _level_not_before=0.0,
        _json_data=SimpleNamespace(id="bulb-1", attributes=SimpleNamespace(is_on=True)),
        async_schedule_update_ha_state=lambda force: None,
    )
    bulb._send = lambda data, updates: light["_send"](bulb, data, updates)

    commands = [
        lambda: light["async_turn_off"](bulb),
        lambda: outlet.async_turn_on(),
        lambda: blind.async_set_cover_position(30),
        lambda: scene.async_activate(),
        lambda: purifier.async_set_percentage(50),
        lambda: outlet.async_turn_off(),
        lambda: blind.async_open_cover(),
        lambda: blind.async_close_cover(),
        lambda: blind.async_stop_cover(),
        lambda: purifier.async_set_preset_mode("auto"),
        lambda: purifier.async_set_fan_mode(FanModeEnum.LOW),
        lambda: purifier.async_set_child_lock(True),
        lambda: purifier.async_set_status_light(False),
    ]

    async def main():
        writer = _writer(hub)
        try:
            # All at once, as Home Assistant does for an area or a scene.
            await asyncio.gather(*(command() for command in commands))
        finally:
            writer.stop()

    asyncio.run(main())

    def patch(device, **attributes):
        return ("PATCH", f"/devices/{device}", [{"attributes": attributes}])

    assert hub.max_in_flight == 1
    # In the order they were asked for, whatever the kind of device.
    assert hub.sent() == [
        patch("bulb-1", isOn=False),
        patch("outlet-1", isOn=True),
        patch("blind-1", blindsTargetLevel=70),
        ("POST", "/scenes/scene-1/trigger"),
        patch("purifier-1", motorState=25),
        patch("outlet-1", isOn=False),
        patch("blind-1", blindsTargetLevel=0),
        patch("blind-1", blindsTargetLevel=100),
        patch("blind-1", blindsState="stopped"),
        patch("purifier-1", fanMode="auto"),
        patch("purifier-1", fanMode="low"),
        patch("purifier-1", childLock=True),
        patch("purifier-1", statusLight=False),
    ]


def test_started_is_called_when_the_command_goes_out_not_when_it_is_asked():
    hub = _Hub()
    hub.gate = threading.Event()
    order = []

    async def main():
        writer = _writer(hub)
        try:
            first = asyncio.ensure_future(writer.run(hub.request, "a", started=lambda: order.append("a out")))
            second = asyncio.ensure_future(writer.run(hub.request, "b", started=lambda: order.append("b out")))
            never = asyncio.ensure_future(writer.run(hub.request, "c", started=lambda: order.append("c out")))
            await asyncio.sleep(0.05)
            order.append("a with the hub")
            never.cancel()
            hub.gate.set()
            await asyncio.gather(first, second)
        finally:
            writer.stop()

    asyncio.run(main())
    assert order == ["a out", "a with the hub", "b out"]


def test_light_is_patched_by_device_id_on_the_writers_own_hub():
    hub = _Hub()

    async def main():
        writer = _writer(hub)
        try:
            await writer.patch_device("bulb-9", [{"attributes": {"isOn": True}}])
        finally:
            writer.stop()

    asyncio.run(main())
    assert hub.sent() == [("PATCH", "/devices/bulb-9", [{"attributes": {"isOn": True}}])]


# --- the worker lives as long as the config entry -------------------------------
#
# Home Assistant cancels every background task before it fires
# EVENT_HOMEASSISTANT_STOP (core.py async_stop, "Cancel all background tasks"),
# and a config entry's async_create_background_task is such a task. The writer
# was one, so an automation that runs at shutdown ("lights off when Home
# Assistant stops") found the hub's writer gone.

class _HomeAssistant:
    """The task bookkeeping of homeassistant.core that matters here."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self._background_tasks = set()

    async def async_add_executor_job(self, fn, *args):
        return await self.loop.run_in_executor(None, fn, *args)

    def async_create_background_task(self, target, name, eager_start=True):
        task = self.loop.create_task(target, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.remove)
        return task

    async def stop_until_the_stop_event(self):
        """async_stop up to where EVENT_HOMEASSISTANT_STOP is fired."""
        for task in list(self._background_tasks):
            task.cancel("Home Assistant is stopping")
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class _Entry:
    def __init__(self, hass):
        self._hass = hass
        self._on_unload = []

    def async_create_background_task(self, hass, target, name, eager_start=True):
        return hass.async_create_background_task(target, name, eager_start)

    def async_on_unload(self, func):
        self._on_unload.append(func)

    def unload(self):
        while self._on_unload:
            self._on_unload.pop()()


def test_writer_still_sends_when_home_assistant_is_stopping():
    hub = _Hub()

    async def main():
        hass = _HomeAssistant()
        entry = _Entry(hass)
        writer = hub_writer.start_hub_writer(hass, entry, hub)
        try:
            await asyncio.sleep(0)  # Home Assistant has been running
            await hass.stop_until_the_stop_event()
            # What a shutdown automation does.
            await asyncio.wait_for(hub_writer.writer_for(hub).run(hub.request, "lights off"), 2)
        finally:
            entry.unload()
        await asyncio.sleep(0)
        return writer

    writer = asyncio.run(main())
    assert hub.sent() == ["lights off"]
    assert writer._task.done(), "unloading the entry ends the worker"


def test_unloading_the_entry_ends_the_worker():
    hub = _Hub()

    async def main():
        hass = _HomeAssistant()
        entry = _Entry(hass)
        writer = hub_writer.start_hub_writer(hass, entry, hub)
        entry.unload()
        await asyncio.sleep(0)
        assert writer._task.done()
        with pytest.raises(hub_writer.HubWriterStopped):
            hub_writer.writer_for(hub)

    asyncio.run(main())


def test_process_shutdown_ends_a_worker_whose_entry_was_never_unloaded():
    # Home Assistant does not unload entries when it stops. What is left is
    # cancelled by the runner (runner.py _cancel_all_tasks_with_timeout, which
    # is asyncio.all_tasks): the worker must be a task of the loop.
    hub = _Hub()

    async def main():
        hass = _HomeAssistant()
        writer = hub_writer.start_hub_writer(hass, _Entry(hass), hub)
        await asyncio.sleep(0)
        assert writer._task in asyncio.all_tasks()
        return writer

    writer = asyncio.run(main())  # asyncio.run cancels all tasks, as the runner does
    assert writer._task.cancelled()
    assert writer._stopped


# --- a worker that never ran ----------------------------------------------------
#
# A task that is cancelled before its first step never runs its coroutine, so
# the worker's `finally` did not run either: the writer still looked alive,
# and run() waited for ever on a queue nobody reads.

@pytest.mark.parametrize("make_task", [
    asyncio.ensure_future,
    lambda work: asyncio.get_running_loop().create_task(work),
    lambda work: asyncio.Task(work, loop=asyncio.get_running_loop(), eager_start=True),
])
@pytest.mark.parametrize("wait_before_run", [False, True])
def test_run_fails_fast_when_the_worker_was_cancelled_before_it_started(make_task, wait_before_run):
    hub = _Hub()

    async def main():
        writer = HubWriter(_Hass(), hub, min_spacing=0)
        writer.start(make_task)
        writer._task.cancel()  # not through stop(): whoever owns the task did it
        if wait_before_run:
            await asyncio.sleep(0)
        with pytest.raises(hub_writer.HubWriterStopped):
            await asyncio.wait_for(writer.run(hub.request, "x"), 1)
        with pytest.raises(hub_writer.HubWriterStopped):
            hub_writer.writer_for(hub)

    asyncio.run(main())
    assert hub.sent() == []


def test_run_without_a_started_worker_fails_fast():
    hub = _Hub()

    async def main():
        with pytest.raises(hub_writer.HubWriterStopped):
            await asyncio.wait_for(HubWriter(_Hass(), hub).run(hub.request, "x"), 1)

    asyncio.run(main())


# --- more than one hub ------------------------------------------------------------

def test_a_slow_hub_does_not_hold_up_the_queue_of_another_hub():
    slow, quick = _Hub("wss://slow"), _Hub("wss://quick", takes=0.0)
    slow.gate = threading.Event()

    async def main():
        writer_slow, writer_quick = _writer(slow), _writer(quick)
        try:
            stuck = asyncio.ensure_future(writer_slow.run(slow.request, "stuck"))
            await asyncio.sleep(0.05)
            done = await asyncio.wait_for(writer_quick.run(quick.request, "other hub"), 1)
            slow.gate.set()
            await stuck
            return done
        finally:
            slow.gate.set()
            writer_slow.stop()
            writer_quick.stop()

    assert asyncio.run(main()) == "other hub"


def test_a_dead_hub_fails_only_its_own_queue():
    dead, alive = _Hub("wss://dead"), _Hub("wss://alive")

    def unreachable():
        time.sleep(0.1)
        raise requests.ConnectionError("no answer")

    async def main():
        writer_dead, writer_alive = _writer(dead), _writer(alive)
        try:
            return await asyncio.gather(
                *(writer_dead.run(unreachable) for _ in range(3)),
                *(writer_alive.run(alive.request, n) for n in range(3)),
                return_exceptions=True)
        finally:
            writer_dead.stop()
            writer_alive.stop()

    results = asyncio.run(main())
    assert isinstance(results[0], requests.ConnectionError)
    assert all(isinstance(r, hub_writer.HubUnreachable) for r in results[1:3]), results
    assert results[3:] == [0, 1, 2], "the other hub's commands were failed too"
