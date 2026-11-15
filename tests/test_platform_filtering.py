"""
Issue #47: only set up the platforms the user selected.

Two things are worth guarding here, and the second one is the reason this file
exists at all.

1. resolve_platforms() must be backward compatible. Every config entry created
   before this option has no key at all, and those users must keep getting all
   seven platforms.

2. async_unload_entry() must unload what was LOADED, not what the options say
   right now. Changing the option triggers a reload: unload runs first, and by
   then entry.data already holds the new selection. If unload re-resolved from
   there it would skip exactly the platform the user just switched off, and its
   entities would survive as orphans that nothing owns. That is a silent leak,
   invisible in logs, so it gets a structural test rather than trust.

The module graph of __init__.py pulls in dirigera, voluptuous and homeassistant,
none of which are installed here, so the function under test is lifted out of the
source with ast and executed against a stub Platform. That runs the REAL body -
no copy of the logic lives in this file.
"""
import ast
import importlib.util
import os

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
INIT = os.path.join(COMPONENT, "__init__.py")


def _load_const():
    """Load const.py by path: importing the package would pull in dirigera."""
    spec = importlib.util.spec_from_file_location(
        "dirigera_const", os.path.join(COMPONENT, "const.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

with open(INIT) as fh:
    SOURCE = fh.read()
TREE = ast.parse(SOURCE)


class _Platform(str):
    """Stand-in for homeassistant.const.Platform, which is a StrEnum."""

    @property
    def value(self):
        return str(self)


def _load_resolve_platforms():
    """Exec the real PLATFORMS_TO_SETUP + resolve_platforms against a stub."""
    const = _load_const()
    CONF_ENABLED_PLATFORMS = const.CONF_ENABLED_PLATFORMS
    ALL_PLATFORMS = const.ALL_PLATFORMS

    wanted = ("PLATFORMS_TO_SETUP",)
    nodes = [
        n for n in TREE.body
        if (isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) in wanted for t in n.targets))
        or (isinstance(n, ast.FunctionDef) and n.name == "resolve_platforms")
    ]
    assert len(nodes) == 2, "expected PLATFORMS_TO_SETUP and resolve_platforms in __init__.py"

    ns = {
        "Platform": type("P", (), {
            name.upper(): _Platform(name) for name in ALL_PLATFORMS
        }),
        "CONF_ENABLED_PLATFORMS": CONF_ENABLED_PLATFORMS,
        "list": list,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), INIT, "exec"), ns)
    return ns["resolve_platforms"], ns["PLATFORMS_TO_SETUP"], CONF_ENABLED_PLATFORMS


resolve_platforms, ALL, KEY = _load_resolve_platforms()


def test_missing_key_keeps_every_platform():
    """A config entry from before this option must not lose entities."""
    assert resolve_platforms({}) == ALL
    assert resolve_platforms({"ip_address": "1.2.3.4"}) == ALL


def test_empty_selection_falls_back_to_everything():
    """An entry that sets up nothing is a broken entry, not a valid choice."""
    assert resolve_platforms({KEY: []}) == ALL


def test_scenes_only_is_the_issue_47_case():
    """The reporter runs Matter alongside and wants scenes only."""
    result = resolve_platforms({KEY: ["scene"]})
    assert [p.value for p in result] == ["scene"]


def test_selection_keeps_the_canonical_order():
    """Order must follow PLATFORMS_TO_SETUP, not whatever the UI hands back."""
    result = resolve_platforms({KEY: ["scene", "switch", "light"]})
    assert [p.value for p in result] == ["switch", "light", "scene"]


def test_unknown_platform_names_are_ignored():
    """A stale or hand-edited entry must not crash setup."""
    result = resolve_platforms({KEY: ["switch", "does_not_exist"]})
    assert [p.value for p in result] == ["switch"]


def _function_source(name):
    for node in TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node)
    raise AssertionError("%s not found in __init__.py" % name)


def test_unload_does_not_use_the_global_platform_list():
    """
    The regression guard. async_unload_entry must not reach for
    PLATFORMS_TO_SETUP: with an option in play that constant is no longer what
    was loaded, and unloading the wrong set leaves orphaned entities behind.
    """
    src = _function_source("async_unload_entry")
    assert "PLATFORMS_TO_SETUP" not in src, (
        "async_unload_entry unloads the hardcoded platform list; it must unload "
        "the list stored on the entry at setup time"
    )
    assert "async_unload_platforms" in src


def test_setup_stores_the_resolved_list_on_the_entry():
    """Unload can only read it back if setup put it there."""
    src = _function_source("async_setup_entry")
    assert 'hass.data[DOMAIN][entry.entry_id]["platforms"]' in src
    assert "async_forward_entry_setups(entry, platforms)" in src


# --- #47 follow-up: deselected platforms must not leave ghost entities -------
#
# Measured on a live hub before this was added: switching off a platform left
# its entity in the registry as unavailable with restored=True, so it still
# showed up in every picker. That is the exact complaint in the issue, so the
# filter is only useful if it hides them too.


class _FakeRegistryEntryDisabler:
    INTEGRATION = "integration"
    USER = "user"


class _FakeEntry:
    def __init__(self, entity_id, disabled_by=None, device_id=None, unique_id=None):
        self.entity_id = entity_id
        self.disabled_by = disabled_by
        self.device_id = device_id
        self.unique_id = unique_id or entity_id.split(".", 1)[1]

    @property
    def domain(self):
        return self.entity_id.split(".")[0]


class _FakeRegistry:
    def __init__(self, entries):
        self.entries = entries
        self.writes = []

    def async_update_entity(self, entity_id, **changes):
        self.writes.append((entity_id, changes.get("disabled_by")))
        for e in self.entries:
            if e.entity_id == entity_id:
                e.disabled_by = changes.get("disabled_by")


class _FakeDeviceEntryDisabler:
    INTEGRATION = "integration"
    USER = "user"


class _FakeDevice:
    def __init__(self, device_id, disabled_by=None, model=None):
        self.id = device_id
        self.disabled_by = disabled_by
        self.model = model


class _FakeDeviceRegistry:
    def __init__(self, devices):
        self.devices = devices
        self.writes = []

    def async_update_device(self, device_id, **changes):
        self.writes.append((device_id, changes.get("disabled_by")))
        for d in self.devices:
            if d.id == device_id:
                d.disabled_by = changes.get("disabled_by")


def _load_sync_function(registry):
    """Exec the real sync_platform_entity_registry against a fake registry."""
    names = ("sync_platform_entity_registry", "_sync_device_registry")
    nodes = [
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    assert len(nodes) == 2, "expected both sync functions in __init__.py"
    er_stub = type("er", (), {
        "async_get": staticmethod(lambda hass: registry),
        "async_entries_for_config_entry": staticmethod(lambda reg, entry_id: reg.entries),
        "RegistryEntryDisabler": _FakeRegistryEntryDisabler,
    })
    device_registry = getattr(registry, "device_registry", None) or _FakeDeviceRegistry([])
    dr_stub = type("dr", (), {
        "async_get": staticmethod(lambda hass: device_registry),
        "async_entries_for_config_entry": staticmethod(
            lambda reg, entry_id: reg.devices
        ),
        "DeviceEntryDisabler": _FakeDeviceEntryDisabler,
    })
    er_stub.async_entries_for_device = staticmethod(
        lambda reg, device_id, include_disabled_entities=False: [
            e for e in reg.entries if getattr(e, "device_id", None) == device_id
        ]
    )
    logged = []
    ns = {
        "er": er_stub,
        "dr": dr_stub,
        "any": any,
        "tuple": tuple,
        "logger": type("L", (), {"info": staticmethod(lambda *a: logged.append(a))}),
        "set": set,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), INIT, "exec"), ns)
    return ns["sync_platform_entity_registry"]


def _run(entries, selected, devices=None, hidden=None):
    registry = _FakeRegistry(entries)
    registry.device_registry = _FakeDeviceRegistry(devices or [])
    fn = _load_sync_function(registry)
    platforms = [_Platform(name) for name in selected]
    fn(object(), type("E", (), {"entry_id": "abc"})(), platforms, hidden or frozenset())
    return registry


def test_deselected_platform_entities_are_disabled():
    reg = _run([_FakeEntry("switch.plug"), _FakeEntry("light.lamp")], ["light"])
    assert reg.writes == [("switch.plug", "integration")]
    assert reg.entries[1].disabled_by is None, "a selected platform must be left alone"


def test_reselecting_a_platform_re_enables_what_we_disabled():
    reg = _run([_FakeEntry("switch.plug", "integration")], ["switch", "light"])
    assert reg.writes == [("switch.plug", None)]


def test_a_user_disabled_entity_is_never_touched():
    """
    The important one. Re-enabling everything on the way back would silently
    undo a deliberate user choice, and they would have no idea why.
    """
    entries = [_FakeEntry("switch.plug", "user"), _FakeEntry("light.lamp", "user")]
    reg = _run(entries, ["switch", "light"])
    assert reg.writes == [], "user-disabled entities must survive a reselect"
    reg = _run(entries, ["light"])
    assert reg.writes == [], "and must not be re-stamped as integration-disabled"


def test_running_twice_writes_nothing_the_second_time():
    """Idempotent, so a plain restart fires no registry events."""
    entries = [_FakeEntry("switch.plug"), _FakeEntry("light.lamp")]
    first = _run(entries, ["light"])
    assert len(first.writes) == 1
    second = _run(entries, ["light"])
    assert second.writes == []


def test_setup_hides_ghosts_before_forwarding_platforms():
    """
    Order matters: a just-reselected platform has to be re-enabled before its
    setup runs, or the entities stay missing until the next reload.
    """
    src = _function_source("async_setup_entry")
    assert src.index("sync_platform_entity_registry(") < src.index("async_forward_entry_setups(")


# --- #47, tweede ronde: de klacht ging over DEVICES, niet over entiteiten -----
#
# De entiteiten uitzetten was niet genoeg en de melder liet dat ook zien: hij
# moest alle apparaten met de hand uit de integratie verwijderen voor het filter
# effect had. Home Assistant houdt een device in het register zolang het
# entiteiten heeft, uitgeschakeld of niet, dus een weggefilterde stekker stond
# nog gewoon in de apparatenlijst. En juist dubbele APPARATEN naast Matter zijn
# waar de issue over gaat.


def test_a_device_with_only_deselected_entities_is_disabled():
    """De kern van de klacht: het apparaat zelf moet uit de lijst verdwijnen."""
    reg = _run(
        [_FakeEntry("switch.plug", device_id="dev1")],
        ["scene"],
        devices=[_FakeDevice("dev1")],
    )
    assert reg.device_registry.writes == [("dev1", "integration")]


def test_a_device_spanning_two_platforms_survives_losing_one():
    """Een stekker levert switch EN sensor. Sensors uitzetten mag de stekker
    niet meenemen, anders verliest de gebruiker het apparaat dat hij wilde
    houden. Dit is de fout die het makkelijkst te maken is."""
    reg = _run(
        [
            _FakeEntry("switch.plug", device_id="dev1"),
            _FakeEntry("sensor.plug_energy", device_id="dev1"),
        ],
        ["switch"],
        devices=[_FakeDevice("dev1")],
    )
    assert reg.device_registry.writes == [], "device met een geselecteerd platform moet blijven"
    assert reg.writes == [("sensor.plug_energy", "integration")], "alleen de sensor gaat uit"


def test_reselecting_brings_the_device_back():
    reg = _run(
        [_FakeEntry("switch.plug", device_id="dev1")],
        ["switch"],
        devices=[_FakeDevice("dev1", "integration")],
    )
    assert reg.device_registry.writes == [("dev1", None)]


def test_a_user_disabled_device_is_never_touched():
    """Wat de gebruiker zelf heeft uitgezet blijft van hem."""
    reg = _run(
        [_FakeEntry("switch.plug", device_id="dev1")],
        ["scene"],
        devices=[_FakeDevice("dev1", "user")],
    )
    assert reg.device_registry.writes == []


def test_a_device_without_entities_is_left_alone():
    """Geen entiteiten betekent geen grond om te oordelen; niet aanraken."""
    reg = _run([], ["scene"], devices=[_FakeDevice("dev_leeg")])
    assert reg.device_registry.writes == []


def test_running_twice_writes_no_device_the_second_time():
    """Een setup die niets verandert mag geen registerschrijfacties doen."""
    entries = [_FakeEntry("switch.plug", device_id="dev1")]
    devices = [_FakeDevice("dev1")]
    registry = _FakeRegistry(entries)
    registry.device_registry = _FakeDeviceRegistry(devices)
    fn = _load_sync_function(registry)
    platforms = [_Platform("scene")]
    entry = type("E", (), {"entry_id": "abc"})()
    fn(object(), entry, platforms)
    first = list(registry.device_registry.writes)
    fn(object(), entry, platforms)
    assert registry.device_registry.writes == first, "tweede run moet niets schrijven"


# --- The device set mode leaves entities out within the light platform -----
#
# Both filters use disabled_by INTEGRATION, so they run as one pass. Two passes
# would undo each other on every start: the platform filter re-enabling the
# lights the set mode disabled, which also makes Home Assistant reload the
# entry 30 seconds later.


def test_a_hidden_light_is_disabled_with_its_device():
    """Sets only: a light inside a set goes, and so does its device."""
    reg = _run(
        [
            _FakeEntry("light.spot", device_id="dev_spot", unique_id="bulb-1"),
            _FakeEntry("light.spots_set", device_id="dev_set", unique_id="set-1"),
        ],
        ["light"],
        devices=[_FakeDevice("dev_spot"), _FakeDevice("dev_set")],
        hidden={"bulb-1"},
    )
    assert reg.writes == [("light.spot", "integration")]
    assert reg.device_registry.writes == [("dev_spot", "integration")]


def test_a_light_the_mode_takes_back_is_re_enabled():
    reg = _run(
        [_FakeEntry("light.spot", "integration", device_id="dev_spot", unique_id="bulb-1")],
        ["light"],
        devices=[_FakeDevice("dev_spot", "integration")],
        hidden=set(),
    )
    assert reg.writes == [("light.spot", None)]
    assert reg.device_registry.writes == [("dev_spot", None)]


def test_the_platform_filter_does_not_re_enable_a_hidden_light():
    """The light platform is selected, but the mode still leaves this one out."""
    entries = [_FakeEntry("light.spot", "integration", unique_id="bulb-1")]
    registry = _FakeRegistry(entries)
    registry.device_registry = _FakeDeviceRegistry([])
    fn = _load_sync_function(registry)
    entry = type("E", (), {"entry_id": "abc"})()
    for _ in range(2):
        fn(object(), entry, [_Platform("light")], {"bulb-1"})
    assert registry.writes == [], "no churn between the two filters"


def test_a_user_disabled_light_stays_off_whatever_the_mode():
    reg = _run(
        [_FakeEntry("light.spot", "user", unique_id="bulb-1")],
        ["light"],
        hidden=set(),
    )
    assert reg.writes == []


def test_hidden_ids_only_apply_within_selected_platforms():
    """A deselected platform hides everything on it, hidden ids or not."""
    reg = _run(
        [_FakeEntry("light.lamp", unique_id="bulb-2"), _FakeEntry("switch.plug")],
        ["switch"],
        hidden={"bulb-1"},
    )
    assert reg.writes == [("light.lamp", "integration")]


def test_setup_hands_the_set_mode_to_the_registry_sync():
    src = _function_source("async_setup_entry")
    assert "hidden_unique_ids(" in src and "resolve_device_set_mode(" in src
    assert src.index("hidden_unique_ids(") < src.index("sync_platform_entity_registry(")


def test_hidden_ids_only_hide_lights():
    """A non-light entity that happens to share a hidden id stays enabled."""
    reg = _run(
        [_FakeEntry("light.spot", unique_id="bulb-1"), _FakeEntry("sensor.spot", unique_id="bulb-1")],
        ["light", "sensor"],
        hidden={"bulb-1"},
    )
    assert reg.writes == [("light.spot", "integration")]


def _load_device_set_entity_ids(registry):
    """Exec the real _device_set_entity_ids against the fake registries."""
    node = next(
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name == "_device_set_entity_ids"
    )
    ns = {
        "er": type("er", (), {
            "async_get": staticmethod(lambda hass: registry),
            "async_entries_for_device": staticmethod(
                lambda reg, device_id, include_disabled_entities=False: [
                    e for e in reg.entries if e.device_id == device_id
                ]
            ),
        }),
        "dr": type("dr", (), {
            "async_get": staticmethod(lambda hass: registry.device_registry),
            "async_entries_for_config_entry": staticmethod(lambda reg, entry_id: reg.devices),
        }),
        "DEVICE_SET_MODEL": _load_const().DEVICE_SET_MODEL,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), INIT, "exec"), ns)
    return ns["_device_set_entity_ids"]


def test_lights_only_finds_set_entities_the_hub_no_longer_has():
    """Sets are recognised by their device's model, so a set that disappeared
    while Home Assistant was stopped is still hidden under Lights only."""
    registry = _FakeRegistry([
        _FakeEntry("light.gone_set", "integration", device_id="dev_set", unique_id="set-gone"),
        _FakeEntry("light.spot", device_id="dev_spot", unique_id="bulb-1"),
    ])
    registry.device_registry = _FakeDeviceRegistry([
        _FakeDevice("dev_set", "integration", model="Device Set"),
        _FakeDevice("dev_spot", model="TRADFRI bulb"),
    ])
    fn = _load_device_set_entity_ids(registry)
    assert fn(object(), type("E", (), {"entry_id": "abc"})()) == {"set-gone"}


def test_setup_hides_every_set_entity_under_lights_only():
    src = _function_source("async_setup_entry")
    assert "_device_set_entity_ids(" in src
    assert src.index("_device_set_entity_ids(") < src.index("sync_platform_entity_registry(")
