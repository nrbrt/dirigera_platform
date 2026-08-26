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
