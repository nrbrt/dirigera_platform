"""One write path per hub: a worker that sends one command at a time.

Home Assistant turns on the lights of an area or a scene as parallel calls, so
a hub used to get every light's PATCH at once. On guaifi's hub (#71) that left
bulbs on the wrong level or colour temperature after the hub had accepted every
request (6 bulbs: 19 of 24 bulb-runs wrong with dirigera 1.6.0, 6 of 24 with
1.6.1). Sent one at a time, a request no sooner than MIN_SPACING_S after the
previous one started, none were wrong.

Every command to a hub, of every kind of device, is handed to the hub's
HubWriter. Its worker task takes them in the order they were asked for and
runs each in the executor. The worker belongs to the config entry (see
start_hub_writer), not to whoever asked:

* A caller that is cancelled (a script in mode: restart) stops waiting. A
  command that is already with the hub is finished, and the next one starts
  after it; a command that was still waiting is not sent.
* When the hub does not answer (a connection error or timeout from requests),
  the commands that were waiting behind it fail at once with HubUnreachable
  instead of each running into its own 10 s timeout. A command that comes
  after that is tried again.
* When the entry is unloaded the worker ends and everything that was waiting
  fails with HubWriterStopped.

Kept free of Home Assistant imports so the tests can use it directly.
"""
import asyncio
import time

import requests

MIN_SPACING_S = 0.2

# What the dirigera library lets through when the hub cannot be reached. An
# HTTP error is not one of them: the hub answered.
_UNREACHABLE = (requests.ConnectionError, requests.Timeout)

_writers = {}  # websocket address of the hub -> its writers, the newest last


class HubWriterStopped(Exception):
    """The hub's integration entry is not loaded (any more)."""


class HubUnreachable(Exception):
    """Not sent: the command before this one found the hub unreachable."""


class HubWriter:
    def __init__(self, hass, hub, min_spacing: float = MIN_SPACING_S):
        self._hass = hass
        self._hub = hub
        self._key = hub.websocket_base_url
        self._min_spacing = min_spacing
        self._queue = asyncio.Queue()
        self._last_start = None
        self._current = None
        self._task = None
        self._stopped = False

    # -- lifetime ---------------------------------------------------------------

    def start(self, create_task=None):
        """Start the worker. `create_task` makes the task from the coroutine
        (see start_hub_writer)."""
        _writers.setdefault(self._key, []).append(self)
        self._task = (create_task or asyncio.ensure_future)(self._work())

    def stop(self):
        """End the worker; what is waiting, or with the hub, fails for its caller."""
        self._close()
        if self._task is not None:
            self._task.cancel()

    def _close(self):
        self._stopped = True
        writers = _writers.get(self._key, [])
        if self in writers:
            writers.remove(self)
        stopped = HubWriterStopped(f"the integration for hub {self._key} was unloaded")
        _fail(self._current, stopped)
        self._fail_waiting(stopped)

    def _fail_waiting(self, error):
        while not self._queue.empty():
            _fail(self._queue.get_nowait()[-1], error)

    # -- commands ---------------------------------------------------------------

    async def run(self, fn, *args, started=None):
        """Run the blocking fn(*args) in the executor, after every command
        asked before it, and return what it returns.

        `started` is called on the event loop when the command's turn has
        come, right before it goes to the hub; not at all for a command that
        is never sent."""
        if self._stopped:
            raise HubWriterStopped(f"the integration for hub {self._key} is not loaded")
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((fn, args, started, future))
        # Cancelling the caller cancels this future, never the worker.
        return await future

    async def patch_device(self, device_id, data, started=None):
        """PATCH /devices/{device_id}; `data` is the list of attribute items."""
        return await self.run(self._hub.patch, f"/devices/{device_id}", data, started=started)

    # -- the worker -------------------------------------------------------------

    async def _work(self):
        try:
            while True:
                fn, args, started, future = await self._queue.get()
                self._current = future  # out of the queue: stop() must find it
                if self._last_start is not None and not future.done():
                    wait = self._last_start + self._min_spacing - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                if future.done():  # the caller gave up before it was sent
                    continue
                self._last_start = time.monotonic()
                try:
                    if started is not None:
                        started()
                    result = await self._hass.async_add_executor_job(fn, *args)
                except Exception as error:  # pylint: disable=broad-except
                    _fail(future, error)
                    if isinstance(error, _UNREACHABLE):
                        self._fail_waiting(HubUnreachable(
                            f"hub {self._key} cannot be reached: {error}"))
                else:
                    if not future.done():
                        future.set_result(result)
        finally:
            # Cancelled by stop() or by the config entry that owns the task.
            self._close()


def start_hub_writer(hass, entry, hub) -> HubWriter:
    """Start the writer of a config entry's hub. Its worker lives exactly as
    long as the entry: it ends when the entry is unloaded or its setup fails
    (async_on_unload runs for both).

    The worker is a plain task of the loop, not one of Home Assistant's:

    * a background task (hass or entry async_create_background_task) is
      cancelled in async_stop before EVENT_HOMEASSISTANT_STOP is fired, so an
      automation that runs at shutdown would find no writer;
    * a tracked task (async_create_task) never ends here, and
      async_block_till_done waits for those.

    Home Assistant does not unload entries when it stops. The worker is then
    cancelled with everything else that is left on the loop, by the runner
    (runner.py, _cancel_all_tasks_with_timeout). Read in HA 2026.11.1."""
    writer = HubWriter(hass, hub)
    writer.start(lambda work: hass.loop.create_task(work, name="dirigera_platform hub writer"))
    entry.async_on_unload(writer.stop)
    return writer


def _fail(future, error):
    if future is not None and not future.done():
        future.set_exception(error)


def writer_for(hub) -> HubWriter:
    """The writer of this hub. Hubs are told apart by their websocket address:
    entities, discovery and scenes each hold their own hub object."""
    writers = _writers.get(hub.websocket_base_url)
    if not writers:
        raise HubWriterStopped(f"the integration for hub {hub.websocket_base_url} is not loaded")
    return writers[-1]
