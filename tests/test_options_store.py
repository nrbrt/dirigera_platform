"""
Issue #61: "Skip Matter over Thread devices" was not kept.

Since #47 the options screen skips pairing when the hub address is unchanged
and the stored token still works; the entry is then written by
OptionsFlowHandler._store(). #49 added CONF_EXCLUDE_MATTER to the pairing
path (async_step_action) but not to _store(), so changing only an option and
saving dropped the setting, and the next visit showed it unticked again.

Both paths must write the same keys, and _store() must write what the user
chose. As in the other tests, the code is lifted out of the source with ast
because importing the package would pull in homeassistant.
"""
import ast
import importlib.util
import os

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)
with open(os.path.join(COMPONENT, "config_flow.py")) as fh:
    TREE = ast.parse(fh.read())

spec = importlib.util.spec_from_file_location("dirigera_const", os.path.join(COMPONENT, "const.py"))
CONST = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CONST)


def _method(class_name, name):
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
                    return stmt
    raise AssertionError("%s.%s not found in config_flow.py" % (class_name, name))


def _keys_written_by_pairing_path():
    """The CONF_* names async_step_action assigns into user_input."""
    keys = set()
    for node in ast.walk(_method("OptionsFlowHandler", "async_step_action")):
        if (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Subscript)
                and getattr(node.targets[0].value, "id", None) == "user_input"):
            keys.add(node.targets[0].slice.id)
    return keys


def _run_store(**attrs):
    fn = _method("OptionsFlowHandler", "_store")
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {name: getattr(CONST, name) for name in dir(CONST) if name.isupper()}
    ns.update(CONF_IP_ADDRESS="ip_address", CONF_TOKEN="token", Dict=dict, Any=object)
    exec(compile(module, "config_flow.py", "exec"), ns)

    written = {}

    class Entries:
        def async_update_entry(self, entry, data, title):
            written.update(data)

    class Hass:
        config_entries = Entries()

    class Flow:
        hass = Hass()
        config_entry = object()

        def async_create_entry(self, title, data):
            return data

    flow = Flow()
    for k, v in attrs.items():
        setattr(flow, k, v)
    ns["_store"](flow, "tok")
    return written


def test_store_keeps_exclude_matter():
    data = _run_store(ip="10.0.0.2", device_set_mode="sets_only", exclude_matter=True)
    assert data.get(CONST.CONF_EXCLUDE_MATTER) is True


def test_store_writes_the_same_keys_as_pairing():
    data = _run_store(ip="10.0.0.2", device_set_mode="sets_only", exclude_matter=False)
    expected = {getattr(CONST, k, None) or {"CONF_IP_ADDRESS": "ip_address", "CONF_TOKEN": "token"}[k]
                for k in _keys_written_by_pairing_path()}
    assert set(data) == expected


# --- The device set mode option ---------------------------------------------


def test_store_keeps_the_device_set_mode():
    data = _run_store(ip="10.0.0.2", device_set_mode="lights_only")
    assert data.get(CONST.CONF_DEVICE_SET_MODE) == "lights_only"


def test_both_paths_write_the_device_set_mode_and_drop_the_checkbox():
    assert "CONF_DEVICE_SET_MODE" in _keys_written_by_pairing_path()
    assert "CONF_HIDE_DEVICE_SET_BULBS" not in _keys_written_by_pairing_path()
    data = _run_store(ip="10.0.0.2", device_set_mode="sets_only")
    assert "hide_device_set_bulbs" not in data

