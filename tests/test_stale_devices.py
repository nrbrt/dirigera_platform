"""
Stale devices: a device the hub no longer has is found (and, outside a dry
run, removed) by comparing the device registry with the raw /devices list.

Found on a live setup: a TRADFRI bulb removed from the hub in January stayed in
Home Assistant as unavailable for nine months, because deviceRemoved was only
logged and nothing compared the two lists.

The module is loaded standalone with a fake device registry.
"""
import asyncio
import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest


class _FakeRegistry:
    def __init__(self, devices):
        self.devices = {d.id: d for d in devices}
        self.removed = []

    def async_update_device(self, device_id, remove_config_entry_id=None, name_by_user=None):
        # Like HA: detach the entry, delete the device once no entry is left.
        device = self.devices[device_id]
        if name_by_user is not None:
            device.name_by_user = name_by_user
        if remove_config_entry_id is None:
            return
        device.config_entries.discard(remove_config_entry_id)
        if not device.config_entries:
            self.removed.append(device_id)
            del self.devices[device_id]


_registry = {"current": None}
_dr = types.ModuleType("homeassistant.helpers.device_registry")
_dr.DeviceEntry = object
_dr.async_get = lambda hass: _registry["current"]
_dr.async_entries_for_config_entry = lambda reg, entry_id: [
    d for d in reg.devices.values() if entry_id in d.config_entries
]
_ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
sys.modules["homeassistant.helpers.device_registry"] = _dr
_helpers.device_registry = _dr

_path = os.path.join(os.path.dirname(__file__), "..", "custom_components",
                     "dirigera_platform", "stale_devices.py")
_spec = importlib.util.spec_from_file_location("stale_devices_under_test", _path)
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def _device(dev_id, identifier, entry="e1", name=None, name_by_user=None):
    return SimpleNamespace(id=dev_id, identifiers={("dirigera_platform", identifier)},
                           config_entries={entry}, name=name or identifier, name_by_user=name_by_user)


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _Hub:
    def __init__(self, devices=None, error=None):
        self._devices, self._error = devices, error

    def get(self, route):
        assert route == "/devices"
        if self._error:
            raise self._error
        return self._devices


HUB = [
    {"id": "bulb_1", "deviceType": "light", "deviceSet": [{"id": "set-a", "name": "Kitchen"}]},
    {"id": "rel-t_1", "relationId": "rel-t", "deviceType": "environmentSensor"},
    {"id": "plug_1", "deviceType": "outlet", "isReachable": False},
    {"id": "matter_1", "deviceType": "light", "attributes": {"discriminator": -1, "setupCode": ""}},
]


def _run(devices, hub, dry_run):
    _registry["current"] = _FakeRegistry(devices)
    found = asyncio.run(sd.async_remove_stale_devices(_Hass(), "e1", hub, dry_run=dry_run))
    return found, _registry["current"]


def test_known_identifiers_are_the_ones_we_create():
    # A half of a split device is known by its relationId only, like the entities.
    assert sd.known_identifiers(HUB) == {"bulb_1", "set-a", "rel-t", "plug_1", "matter_1"}


@pytest.mark.parametrize("raw", [None, [], {"error": "x"}])
def test_unusable_response_gives_none(raw):
    assert sd.known_identifiers(raw) is None


def _ours():
    return [
        _device("d-bulb", "bulb_1"),
        _device("d-set", "set-a"),          # device set named by a light
        _device("d-split", "rel-t"),        # split device, grouped by relationId
        _device("d-plug", "plug_1"),        # unreachable, still on the hub
        _device("d-matter", "matter_1"),    # on the raw list even if the Matter filter skips it
        _device("d-gone", "gone_1", name="Overloop"),
    ]


def test_dry_run_reports_but_removes_nothing():
    found, reg = _run(_ours(), _Hub(HUB), dry_run=True)
    assert found == ["gone_1"]
    assert reg.removed == []


def test_removal_takes_only_the_device_the_hub_lost():
    found, reg = _run(_ours(), _Hub(HUB), dry_run=False)
    assert found == ["gone_1"]
    assert reg.removed == ["d-gone"]


def test_nothing_stale_nothing_removed():
    found, reg = _run(_ours()[:5], _Hub(HUB), dry_run=False)
    assert found == [] and reg.removed == []


def test_split_device_stays_while_one_half_is_left():
    hub = [{"id": "rel-t_2", "relationId": "rel-t"}]
    found, reg = _run([_device("d-split", "rel-t"), _device("d-x", "x_1")],
                      _Hub(hub), dry_run=False)
    assert "rel-t" not in found
    assert reg.removed == ["d-x"]


@pytest.mark.parametrize("hub", [_Hub([]), _Hub(None), _Hub(error=OSError("unreachable"))])
def test_no_usable_list_removes_nothing(hub):
    found, reg = _run(_ours(), hub, dry_run=False)
    assert found == [] and reg.removed == []


def test_more_than_half_gone_removes_nothing():
    ours = [_device("d-bulb", "bulb_1"), _device("d-a", "a_1"), _device("d-b", "b_1")]
    found, reg = _run(ours, _Hub(HUB), dry_run=False)
    assert found == [] and reg.removed == []


def test_other_config_entry_is_left_alone():
    ours = _ours() + [_device("d-other", "other_1", entry="e2")]
    found, reg = _run(ours, _Hub(HUB), dry_run=False)
    assert reg.removed == ["d-gone"]
    assert "d-other" in reg.devices


def test_user_may_delete_only_a_device_the_hub_lost():
    known = sd.known_identifiers(HUB)
    assert sd.is_stale(_device("d-gone", "gone_1"), known) is True
    assert sd.is_stale(_device("d-bulb", "bulb_1"), known) is False
    assert sd.is_stale(_device("d-gone", "gone_1"), None) is False


def test_device_shared_with_another_entry_only_loses_ours():
    shared = _device("d-gone", "gone_1")
    shared.config_entries.add("e2")
    found, reg = _run(_ours()[:5] + [shared], _Hub(HUB), dry_run=False)
    assert found == ["gone_1"]
    assert reg.removed == []
    assert reg.devices["d-gone"].config_entries == {"e2"}


def test_one_device_setup_can_still_clean_up():
    found, reg = _run([_device("d-gone", "gone_1")], _Hub(HUB), dry_run=False)
    assert reg.removed == ["d-gone"]


def test_device_without_our_identifier_is_never_stale():
    foreign = SimpleNamespace(id="d-f", identifiers={("other", "x")}, config_entries={"e1"},
                              name="f", name_by_user=None)
    assert sd.find_stale_devices([foreign], sd.known_identifiers(HUB)) == []


# Switching from upstream: it created one device per half of a split device,
# identified by the half's id. Reported in #68 (TIMMERFLOTTE, BILRESA, GRILLPLATS).
SPLIT_HUB = [
    {"id": "t_1", "relationId": "t"}, {"id": "t_2", "relationId": "t"},
    {"id": "g_1", "relationId": "g"}, {"id": "g_2", "relationId": "g"},
    {"id": "bulb_1"},
]


def _upstream_leftovers():
    return [
        _device("d-t", "t"), _device("d-g", "g"), _device("d-bulb", "bulb_1"),
        _device("d-t1", "t_1"), _device("d-t2", "t_2"), _device("d-g1", "g_1"),
    ]


def test_upstream_half_devices_are_removed_even_when_more_than_half():
    found, reg = _run(_upstream_leftovers(), _Hub(SPLIT_HUB), dry_run=False)
    assert sorted(reg.removed) == ["d-g1", "d-t1", "d-t2"]
    assert sorted(found) == ["g_1", "t_1", "t_2"]


def test_upstream_half_devices_dry_run_removes_nothing():
    found, reg = _run(_upstream_leftovers(), _Hub(SPLIT_HUB), dry_run=True)
    assert sorted(found) == ["g_1", "t_1", "t_2"]
    assert reg.removed == []


def test_half_device_name_moves_to_the_relation_device():
    ours = _upstream_leftovers()
    ours[3].name_by_user = "Slaapkamer"      # t_1
    ours[4].name_by_user = "Other half"      # t_2, primary half wins
    ours[1].name_by_user = "Mine"            # g already named, keep it
    ours[5].name_by_user = "Plug"            # g_1
    _, reg = _run(ours, _Hub(SPLIT_HUB), dry_run=False)
    assert reg.devices["d-t"].name_by_user == "Slaapkamer"
    assert reg.devices["d-g"].name_by_user == "Mine"


def test_half_device_name_not_moved_in_dry_run():
    ours = _upstream_leftovers()
    ours[3].name_by_user = "Slaapkamer"
    _, reg = _run(ours, _Hub(SPLIT_HUB), dry_run=True)
    assert reg.devices["d-t"].name_by_user is None


def test_gone_guard_still_holds_next_to_leftovers():
    ours = _upstream_leftovers()[:2] + [_device("d-a", "a_1"), _device("d-b", "b_1"),
                                        _device("d-t1", "t_1")]
    found, reg = _run(ours, _Hub(SPLIT_HUB), dry_run=False)
    # a_1 and b_1 are 2 of 5 gone: allowed. Now make the gone ones more than half.
    assert sorted(reg.removed) == ["d-a", "d-b", "d-t1"]
    ours = [_device("d-t", "t"), _device("d-a", "a_1"), _device("d-b", "b_1"),
            _device("d-c", "c_1"), _device("d-t1", "t_1")]
    found, reg = _run(ours, _Hub(SPLIT_HUB), dry_run=False)
    assert reg.removed == ["d-t1"]


def test_user_may_delete_an_upstream_half_device():
    known = sd.known_identifiers(SPLIT_HUB)
    assert sd.is_stale(_device("d-t1", "t_1"), known) is True
    assert sd.is_stale(_device("d-t", "t"), known) is False


def test_device_with_its_own_id_as_relation_id_is_not_superseded():
    hub = [{"id": "x_1", "relationId": "x_1"}]
    assert sd.superseded_identifiers(hub) == {}
    assert sd.known_identifiers(hub) == {"x_1"}
