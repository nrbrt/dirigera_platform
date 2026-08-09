"""
TDD for the coalescing push throttle (issue #44, GRILLPLATS frozen sensors).

Mechanism being fixed: the #40 push throttle DROPPED a state push that arrived
inside the throttle window instead of deferring it. That is fine as long as more
pushes keep coming, because a later one eventually clears the window. It breaks
when the device goes quiet right after a throttled push: switch a metering plug
off and the hub stops sending electricalSensor events, so the last value never
reaches HA. The #36 clamp (0 W while the outlet is off) lives in native_value and
is therefore only applied on a state write, so the sensor freezes on a stale
nonzero reading until the integration is reloaded.

Until v0.3.12 HA's 30s entity poll wrote the cached value regardless and masked
this; fcc3bfc disabled that poll (correctly -- it never fetched from the hub --
but that removed the accidental flush).

Fix under test: a throttled push arms a one-shot trailing flush at the end of the
window. The flush re-opens the window itself, so #40's guarantee -- at most one
recorder write per configured interval -- must still hold.

base_classes.py is loaded standalone; we stub the HA imports so the real throttle
logic runs unchanged.
"""
import importlib.util
import os
import sys
import types


class _PermissiveMeta(type):
    """Metaclass so a fabricated class also answers arbitrary attribute lookups."""

    def __getattr__(cls, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        obj = _PermissiveMeta(name, (), {})
        setattr(cls, name, obj)
        return obj


class _PermissiveModule(types.ModuleType):
    """Stub module that yields a dummy class for ANY non-dunder attribute.

    Enumerating the HA surface name-by-name is brittle: base_classes.py imports
    HomeAssistantError, Entity, SensorEntity, blinds, and more. A missing name
    made the import fail, and the first version of this file then let every test
    return early -- they passed on unfixed code too, which is worse than no test.
    """

    __path__ = []  # marks it as a package so submodule imports resolve

    def __getattr__(self, name):
        # Dunders must keep raising: the import machinery probes __all__ and
        # friends and chokes on a class where it expects a list.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # The synthesised object must work both as a class (HomeAssistantError,
        # SensorEntity) AND as a namespace (core.HomeAssistant), so give it a
        # metaclass that keeps fabricating attributes on demand.
        obj = _PermissiveMeta(name, (), {})
        setattr(self, name, obj)
        return obj


class _StubFinder:
    """Meta-path finder that fabricates any homeassistant.* / dirigera.* module."""

    ROOTS = ("homeassistant", "dirigera", "voluptuous", "websocket", "requests", "urllib3")

    def find_module(self, fullname, path=None):
        return self if fullname.split(".")[0] in self.ROOTS else None

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        mod = _PermissiveModule(fullname)
        mod.__loader__ = self
        sys.modules[fullname] = mod
        return mod


def _load_base_classes():
    """Import base_classes.py standalone, with fabricated HA/dirigera modules."""
    finder = _StubFinder()
    if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, finder)
    for name in [m for m in sys.modules if m.split(".")[0] in _StubFinder.ROOTS]:
        sys.modules.pop(name, None)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg_dir = os.path.join(here, "custom_components", "dirigera_platform")
    # base_classes.py uses relative imports (from .const import ...), so it needs
    # a parent package. Register one whose __path__ points at the integration.
    pkg = types.ModuleType("dp_under_test")
    pkg.__path__ = [pkg_dir]
    sys.modules["dp_under_test"] = pkg
    spec = importlib.util.spec_from_file_location(
        "dp_under_test.base_classes", os.path.join(pkg_dir, "base_classes.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dp_under_test.base_classes"] = module
    spec.loader.exec_module(module)   # deliberately NOT swallowed: a broken
    return module                     # import must fail the test, not skip it


class FakeLoop:
    """Records what the code schedules instead of running a real event loop."""

    def __init__(self):
        self.threadsafe_calls = []
        self.later_calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.threadsafe_calls.append(fn)
        fn(*args)  # run inline so the test can observe call_later

    def call_later(self, delay, fn, *args):
        self.later_calls.append((delay, fn, args))
        return object()


class FakeHass:
    def __init__(self):
        self.loop = FakeLoop()


class FakeListener:
    """Minimal stand-in for a sensor entity registered as a device listener."""

    def __init__(self, throttle=60):
        self.hass = FakeHass()
        self._ha_push_throttle_seconds = throttle
        self.unique_id = "fake_outlet_power"
        self.writes = 0

    def schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1


def _fire_pending(listener):
    """Run whatever the code scheduled via call_later."""
    for _delay, fn, args in list(listener.hass.loop.later_calls):
        fn(*args)
    listener.hass.loop.later_calls.clear()


def test_first_push_is_not_throttled():
    bc = _load_base_classes()
    listener = FakeListener()
    assert bc.ikea_base_device._push_throttled(listener, False) is False


def test_throttled_push_arms_a_trailing_flush():
    """The regression: a dropped push must instead be deferred."""
    bc = _load_base_classes()
    listener = FakeListener()
    bc.ikea_base_device._push_throttled(listener, False)          # opens window
    assert bc.ikea_base_device._push_throttled(listener, False) is True  # throttled
    assert listener.hass.loop.later_calls, "throttled push scheduled no trailing flush"


def test_trailing_flush_writes_state_once():
    """The deferred value must actually land in HA state."""
    bc = _load_base_classes()
    listener = FakeListener()
    bc.ikea_base_device._push_throttled(listener, False)
    bc.ikea_base_device._push_throttled(listener, False)
    assert listener.writes == 0
    _fire_pending(listener)
    assert listener.writes == 1, "trailing flush did not write state"


def test_only_one_flush_pending_at_a_time():
    """#40's write cap: a burst of throttled pushes yields ONE deferred write."""
    bc = _load_base_classes()
    listener = FakeListener()
    bc.ikea_base_device._push_throttled(listener, False)
    for _ in range(10):
        bc.ikea_base_device._push_throttled(listener, False)
    assert len(listener.hass.loop.later_calls) == 1, "burst armed more than one flush"
    _fire_pending(listener)
    assert listener.writes == 1


def test_flush_reopens_the_throttle_window():
    """After flushing, the next push must be throttled again (rate stays capped)."""
    bc = _load_base_classes()
    listener = FakeListener()
    bc.ikea_base_device._push_throttled(listener, False)
    bc.ikea_base_device._push_throttled(listener, False)
    _fire_pending(listener)
    assert bc.ikea_base_device._push_throttled(listener, False) is True


def test_force_refresh_is_never_throttled():
    bc = _load_base_classes()
    listener = FakeListener()
    assert bc.ikea_base_device._push_throttled(listener, True) is False


def test_zero_throttle_disables_the_feature():
    """Interim workaround offered to the reporter: set the throttle to 0."""
    bc = _load_base_classes()
    listener = FakeListener(throttle=0)
    for _ in range(5):
        assert bc.ikea_base_device._push_throttled(listener, False) is False
    assert not listener.hass.loop.later_calls
