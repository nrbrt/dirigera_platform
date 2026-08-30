"""
Issue #49: do not import the devices the hub commissioned over Matter.

The fixture is a real /devices response from a DIRIGERA on firmware 26.4.x,
23 devices, reduced to the fields this filter looks at and with the ids
replaced. 14 of them carry the Matter commissioning attributes and 9 do not,
which is the split the filter has to reproduce.

Three things are guarded here.

1. The default. A config entry created before this option has no key at all,
   and those users must keep seeing all 23 devices. This is the property that
   makes the feature safe to ship, so it is tested rather than assumed.

2. Fail-closed. A device is dropped only when the marker is positively present.
   An unrecognised device keeps being imported: one duplicate the user disables
   by hand is a much better failure than a device that silently disappears.

3. The single device path. Runtime discovery fetches one device at a time via
   /devices/<id>, which never passes through the list filter, so it asks
   is_excluded_matter_device() instead. If that regressed, new Matter devices
   would still appear after startup, which is exactly what the reporter asked
   us to prevent.

As in test_platform_filtering.py, the functions under test are lifted out of
the source with ast and executed against a stub, because importing the package
would pull in dirigera and homeassistant. The real bodies run; no copy of the
logic lives in this file.
"""
import ast
import importlib.util
import json
import os

import pytest

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
PATCH = os.path.join(COMPONENT, "dirigera_lib_patch.py")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_hub_devices.json")


def _load_const():
    spec = importlib.util.spec_from_file_location(
        "dirigera_const", os.path.join(COMPONENT, "const.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONST = _load_const()

with open(PATCH) as fh:
    TREE = ast.parse(fh.read())


def _lift(name, class_name=None):
    """Pull one function out of dirigera_lib_patch.py and compile just that."""
    if class_name is None:
        nodes = [n for n in TREE.body
                 if isinstance(n, ast.FunctionDef) and n.name == name]
    else:
        cls = next(n for n in TREE.body
                   if isinstance(n, ast.ClassDef) and n.name == class_name)
        nodes = [n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == name]
    assert nodes, f"{name} not found in dirigera_lib_patch.py"
    namespace = {"MATTER_ATTRIBUTE_KEYS": CONST.MATTER_ATTRIBUTE_KEYS}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), PATCH, "exec"), namespace)
    return namespace[name], namespace


IS_MATTER, NAMESPACE = _lift("is_matter_device")
WITHOUT_MATTER, _ = _lift("_without_matter", "HubX")
IS_EXCLUDED, _ = _lift("is_excluded_matter_device", "HubX")

# _without_matter calls the module-level is_matter_device, so it needs to be
# resolvable from the lifted function's globals.
WITHOUT_MATTER.__globals__["is_matter_device"] = IS_MATTER
IS_EXCLUDED.__globals__["is_matter_device"] = IS_MATTER


class StubHub:
    def __init__(self, exclude_matter):
        self._exclude_matter = exclude_matter

    _without_matter = WITHOUT_MATTER
    is_excluded_matter_device = IS_EXCLUDED


@pytest.fixture
def devices():
    with open(FIXTURE) as fh:
        return json.load(fh)


def test_fixture_is_the_hub_we_measured(devices):
    """Guards the fixture itself: 23 devices, 14 of them marked."""
    assert len(devices) == 23
    assert sum(1 for d in devices if IS_MATTER(d)) == 14


def test_marker_splits_matter_from_zigbee(devices):
    marked = {d["attributes"]["model"] for d in devices if IS_MATTER(d)}
    unmarked = {d["attributes"]["model"] for d in devices if not IS_MATTER(d)}

    for model in ("MYGGSPRAY", "MYGGBETT", "KLIPPBOK", "ALPSTUGA", "BILRESA"):
        assert any(m.startswith(model) for m in marked), f"{model} should be Matter"
    for model in ("TRADFRI", "RODRET", "TRETAKT", "DIRIGERA"):
        assert any(m.startswith(model) for m in unmarked), f"{model} should be Zigbee"

    assert not marked & unmarked


def test_default_imports_everything(devices):
    """No option set means no behaviour change, for every entry that exists today."""
    assert StubHub(exclude_matter=False)._without_matter(devices) == devices


def test_enabled_drops_only_the_marked_ones(devices):
    kept = StubHub(exclude_matter=True)._without_matter(devices)
    assert len(kept) == 9
    assert not any(IS_MATTER(d) for d in kept)
    # The devices that survive are untouched, not rebuilt.
    assert all(d in devices for d in kept)


@pytest.mark.parametrize("attributes", [
    {},
    {"model": "SOMETHING NEW", "manufacturer": "IKEA of Sweden"},
    {"discriminatorish": -1},
])
def test_fails_closed_on_unknown_shapes(attributes):
    """Anything we cannot positively identify as Matter keeps being imported."""
    device = {"id": "x", "attributes": attributes}
    assert IS_MATTER(device) is False
    assert StubHub(exclude_matter=True)._without_matter([device]) == [device]


def test_missing_attributes_key_does_not_raise():
    """The hub has returned devices without attributes before; do not crash."""
    assert IS_MATTER({"id": "x"}) is False
    assert IS_MATTER({"id": "x", "attributes": None}) is False


@pytest.mark.parametrize("key", ["discriminator", "qrCode", "setupCode"])
def test_any_single_marker_key_is_enough(key):
    """The hub sends all three together, but one is sufficient to identify."""
    assert IS_MATTER({"attributes": {key: -1}}) is True


def test_sentinel_values_do_not_matter(devices):
    """Presence is the signal: the hub blanks the values out."""
    marked = [d for d in devices if IS_MATTER(d)]
    assert marked, "fixture should contain Matter devices"
    for device in marked:
        assert device["attributes"]["discriminator"] == -1
        assert device["attributes"]["qrCode"] == ""
        assert device["attributes"]["setupCode"] == ""


def test_single_device_path_respects_the_option(devices):
    """Runtime discovery bypasses the list filter and asks this instead."""
    matter = next(d for d in devices if IS_MATTER(d))
    zigbee = next(d for d in devices if not IS_MATTER(d))

    on = StubHub(exclude_matter=True)
    assert on.is_excluded_matter_device(matter) is True
    assert on.is_excluded_matter_device(zigbee) is False

    off = StubHub(exclude_matter=False)
    assert off.is_excluded_matter_device(matter) is False


def test_non_list_responses_pass_through():
    """get() routes a single device through the same helper; do not mangle it."""
    single = {"id": "x", "attributes": {"discriminator": -1}}
    assert StubHub(exclude_matter=True)._without_matter(single) is single
    assert StubHub(exclude_matter=True)._without_matter(None) is None
