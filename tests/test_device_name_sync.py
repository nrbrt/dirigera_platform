"""
#72: a name the user gave a device in Home Assistant survives a restart.

The startup name sync called _update_device_name(force=True), which cleared
name_by_user, so a device renamed in HA came back with its IKEA name after
every restart. Home Assistant shows name_by_user over name, and that field
belongs to the user. Now only `name` follows the hub: a rename in the IKEA
app still reaches every device the user did not rename in HA (#19).

The method is lifted out of hub_event_listener.py with ast, so it runs
without importing homeassistant.
"""
import ast
import asyncio
import logging
import os
from types import SimpleNamespace

PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "hub_event_listener.py",
)


class _Registry:
    def __init__(self, entry):
        self.entry = entry
        self.updates = []

    def async_update_device(self, device_id, **changes):
        self.updates.append(changes)
        for key, value in changes.items():
            setattr(self.entry, key, value)


def _method(registry):
    with open(PATH) as fh:
        tree = ast.parse(fh.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "hub_event_listener")
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_update_device_name")
    ns = {
        "dr": SimpleNamespace(async_get=lambda hass: registry),
        "get_device_by_identifier": lambda reg, identifier, entry_id: reg.entry,
        "logger": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), PATH, "exec"), ns)
    return ns["_update_device_name"]


def _run(entry, new_name):
    registry = _Registry(entry)
    listener = SimpleNamespace(_hass=None, _config_entry_id="entry-1")
    asyncio.run(_method(registry)(listener, "dev-1", new_name))
    return registry


def test_user_name_is_kept_and_the_hub_name_still_follows():
    entry = SimpleNamespace(id="d1", name="Bulb", name_by_user="Hue - Bulb")
    registry = _run(entry, "Desk lamp")
    assert entry.name_by_user == "Hue - Bulb"
    assert entry.name == "Desk lamp"
    assert all("name_by_user" not in u for u in registry.updates)


def test_ikea_rename_reaches_a_device_without_a_user_name():
    entry = SimpleNamespace(id="d1", name="Bulb", name_by_user=None)
    _run(entry, "Desk lamp")
    assert (entry.name, entry.name_by_user) == ("Desk lamp", None)


def test_nothing_is_written_when_the_name_is_unchanged():
    entry = SimpleNamespace(id="d1", name="Desk lamp", name_by_user="Mine")
    assert _run(entry, "Desk lamp").updates == []


def test_no_caller_asks_to_overwrite_the_user_name():
    with open(PATH) as fh:
        source = fh.read()
    assert "force=True" not in source
    assert "name_by_user=None" not in source
