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
    def __init__(self, entity_id, disabled_by=None):
        self.entity_id = entity_id
        self.disabled_by = disabled_by

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


def _load_sync_function(registry):
    """Exec the real sync_platform_entity_registry against a fake registry."""
    node = next(
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name == "sync_platform_entity_registry"
    )
    er_stub = type("er", (), {
        "async_get": staticmethod(lambda hass: registry),
        "async_entries_for_config_entry": staticmethod(lambda reg, entry_id: reg.entries),
        "RegistryEntryDisabler": _FakeRegistryEntryDisabler,
    })
    logged = []
    ns = {
        "er": er_stub,
        "logger": type("L", (), {"info": staticmethod(lambda *a: logged.append(a))}),
        "set": set,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), INIT, "exec"), ns)
    return ns["sync_platform_entity_registry"]


def _run(entries, selected):
    registry = _FakeRegistry(entries)
    fn = _load_sync_function(registry)
    platforms = [_Platform(name) for name in selected]
    fn(object(), type("E", (), {"entry_id": "abc"})(), platforms)
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
