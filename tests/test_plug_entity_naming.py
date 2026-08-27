"""
Issue #46: give the plug sensors the Zigbee2MQTT layout.

Requested: the same entity names and icons Zigbee2MQTT uses, and only Energy and
Power enabled by default, so a plug looks the same whether it arrives over Zigbee
or over Dirigera.

Two invariants matter more than the cosmetics and are what these tests actually
protect:

1. id_suffix must NOT change. It is concatenated into unique_id, so touching it
   would orphan every existing entity and throw away its recorder history. A
   rename that quietly took the unique_id with it would look fine in review and
   destroy data on upgrade.

2. Power and Energy must stay enabled by default. The whole point of the change
   is to hide the two noisy readings, not to leave a plug with nothing on it.

entity_registry_enabled_default is only consulted when an entity is registered
for the first time, so existing users keep every sensor they already have; this
only changes what a fresh install (or a newly paired plug) starts with.

homeassistant is not installed here, so the classes are read out of the source
with ast rather than imported.
"""
import ast
import os

SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform", "base_classes.py",
)
with open(SOURCE_PATH) as fh:
    TREE = ast.parse(fh.read())

# class name -> (id_suffix, entity name, icon, enabled by default)
EXPECTED = {
    "current_amps_sensor":                  ("CA01",   "Current",             "mdi:current-ac",     True),
    "current_active_power_sensor":          ("CAP01",  "Power",               "mdi:flash",          True),
    "current_voltage_sensor":               ("CV01",   "Voltage",             "mdi:sine-wave",      True),
    "total_energy_consumed_sensor":         ("TEC01",  "Energy",              "mdi:lightning-bolt", True),
    "energy_consumed_at_last_reset_sensor": ("ELAR01", "Energy at last reset", None,                False),
}


def _class(name):
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError("class %s not found in base_classes.py" % name)


def _super_kwargs(cls_node):
    """The keyword arguments the class passes to its super().__init__()."""
    for node in ast.walk(cls_node):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
                and isinstance(node.func.value, ast.Call)
                and getattr(node.func.value.func, "id", None) == "super"):
            return {
                kw.arg: (kw.value.value if isinstance(kw.value, ast.Constant) else None)
                for kw in node.keywords
            }
    raise AssertionError("no super().__init__ call in %s" % cls_node.name)


def _enabled_by_default(cls_node):
    """False only if the class explicitly opts out."""
    for stmt in cls_node.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = [getattr(t, "id", None) for t in stmt.targets]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [getattr(stmt.target, "id", None)]
            value = stmt.value
        else:
            continue
        if "_attr_entity_registry_enabled_default" in targets:
            return bool(getattr(value, "value", True))
    return True


def test_unique_id_suffixes_are_unchanged():
    """The load-bearing invariant: renaming must not orphan existing entities."""
    for cls_name, (suffix, _, _, _) in EXPECTED.items():
        assert _super_kwargs(_class(cls_name))["id_suffix"] == suffix, (
            "%s changed its id_suffix; that rewrites unique_id and drops the "
            "recorder history of every existing entity" % cls_name
        )


def test_entity_names_follow_the_zigbee2mqtt_layout():
    for cls_name, (_, name, _, _) in EXPECTED.items():
        assert _super_kwargs(_class(cls_name))["name"] == name


def test_icons_follow_the_zigbee2mqtt_layout():
    for cls_name, (_, _, icon, _) in EXPECTED.items():
        if icon is None:
            continue
        assert _super_kwargs(_class(cls_name))["icon"] == icon


def test_only_the_last_reset_helper_is_disabled_by_default():
    """A fresh Silvercrest plug on Zigbee2MQTT shows Energy, Power, Voltage and
    Current, all four enabled and reporting (#46, reporter's own screenshot after
    he corrected his first description). Matching that layout means the only
    sensor we may register disabled is the last-reset bookkeeping value, which
    Z2M has no counterpart for."""
    disabled = {c for c in EXPECTED if not _enabled_by_default(_class(c))}
    assert disabled == {"energy_consumed_at_last_reset_sensor"}, (
        "a plug must start with Energy, Power, Voltage and Current switched on, "
        "and only the last-reset helper off; got disabled=%s" % sorted(disabled)
    )


def test_device_classes_are_untouched_by_the_rename():
    """The rename is cosmetic; the semantics HA keys off must survive it."""
    expected_device_class = {
        "current_amps_sensor": "CURRENT",
        "current_active_power_sensor": "POWER",
        "current_voltage_sensor": "VOLTAGE",
        "total_energy_consumed_sensor": "ENERGY",
        "energy_consumed_at_last_reset_sensor": "ENERGY",
    }
    for cls_name, attr in expected_device_class.items():
        node = _class(cls_name)
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "__init__"):
                for kw in call.keywords:
                    if kw.arg == "device_class":
                        assert kw.value.attr == attr, cls_name
                        break
                else:
                    raise AssertionError("no device_class on %s" % cls_name)
                break
