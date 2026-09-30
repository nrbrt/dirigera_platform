"""
Issue #52: stop using the APIs Home Assistant deprecates in 2026.9 without
breaking the older releases that lack their replacements.

Covers both halves: the unit constants in compat.py and the device-registry
lookup in hub_event_listener.py, each on a "new" and an "old" Home Assistant.
The modules are loaded standalone against stubbed HA modules.
"""
import enum
import importlib.util
import os
import sys
import types

_BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "dirigera_platform")


def _load_compat(const_module):
    saved = {k: sys.modules.get(k) for k in ("homeassistant", "homeassistant.const")}
    ha = types.ModuleType("homeassistant")
    ha.const = const_module
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.const"] = const_module
    try:
        spec = importlib.util.spec_from_file_location("compat_uut", os.path.join(_BASE, "compat.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_units_use_the_new_enums_when_present():
    const = types.ModuleType("homeassistant.const")

    class UnitOfDensity(str, enum.Enum):
        MICROGRAMS_PER_CUBIC_METER = "µg/m³"

    class UnitOfRatio(str, enum.Enum):
        PARTS_PER_MILLION = "ppm"

    const.UnitOfDensity, const.UnitOfRatio = UnitOfDensity, UnitOfRatio
    compat = _load_compat(const)
    assert compat.MICROGRAMS_PER_CUBIC_METER is UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    assert compat.PARTS_PER_MILLION is UnitOfRatio.PARTS_PER_MILLION


def test_units_fall_back_on_older_home_assistant():
    const = types.ModuleType("homeassistant.const")
    const.CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = "µg/m³"
    const.CONCENTRATION_PARTS_PER_MILLION = "ppm"
    compat = _load_compat(const)
    assert compat.MICROGRAMS_PER_CUBIC_METER == "µg/m³"
    assert compat.PARTS_PER_MILLION == "ppm"


def _load_lookup():
    """Pull get_device_by_identifier out of hub_event_listener.py without
    importing the module (it needs websocket, dirigera and HA)."""
    src = open(os.path.join(_BASE, "hub_event_listener.py"), encoding="utf-8").read()
    start = src.index("def get_device_by_identifier")
    end = src.index("\n\n\n", start)
    ns = {}
    exec(src[start:end], ns)
    return ns["get_device_by_identifier"]


class NewRegistry:
    def __init__(self):
        self.calls = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.calls.append(("new", identifier, config_entry_id))
        return "dev"

    def async_get_device(self, identifiers):
        raise AssertionError("deprecated lookup used on a new Home Assistant")


class OldRegistry:
    def __init__(self):
        self.calls = []

    def async_get_device(self, identifiers):
        self.calls.append(("old", identifiers))
        return "dev"


IDENT = ("dirigera_platform", "abc_1")


def test_lookup_uses_the_scoped_method_when_available():
    lookup = _load_lookup()
    reg = NewRegistry()
    assert lookup(reg, IDENT, "entry1") == "dev"
    assert reg.calls == [("new", IDENT, "entry1")]


def test_lookup_falls_back_on_older_home_assistant():
    lookup = _load_lookup()
    reg = OldRegistry()
    assert lookup(reg, IDENT, "entry1") == "dev"
    assert reg.calls == [("old", {IDENT})]


def test_lookup_without_entry_id_uses_the_old_call():
    """Guard: a listener built without an entry id must still find devices."""
    lookup = _load_lookup()
    reg = OldRegistry()
    lookup(reg, IDENT, None)
    assert reg.calls == [("old", {IDENT})]
