"""Real-Home-Assistant tests: the integration set up in an actual HA core against a fake hub.

The unit tests in tests/ stub Home Assistant per module. These tests do not: the
pins are about what Home Assistant itself stores (entity and device registry,
device triggers), so they run the integration's real setup in a real HA, with
only the hub replaced. Run them in the image from tests_ha/Dockerfile:

    docker build -t dirigera-pintest tests_ha
    docker run --rm -v "$PWD":/repo dirigera-pintest tests_ha -p no:cacheprovider
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeHub:
    """Answers the routes the integration reads, from one fixture; records writes.

    Patched in at dirigera.Hub, so HubX and the plain Hub the listener uses both
    read from it. An unknown route is recorded and answered with an error, so a
    new read shows up instead of silently getting an empty answer.
    """

    def __init__(self, fixture: dict):
        self.devices = fixture["devices"]
        self.scenes = fixture.get("scenes", [])
        self.writes: list[tuple] = []
        self.unknown: list[str] = []

    def get(self, route: str):
        route = route.rstrip("/") or route
        if route == "/devices":
            return copy.deepcopy(self.devices)
        if route.startswith("/devices/"):
            want = route.split("/", 2)[2]
            for d in self.devices:
                if d["id"] == want:
                    return copy.deepcopy(d)
            raise ConnectionError(f"fake hub: no device {want}")
        if route == "/scenes":
            return copy.deepcopy(self.scenes)
        if route.startswith("/scenes/"):
            want = route.split("/")[2]
            for s in self.scenes:
                if s["id"] == want:
                    return copy.deepcopy(s)
        self.unknown.append(route)
        raise ConnectionError(f"fake hub: route {route} not in the fixture")

    def write(self, method, route, data=None):
        self.writes.append((method, route, data))
        return None


@pytest.fixture
def fake_hub_factory():
    """patch_hub(fixture_name) -> FakeHub, with dirigera.Hub's network methods patched."""
    from dirigera.hub.hub import Hub

    patches = []

    def make(name: str) -> FakeHub:
        hub = FakeHub(json.loads((FIXTURES / f"{name}.json").read_text()))
        patches.extend([
            patch.object(Hub, "get", lambda self, route: hub.get(route)),
            patch.object(Hub, "patch", lambda self, route, data: hub.write("PATCH", route, data)),
            patch.object(Hub, "post", lambda self, route, data=None: hub.write("POST", route, data)),
            patch.object(Hub, "delete", lambda self, route, data=None: hub.write("DELETE", route, data)),
        ])
        for p in patches:
            p.start()
        return hub

    yield make
    for p in reversed(patches):
        p.stop()


@pytest.fixture
def captured_listener():
    """The integration's hub_event_listener, created by setup but never started.

    No WebSocket thread runs; the test feeds messages to _apply_message on the
    event loop, which is where on_message hands them in production.
    """
    # The package re-exports the class under the module's name, so import the module itself.
    hel = importlib.import_module("custom_components.dirigera_platform.hub_event_listener")

    holder: list = []
    real_stop = hel.hub_event_listener.stop

    def fake_start(self):
        holder.append(self)

    def fake_stop(self):
        # The real stop, including its unregister_hub; only the join is
        # skipped, as no thread was ever started.
        self.join = lambda *args, **kwargs: None
        real_stop(self)

    # The listener's registry is a class attribute: a test must not see the
    # entities of the one before it.
    hel.hub_event_listener.device_registry.clear()
    hel.controller_trigger_last_time_map.clear()
    hel.remotes_with_press_events.clear()
    with patch.object(hel.hub_event_listener, "start", fake_start), \
            patch.object(hel.hub_event_listener, "stop", fake_stop):
        yield holder
    # Unloading the entry stops the listener, which drops this hub's
    # registrations; anything left would route events to dead entities.
    leftover = {hub: len(ids) for hub, ids in hel.hub_event_listener.device_registry.items() if ids}
    hel.hub_event_listener.device_registry.clear()
    assert not leftover, f"listener registrations left after unload: {leftover}"
