"""
Choosing how IKEA device sets are imported.

device_sets.py holds the decisions (which mode an entry is in, which lights
are in which set, what a mode leaves out). It has no Home Assistant imports,
but it imports const.py relatively, so it is loaded as part of a stand-in
package that has the component directory as its path and skips the real
__init__.py (which pulls in dirigera and homeassistant).
"""
import importlib
import os
import sys
import types
from types import SimpleNamespace

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)

_PKG = "dirigera_platform_device_sets_uut"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [COMPONENT]
    sys.modules[_PKG] = _pkg
ds = importlib.import_module(f"{_PKG}.device_sets")
const = importlib.import_module(f"{_PKG}.const")

SETS_ONLY = const.DEVICE_SET_MODE_SETS_ONLY
SETS_AND_LIGHTS = const.DEVICE_SET_MODE_SETS_AND_LIGHTS
LIGHTS_ONLY = const.DEVICE_SET_MODE_LIGHTS_ONLY


def _light(light_id, set_id=None, set_name=None, room=None):
    device_set = [{"id": set_id, "name": set_name or set_id, "icon": "lamp"}] if set_id else []
    return SimpleNamespace(_json_data=SimpleNamespace(id=light_id, device_set=device_set, room=room))


def _index():
    return ds.DeviceSetIndex.from_lights([
        _light("a", "kitchen", "Kitchen", room="room-k"),
        _light("b", "kitchen", "Kitchen", room="room-other"),
        _light("c"),
        _light("d", "hall", "Hall"),
    ])


# --- the option -------------------------------------------------------------

def test_an_entry_from_before_the_option_keeps_sets_with_lights():
    # Every existing entry got the sets and their lights, whatever the old
    # checkbox said, so that is what it keeps after updating.
    assert ds.resolve_device_set_mode({}) == SETS_AND_LIGHTS
    assert ds.resolve_device_set_mode({"hide_device_set_bulbs": True}) == SETS_AND_LIGHTS


def test_a_stored_mode_is_used():
    for mode in (SETS_ONLY, SETS_AND_LIGHTS, LIGHTS_ONLY):
        assert ds.resolve_device_set_mode({const.CONF_DEVICE_SET_MODE: mode}) == mode


def test_an_unknown_mode_falls_back_instead_of_failing_setup():
    assert ds.resolve_device_set_mode({const.CONF_DEVICE_SET_MODE: "bogus"}) == SETS_AND_LIGHTS


def test_new_entries_default_to_sets_only():
    # Sets only matches what the IKEA app shows.
    assert const.DEFAULT_DEVICE_SET_MODE == SETS_ONLY
    assert const.DEVICE_SET_MODES == (SETS_ONLY, SETS_AND_LIGHTS, LIGHTS_ONLY)


def test_what_each_mode_imports():
    assert ds.sets_wanted(SETS_ONLY) and ds.sets_wanted(SETS_AND_LIGHTS)
    assert not ds.sets_wanted(LIGHTS_ONLY)
    # A light in no set is imported with every choice.
    assert all(ds.light_wanted(mode, in_set=False) for mode in (SETS_ONLY, SETS_AND_LIGHTS, LIGHTS_ONLY))
    assert not ds.light_wanted(SETS_ONLY, in_set=True)
    assert ds.light_wanted(SETS_AND_LIGHTS, in_set=True)
    assert ds.light_wanted(LIGHTS_ONLY, in_set=True)


# --- the index --------------------------------------------------------------

def test_index_groups_lights_by_set_in_order():
    index = _index()
    assert sorted(index.set_ids()) == ["hall", "kitchen"]
    assert index.get("kitchen").members == ["a", "b"]
    assert index.controller_id("kitchen") == "a"
    assert not index.in_set("c")
    kitchen = index.get("kitchen")
    assert kitchen.name == "Kitchen"
    # The room of the first light seen is the suggested area.
    assert kitchen.room == "room-k"


def test_index_takes_dirigera_models_directly():
    model = _light("a", "kitchen")._json_data
    assert ds.DeviceSetIndex.from_lights([model]).get("kitchen").members == ["a"]


def test_hidden_unique_ids_per_mode():
    index = _index()
    assert index.hidden_unique_ids(SETS_ONLY) == {"a", "b", "d"}
    assert index.hidden_unique_ids(LIGHTS_ONLY) == {"kitchen", "hall"}
    assert index.hidden_unique_ids(SETS_AND_LIGHTS) == set()


def test_unchanged_membership_is_a_no_op():
    # A reconnect resync replays every light with its deviceSet.
    index = _index()
    change = index.set_membership("a", [{"id": "kitchen", "name": "Kitchen"}])
    assert not change.changed
    assert not index.set_membership("c", []).changed
    assert index.get("kitchen").members == ["a", "b"]


def test_leaving_a_set_hands_the_set_to_the_next_light():
    index = _index()
    change = index.set_membership("a", [])
    assert (change.left, change.joined) == ("kitchen", None)
    assert index.controller_id("kitchen") == "b"
    assert not index.in_set("a")


def test_joining_an_unknown_set_creates_it_from_the_payload():
    index = _index()
    change = index.set_membership("c", [{"id": "new", "name": "Spots", "icon": "x"}], room="room-c")
    assert (change.left, change.joined) == (None, "new")
    assert index.get("new").name == "Spots"
    assert index.get("new").room == "room-c"
    assert index.controller_id("new") == "c"


def test_moving_between_sets_reports_both():
    index = _index()
    change = index.set_membership("d", [{"id": "kitchen", "name": "Kitchen"}])
    assert (change.left, change.joined) == ("hall", "kitchen")
    assert index.get("kitchen").members == ["a", "b", "d"]
    assert index.empty_set_ids() == ["hall"]


def test_a_rename_carried_by_a_membership_payload_is_picked_up():
    # A rename missed while disconnected comes back through the resync.
    index = _index()
    change = index.set_membership("a", [{"id": "kitchen", "name": "Cooking"}])
    assert change.renamed == "kitchen" and change.left is None and change.joined is None
    assert index.get("kitchen").name == "Cooking"


def test_created_set_waits_for_its_first_light():
    index = _index()
    index.create("new", "Spots")
    assert index.controller_id("new") is None
    assert index.empty_set_ids() == ["new"]
    index.create("new", "Again")
    assert index.get("new").name == "Spots", "creating a known set changes nothing"


def test_rename_reports_only_real_changes():
    index = _index()
    assert index.rename("kitchen", "Kitchen") is False
    assert index.rename("kitchen", None) is False
    assert index.rename("missing", "x") is False
    assert index.rename("kitchen", "Cooking") is True


def test_delete_returns_the_former_members_and_frees_them():
    index = _index()
    assert index.delete("kitchen") == ["a", "b"]
    assert index.get("kitchen") is None
    assert not index.in_set("a") and not index.in_set("b")
    # The hub's follow-up deviceSet [] for each light is then a no-op.
    assert not index.set_membership("a", []).changed
    assert index.delete("kitchen") == []
