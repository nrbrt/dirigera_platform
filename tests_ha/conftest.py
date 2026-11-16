"""Real-Home-Assistant tests: the integration set up in an actual HA core against a fake hub.

The unit tests in tests/ stub Home Assistant per module. These tests do not:
the pins are about what Home Assistant itself stores and does, so they run the
integration's real setup in a real Home Assistant, with only the hub replaced.

Run them in the image from tests_ha/Dockerfile (from the repository root):

    docker build -t dirigera-pintest tests_ha
    docker run --rm -v "$PWD":/repo -u "$(id -u):$(id -g)" -e HOME=/tmp \\
        dirigera-pintest -c tests_ha/pytest.ini tests_ha

Add ``--pin-update`` to write the pin files instead of comparing with them.

Everything that relies on the integration's internals lives here, so a
change in 0.5 needs one edit: how the event listener is kept from starting,
how a hub message is handed to it (``deliver``), the name of the event a
remote fires, and how hub devices map to Home Assistant devices and entities
(``device_identifier``, ``hub_device_of``).
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest
from dirigera.hub.hub import Hub

from custom_components.dirigera_platform.const import DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"

# The event the integration fires for a remote press (hub_event_listener.py).
REMOTE_EVENT = "dirigera_platform_event"

# The package re-exports the listener class under the module's name, so import the module itself.
_listener_module = importlib.import_module("custom_components.dirigera_platform.hub_event_listener")
HubEventListener = _listener_module.hub_event_listener


def pytest_addoption(parser):
    parser.addoption("--pin-update", action="store_true", help="write the pin files instead of comparing")


@pytest.fixture
def pin_update(request) -> bool:
    return request.config.getoption("--pin-update")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeHub:
    """Answers the routes the integration reads, from one fixture; records writes.

    A route or id the fixture does not have is recorded in ``unknown`` and
    answered with an error, so a new read shows up instead of silently getting
    an empty answer.
    """

    def __init__(self, fixture: dict):
        self.devices: list[dict] = fixture["devices"]
        self.scenes: list[dict] = fixture.get("scenes", [])
        self.writes: list[tuple[str, str]] = []
        self.unknown: list[str] = []

    def get(self, route: str):
        parts = route.strip("/").split("/")
        if parts == ["devices"]:
            return copy.deepcopy(self.devices)
        if parts == ["scenes"]:
            return copy.deepcopy(self.scenes)
        if len(parts) == 2 and parts[0] in ("devices", "scenes"):
            items = self.devices if parts[0] == "devices" else self.scenes
            for item in items:
                if item["id"] == parts[1]:
                    return copy.deepcopy(item)
        self.unknown.append(route)
        raise ConnectionError(f"fake hub: {route} is not in the fixture")

    def write(self, method: str, route: str) -> None:
        self.writes.append((method, route))


@pytest.fixture
def fake_hub(monkeypatch):
    """make(fixture_name) -> FakeHub, answering for every dirigera Hub (HubX and the listener's)."""

    def make(name: str) -> FakeHub:
        hub = FakeHub(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))
        monkeypatch.setattr(Hub, "get", lambda self, route: hub.get(route))
        monkeypatch.setattr(Hub, "patch", lambda self, route, data: hub.write("PATCH", route))
        monkeypatch.setattr(Hub, "post", lambda self, route, data=None: hub.write("POST", route))
        monkeypatch.setattr(Hub, "delete", lambda self, route, data=None: hub.write("DELETE", route))
        return hub

    return make


@pytest.fixture
def captured_listener(monkeypatch):
    """The listeners setup created; none is started, so no WebSocket thread runs.

    Messages go in with ``deliver``, on the event loop, which is where
    on_message hands them in production.
    """
    listeners: list = []
    real_stop = HubEventListener.stop

    def fake_stop(self):
        # The real stop, including its unregister_hub; only the join is
        # skipped, as no thread was ever started.
        self.join = lambda *args, **kwargs: None
        real_stop(self)

    # The listener's registry is a class attribute: a test must not see the
    # entities of the one before it.
    HubEventListener.device_registry.clear()
    monkeypatch.setattr(HubEventListener, "start", lambda self: listeners.append(self))
    monkeypatch.setattr(HubEventListener, "stop", fake_stop)
    yield listeners
    HubEventListener.device_registry.clear()


def listener_registrations_left() -> dict[str, int]:
    """Registrations per hub still in the listener's registry.

    Unloading the entry stops the listener, which drops its hub's
    registrations; anything left would route events to dead entities.
    """
    return {hub: len(ids) for hub, ids in HubEventListener.device_registry.items() if ids}


def deliver(listener, message: dict) -> None:
    """Hand one hub message to the listener, as on_message does after parsing it."""
    listener._apply_message(message)


def device_identifier(hub_device: dict) -> tuple[str, str]:
    """The Home Assistant device identifier the integration gives a hub device:
    its relationId (shared by the parts of a split device), else its id."""
    return DOMAIN, hub_device.get("relationId") or hub_device["id"]


def hub_device_of(unique_id: str, hub_devices: list[dict]) -> dict | None:
    """The hub device an entity was made for: entity unique_ids start with
    their hub device's id (device-05_1CA01 belongs to device-05_1)."""
    found = [d for d in hub_devices if unique_id.startswith(d["id"])]
    return max(found, key=lambda d: len(d["id"]), default=None)
