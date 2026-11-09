"""One queue of commands per hub (#71).

Home Assistant turns on the lights of an area or a scene as parallel calls, so
a hub used to get every light's PATCH at once. On guaifi's hub that left bulbs
on the wrong level or colour temperature after the hub had accepted every
request (6 bulbs: 19 of 24 bulb-runs wrong with dirigera 1.6.0, 6 of 24 with
1.6.1). Sent one at a time, a request no sooner than MIN_SPACING_S after the
previous one started, none were wrong.

Kept free of Home Assistant imports so the tests can use it directly.
"""
import asyncio
import time

MIN_SPACING_S = 0.2

_queues = {}


class HubCommandQueue:
    def __init__(self, min_spacing: float = MIN_SPACING_S):
        self._lock = asyncio.Lock()
        self._min_spacing = min_spacing
        self._last_start = None

    async def run(self, hass, fn, *args):
        """Run fn(*args) in the executor, after every command queued before it."""
        async with self._lock:
            if self._last_start is not None:
                wait = self._last_start + self._min_spacing - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_start = time.monotonic()
            return await hass.async_add_executor_job(fn, *args)


def queue_for(hub) -> HubCommandQueue:
    """The queue of this hub; hubs are told apart by their websocket address."""
    key = getattr(hub, "websocket_base_url", None) or id(hub)
    queue = _queues.get(key)
    if queue is None:
        queue = _queues[key] = HubCommandQueue()
    return queue
