"""
IKEA device sets whose lights may be left out, kept up to date while Home
Assistant runs.

Two parts are tested here.

The hub event listener. A set takes its state from its first light, through
the cascade in on_message. Under "Sets only" that light is not in Home
Assistant, so its own state write is skipped while the cascade to the set
still runs. The listener also hands set lifecycle events and membership
changes to the device set manager.

The device set manager (light.py), which applies the mode as sets and
memberships change. It is lifted out of light.py with ast, as in the other
light tests, and runs against the real listener registry and DeviceSetIndex,
with stub Home Assistant registries.
"""
import ast
import asyncio
import importlib
import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace

import pytest
from pydantic import BaseModel


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# --- load hub_event_listener.py standalone (as test_reconnect_resync.py) -----
_stub("websocket", WebSocketApp=object)
_dir = _stub("dirigera", Hub=object) if "dirigera" not in sys.modules else sys.modules["dirigera"]
if "dirigera.devices.device" not in sys.modules:
    _dir_dev = _stub("dirigera.devices")
    _dir_dev_device = _stub("dirigera.devices.device", Room=object)
    _dir.devices = _dir_dev
    _dir_dev.device = _dir_dev_device
_ha = _stub("homeassistant")
_ha_const = _stub("homeassistant.const", ATTR_ENTITY_ID="entity_id")
_ha_comp = _stub("homeassistant.components")
_ColorMode = type("ColorMode", (), {"HS": "hs", "COLOR_TEMP": "color_temp", "ONOFF": "onoff"})
_ha_light = _stub("homeassistant.components.light", ColorMode=_ColorMode)
_ha_helpers = _stub(
    "homeassistant.helpers",
    device_registry=types.ModuleType("dr"),
    entity_registry=types.ModuleType("er"),
    area_registry=types.ModuleType("ar"),
)
_ha.const = _ha_const
_ha.components = _ha_comp
_ha_comp.light = _ha_light
_ha.helpers = _ha_helpers

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
asyncio.set_event_loop(asyncio.new_event_loop())
_spec = importlib.util.spec_from_file_location(
    "hub_event_listener_device_sets_uut", os.path.join(COMPONENT, "hub_event_listener.py")
)
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)
hub_event_listener = hel.hub_event_listener

# --- device_sets.py through a stand-in package (see test_device_sets.py) -----
_PKG = "dirigera_platform_device_sets_uut"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [COMPONENT]
    sys.modules[_PKG] = _pkg
ds = importlib.import_module(f"{_PKG}.device_sets")
const = importlib.import_module(f"{_PKG}.const")

DOMAIN = "dirigera_platform"
ENTRY_ID = "entry-73"
HUB_KEY = "wss://hub-73"


# --- the listener -------------------------------------------------------------

class FakeBulb:
    """Stands in for ikea_bulb: registered with the listener, maybe not in HA."""

    def __init__(self, light_id, set_id=None, set_name=None, room=None, added=False):
        device_set = [{"id": set_id, "name": set_name or set_id}] if set_id else []
        self.unique_id = light_id
        self._json_data = SimpleNamespace(
            id=light_id, device_set=device_set, room=room, is_reachable=True,
            relation_id=None, attributes=SimpleNamespace(),
        )
        self.available = True
        # What Home Assistant sets: hass once added, enabled from the registry.
        self.hass = object() if added else None
        self.enabled = True
        self.should_ignore_update = False
        self.brightness = 128
        self.supported_color_modes = {"color_temp"}
        self.color_mode = "color_temp"
        self.pushed = 0
        hub_event_listener.register(HUB_KEY, light_id, hel.registry_entry(self))

    def async_schedule_update_ha_state(self, force_refresh=False):
        if self.hass is None:
            # What Home Assistant does for an entity that was never added.
            raise AttributeError("'NoneType' object has no attribute 'loop'")
        self.pushed += 1


class FakeSet:
    unique_id = "kitchen"
    hass = object()
    enabled = True

    def __init__(self):
        self.pushed = 0

    def async_schedule_update_ha_state(self, force_refresh=False):
        self.pushed += 1


class ImmediateLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def is_closed(self):
        return False


class Spy:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("async_"):
            return lambda *args: self.calls.append((name, args))
        raise AttributeError(name)


def _listener(manager=None, devices=None):
    hub_event_listener.device_registry.pop(HUB_KEY, None)
    asyncio.set_event_loop(asyncio.new_event_loop())
    hub = SimpleNamespace(websocket_base_url=HUB_KEY, get=lambda path: devices or [])
    data = {DOMAIN: {ENTRY_ID: {"device_sets": manager} if manager else {}}}

    class Hass:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    hass = Hass()
    hass.data = data
    listener = hub_event_listener(hub, hass, None, ENTRY_ID)
    listener._loop = ImmediateLoop()
    return listener


def _send(listener, msg_type, data):
    listener.on_message(None, json.dumps({"type": msg_type, "data": data}))


def test_a_light_left_out_of_ha_still_updates_its_set():
    listener = _listener()
    bulb = FakeBulb("a", "kitchen")
    device_set = FakeSet()
    hub_event_listener.get_registry_entry(HUB_KEY, "a").cascade_entity = device_set

    # Merged into the 0.4.1 refactor: a state write follows a real change of the
    # model, and this fake's _json_data is no pydantic model (isReachable is not
    # applied to it), so the event carries an attribute.
    _send(listener, "deviceStateChanged", {"id": "a", "deviceType": "light", "isReachable": True,
                                           "attributes": {"lightLevel": 50}})

    assert bulb.pushed == 0, "a light that is not in HA gets no state write"
    assert device_set.pushed == 1, "the set must still follow its first light"


def test_a_light_the_mode_disabled_gets_no_state_write():
    # Home Assistant would drop the write, but warn that we update a disabled
    # entity. The set still follows the light.
    listener = _listener()
    bulb = FakeBulb("a", "kitchen", added=True)
    bulb.enabled = False
    device_set = FakeSet()
    hub_event_listener.get_registry_entry(HUB_KEY, "a").cascade_entity = device_set

    # Merged into the 0.4.1 refactor: a state write follows a real change of the
    # model, and this fake's _json_data is no pydantic model (isReachable is not
    # applied to it), so the event carries an attribute.
    _send(listener, "deviceStateChanged", {"id": "a", "deviceType": "light", "isReachable": False,
                                           "attributes": {"lightLevel": 50}})

    assert bulb.pushed == 0
    assert device_set.pushed == 1


def test_set_lifecycle_events_go_to_the_manager():
    spy = Spy()
    listener = _listener(spy)
    _send(listener, "deviceSetCreated", {"id": "s1", "name": "Spots", "icon": "x"})
    _send(listener, "deviceSetUpdated", {"id": "s1", "name": "Lamps", "icon": "x"})
    _send(listener, "deviceSetDeleted", {"id": "s1"})
    assert spy.calls == [
        ("async_handle_set_event", ("deviceSetCreated", "s1", "Spots")),
        ("async_handle_set_event", ("deviceSetUpdated", "s1", "Lamps")),
        ("async_handle_set_event", ("deviceSetDeleted", "s1", None)),
    ]


def test_a_membership_change_goes_to_the_manager():
    spy = Spy()
    listener = _listener(spy)
    FakeBulb("a", added=True)
    payload = [{"id": "s1", "name": "Spots", "icon": "x"}]
    _send(listener, "deviceStateChanged", {
        "id": "a", "deviceType": "light", "attributes": {}, "room": None, "deviceSet": payload,
    })
    assert spy.calls == [("async_handle_membership", ("a", payload, None))]


def test_the_membership_change_comes_after_the_state_write():
    """Joining a set under "Sets only" disables the light. A state write
    queued after that reaches a disabled entity, which Home Assistant warns
    about, so the write has to be queued first."""
    spy = Spy()

    class RecordingBulb(FakeBulb):
        def async_schedule_update_ha_state(self, force_refresh=False):
            spy.calls.append(("write", self.unique_id))

    listener = _listener(spy)
    RecordingBulb("a", added=True)
    payload = [{"id": "s1", "name": "Spots"}]
    _send(listener, "deviceStateChanged", {
        "id": "a", "deviceType": "light", "attributes": {}, "deviceSet": payload,
    })
    assert spy.calls == [("write", "a"), ("async_handle_membership", ("a", payload, None))]


def test_a_plain_state_event_is_not_a_membership_change():
    spy = Spy()
    listener = _listener(spy)
    FakeBulb("a", added=True)
    _send(listener, "deviceStateChanged", {"id": "a", "deviceType": "light", "attributes": {}})
    assert spy.calls == []


def test_set_events_without_the_light_platform_are_ignored():
    listener = _listener(None)
    _send(listener, "deviceSetCreated", {"id": "s1", "name": "Spots"})
    _send(listener, "deviceSetDeleted", {})  # malformed: logged, not raised


def test_resync_prunes_empty_sets_after_the_replay():
    spy = Spy()
    devices = [{"id": "a", "deviceType": "light", "attributes": {}, "deviceSet": []}]
    listener = _listener(spy, devices)
    FakeBulb("a", "kitchen", added=True)
    asyncio.run(listener._resync_all_states())
    assert [name for name, _ in spy.calls] == ["async_handle_membership", "async_prune_empty_sets"]



class _Attributes(BaseModel):
    is_on: bool = False


def _scene(*device_ids):
    return {"data": {"actions": [
        {"type": "device", "deviceId": device_id, "attributes": {"isOn": True}} for device_id in device_ids
    ]}}


def test_one_failing_scene_action_does_not_stop_the_others():
    class BrokenBulb(FakeBulb):
        def async_schedule_update_ha_state(self, force_refresh=False):
            raise RuntimeError("boom")

    listener = _listener()
    BrokenBulb("a", added=True)._json_data.attributes = _Attributes()
    good = FakeBulb("b", added=True)
    good._json_data.attributes = _Attributes()

    listener._apply_scene_actions(_scene("a", "b"))

    assert good._json_data.attributes.is_on is True
    assert good.pushed == 1


def test_scene_actions_write_no_state_while_stopping():
    listener = _listener()
    bulb = FakeBulb("a", added=True)
    bulb._json_data.attributes = _Attributes()
    listener._request_to_stop = True

    listener._apply_scene_actions(_scene("a"))

    assert bulb.pushed == 0

# --- the device set manager ---------------------------------------------------

LIGHT = os.path.join(COMPONENT, "light.py")


@pytest.fixture(autouse=True)
def _restore_registry_sync(monkeypatch):
    """World patches the sync onto the stand-in package; undo it after each test."""
    monkeypatch.setattr(sys.modules[_PKG], "sync_platform_entity_registry", None, raising=False)


class FakeLightEntity:
    """The part of LightEntity the set entity uses."""

    hass = None

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1


class FakeEntityRegistry:
    def __init__(self, known):
        self.known = known
        self.removed = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.known.get(unique_id)

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


class FakeDeviceRegistry:
    def __init__(self):
        self.detached = []
        self.names = []

    def async_update_device(self, device_id, name=None, remove_config_entry_id=None):
        if remove_config_entry_id is not None:
            self.detached.append((device_id, remove_config_entry_id))
        if name is not None:
            self.names.append((device_id, name))


class World:
    """One manager with its stub Home Assistant around it."""

    def __init__(self, mode, bulbs, known_entities=None):
        hub_event_listener.device_registry.pop(HUB_KEY, None)
        self.bulbs = {b[0]: FakeBulb(*b) for b in bulbs}
        self.added = []
        self.syncs = []
        self.ent_reg = FakeEntityRegistry(known_entities or {})
        self.dev_reg = FakeDeviceRegistry()

        sys.modules[_PKG].sync_platform_entity_registry = (
            lambda hass, entry, platforms, hidden: self.syncs.append(set(hidden))
        )
        ns = _manager_namespace(self)
        bulbs = list(self.bulbs.values())
        data = {const.CONF_DEVICE_SET_MODE: mode, "platforms": ["light"]}
        self.manager = ns["DeviceSetManager"](
            SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID),
            SimpleNamespace(websocket_base_url=HUB_KEY), data,
            ds.DeviceSetIndex.from_lights(bulbs), bulbs,
            lambda entities: self.added.extend(entities),
        )
        self.manager.async_setup()

    def added_ids(self):
        return [e.unique_id for e in self.added]

    def set_entity(self, set_id):
        return next(e for e in self.added if e.unique_id == set_id)

    def cascade_of(self, light_id):
        return hub_event_listener.get_registry_entry(HUB_KEY, light_id).cascade_entity


def _manager_namespace(world):
    with open(LIGHT) as fh:
        tree = ast.parse(fh.read())
    wanted = ("DeviceSetManager", "ikea_bulb_device_set")
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in wanted]
    assert len(nodes) == 2, "expected DeviceSetManager and ikea_bulb_device_set in light.py"
    ns = {
        "__name__": "light_uut",
        "__package__": _PKG,
        "LightEntity": FakeLightEntity,
        "callback": lambda fn: fn,
        "DeviceSet": ds.DeviceSet,
        "DeviceSetIndex": ds.DeviceSetIndex,
        "light_wanted": ds.light_wanted,
        "resolve_device_set_mode": ds.resolve_device_set_mode,
        "sets_wanted": ds.sets_wanted,
        "hub_event_listener": hub_event_listener,
        "registry_entry": hel.registry_entry,
        # The device still carries the set id as its name, so a rename differs.
        "get_device_by_identifier": lambda reg, identifier, entry_id: SimpleNamespace(
            id=f"dev-{identifier[1]}", name=identifier[1]
        ),
        "er": SimpleNamespace(async_get=lambda hass: world.ent_reg),
        "dr": SimpleNamespace(async_get=lambda hass: world.dev_reg),
        "DOMAIN": DOMAIN,
        "DEVICE_SET_MODEL": const.DEVICE_SET_MODEL,
        "ColorMode": _ColorMode,
        "DeviceInfo": dict,
        "HomeAssistantError": Exception,
        "ikea_bulb": FakeBulb,
        "logger": hel.logger,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), LIGHT, "exec"), ns)
    return ns


SETS_ONLY = const.DEVICE_SET_MODE_SETS_ONLY
SETS_AND_LIGHTS = const.DEVICE_SET_MODE_SETS_AND_LIGHTS
LIGHTS_ONLY = const.DEVICE_SET_MODE_LIGHTS_ONLY

# kitchen: a (first), b; hall: d; c is in no set.
BULBS = [("a", "kitchen", "Kitchen"), ("b", "kitchen", "Kitchen"), ("c",), ("d", "hall", "Hall")]


def test_sets_only_imports_the_sets_and_the_lights_outside_them():
    world = World(SETS_ONLY, BULBS)
    assert sorted(world.added_ids()) == ["c", "hall", "kitchen"]


def test_sets_with_lights_imports_everything():
    world = World(SETS_AND_LIGHTS, BULBS)
    assert sorted(world.added_ids()) == ["a", "b", "c", "d", "hall", "kitchen"]


def test_lights_only_imports_no_sets():
    world = World(LIGHTS_ONLY, BULBS)
    assert sorted(world.added_ids()) == ["a", "b", "c", "d"]


def test_a_set_follows_its_first_light():
    world = World(SETS_ONLY, BULBS)
    kitchen = world.set_entity("kitchen")
    assert world.cascade_of("a") is kitchen
    assert world.cascade_of("b") is None
    assert kitchen.brightness == 128 and kitchen.available


def test_when_the_first_light_leaves_the_set_follows_the_next_one():
    world = World(SETS_ONLY, BULBS)
    kitchen = world.set_entity("kitchen")
    kitchen.hass = object()

    world.manager.async_handle_membership("a", [], None)

    assert kitchen._controller is world.bulbs["b"]
    assert world.cascade_of("a") is None and world.cascade_of("b") is kitchen
    assert kitchen.writes == 1
    # 'a' is in no set now, so Sets only imports it: its registry entry is no
    # longer hidden, and the never-added instance is added right away.
    assert world.syncs[-1] == {"b", "d"}
    assert "a" in world.added_ids()


def test_a_light_joining_a_set_is_left_out_under_sets_only():
    world = World(SETS_ONLY, BULBS)
    world.manager.async_handle_membership("c", [{"id": "kitchen", "name": "Kitchen"}], None)
    # Hiding is the registry's job: disabling the entry removes the entity.
    assert world.syncs[-1] == {"a", "b", "c", "d"}
    assert world.manager._index.get("kitchen").members == ["a", "b", "c"]


def test_a_light_joining_an_unknown_set_creates_the_set():
    world = World(SETS_ONLY, BULBS)
    world.manager.async_handle_membership("c", [{"id": "spots", "name": "Spots"}], SimpleNamespace(name="Hall"))
    spots = world.set_entity("spots")
    assert spots.device_info["name"] == "Spots" and spots.device_info["suggested_area"] == "Hall"
    assert spots._controller is world.bulbs["c"]
    assert world.cascade_of("c") is spots


def test_a_created_set_appears_with_its_first_light():
    world = World(SETS_ONLY, BULBS)
    world.manager.async_handle_set_event("deviceSetCreated", "spots", "Spots")
    assert "spots" not in world.added_ids(), "no entity before a light is in it"
    world.manager.async_handle_membership("c", [{"id": "spots", "name": "Spots"}], None)
    assert world.set_entity("spots").device_info["name"] == "Spots"


def test_lights_only_creates_no_set_when_lights_join():
    world = World(LIGHTS_ONLY, BULBS)
    world.manager.async_handle_membership("c", [{"id": "spots", "name": "Spots"}], None)
    assert "spots" not in world.added_ids()
    assert world.syncs[-1] == {"kitchen", "hall", "spots"}


def test_a_renamed_set_renames_its_device():
    world = World(SETS_ONLY, BULBS)
    kitchen = world.set_entity("kitchen")
    world.manager.async_handle_set_event("deviceSetUpdated", "kitchen", "Cooking")
    assert kitchen.device_info["name"] == "Cooking"
    # Only the device's own name is written, never name_by_user (#72).
    assert world.dev_reg.names == [("dev-kitchen", "Cooking")]


def test_a_set_entity_is_named_by_its_device():
    """With a name of its own, Home Assistant joins device and entity name,
    and a rename showed as "Test set 2 2" on a real hub."""
    kitchen = World(SETS_ONLY, BULBS).set_entity("kitchen")
    assert kitchen._attr_has_entity_name is True
    assert kitchen._attr_name is None


def test_a_deleted_set_is_removed_and_its_lights_come_back():
    world = World(SETS_ONLY, BULBS, known_entities={"kitchen": "light.kitchen"})
    world.manager.async_handle_set_event("deviceSetDeleted", "kitchen")

    assert world.ent_reg.removed == ["light.kitchen"]
    assert world.dev_reg.detached == [("dev-kitchen", ENTRY_ID)]
    assert world.cascade_of("a") is None
    assert hub_event_listener.get_registry_entry(HUB_KEY, "kitchen") is None
    assert world.syncs[-1] == {"d"}
    assert {"a", "b"} <= set(world.added_ids())
    # The hub's follow-up deviceSet [] per light changes nothing more.
    syncs = len(world.syncs)
    world.manager.async_handle_membership("a", [], None)
    assert len(world.syncs) == syncs


def test_replaying_unchanged_membership_touches_nothing():
    world = World(SETS_ONLY, BULBS)
    added = list(world.added)
    world.manager.async_handle_membership("a", [{"id": "kitchen", "name": "Kitchen"}], None)
    world.manager.async_handle_membership("c", [], None)
    assert world.syncs == [] and world.added == added


def test_an_emptied_set_is_unavailable_until_pruned():
    world = World(SETS_ONLY, BULBS, known_entities={"hall": "light.hall"})
    hall = world.set_entity("hall")
    world.manager.async_handle_membership("d", [], None)
    assert hall.available is False
    assert hall.supported_color_modes == {"onoff"} and hall.color_mode == "onoff"
    for prop in ("brightness", "min_color_temp_kelvin", "max_color_temp_kelvin",
                 "color_temp_kelvin", "hs_color", "is_on"):
        assert getattr(hall, prop) is None, prop

    world.manager.async_prune_empty_sets()
    assert world.ent_reg.removed == ["light.hall"]
    assert world.manager._index.get("hall") is None


def test_a_light_paired_at_runtime_follows_the_mode():
    world = World(SETS_ONLY, BULBS)
    in_set = FakeBulb("e", "kitchen", "Kitchen")
    alone = FakeBulb("f")
    world.manager.async_add_discovered([in_set, alone])
    assert "e" not in world.added_ids()
    assert "f" in world.added_ids()
    assert world.manager._index.get("kitchen").members == ["a", "b", "e"]


def test_an_entity_is_handed_to_home_assistant_only_once():
    world = World(SETS_AND_LIGHTS, BULBS)
    world.manager.async_handle_membership("c", [{"id": "kitchen", "name": "Kitchen"}], None)
    world.manager.async_handle_membership("c", [], None)
    ids = world.added_ids()
    assert len(ids) == len(set(ids))
