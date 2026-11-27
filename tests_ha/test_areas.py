"""The area of a device is the user's; it follows the hub only when the hub room changes.

Until 0.4.1 every start put each device back in its IKEA room and created that
area if it was gone (reported on #72). These run the real setup in a real Home
Assistant against the fake hub: a reload is what a restart does to the
registries, and the rooms the integration remembers live in hass_storage, as
they would on disk.
"""
from __future__ import annotations

import pytest
from conftest import FIXTURES, deliver, device_identifier
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pins import ENTRY_DATA, HUB_IP

from custom_components.dirigera_platform.const import DOMAIN, rooms_store_key

FIXTURE = sorted(FIXTURES.glob("*.json"))[0].stem


class Setup:
    """One hub entry in hass, with a device that has a room and is not split."""

    def __init__(self, hass, hub, listeners):
        self.hass, self.hub, self.listeners = hass, hub, listeners
        self.entry = MockConfigEntry(domain=DOMAIN, title="areas", data=dict(ENTRY_DATA), unique_id=HUB_IP)
        self.entry.add_to_hass(hass)
        self.target = next(d for d in hub.devices if d.get("room") and not d.get("relationId"))

    async def start(self) -> None:
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def restart(self) -> None:
        assert await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()

    def device(self):
        # Looked up through the entry: async_get_device is deprecated.
        wanted = device_identifier(self.target)
        found = [d for d in dr.async_entries_for_config_entry(dr.async_get(self.hass), self.entry.entry_id)
                 if wanted in d.identifiers]
        assert len(found) == 1, f"{wanted}: {len(found)} devices"
        return found[0]

    def area_of_device(self) -> str | None:
        area_id = self.device().area_id
        return ar.async_get(self.hass).async_get_area(area_id).name if area_id else None

    def move_in_ha(self, area_name: str) -> None:
        area = ar.async_get(self.hass).async_get_or_create(area_name)
        dr.async_get(self.hass).async_update_device(self.device().id, area_id=area.id)

    def with_target(self, hub_device: dict) -> "Setup":
        """The same setup, looking at another hub device."""
        view = Setup.__new__(Setup)
        view.__dict__.update(self.__dict__, target=hub_device)
        return view

    def area_names(self) -> set[str]:
        return {a.name for a in ar.async_get(self.hass).async_list_areas()}


@pytest.fixture
async def setup(hass, fake_hub, captured_listener) -> Setup:
    s = Setup(hass, fake_hub(FIXTURE), captured_listener)
    await s.start()
    assert s.area_of_device() == s.target["room"]["name"], "a new device starts in its hub room"
    return s


async def test_a_deleted_area_stays_deleted(setup):
    room = setup.target["room"]["name"]
    areas = ar.async_get(setup.hass)
    areas.async_delete(areas.async_get_area_by_name(room).id)
    await setup.restart()
    assert room not in setup.area_names()
    assert setup.area_of_device() is None


async def test_an_area_chosen_in_ha_stays(setup):
    setup.move_in_ha("Chosen in HA")
    await setup.restart()
    assert setup.area_of_device() == "Chosen in HA"


async def test_first_start_of_this_version_moves_nothing(setup, hass_storage):
    """An upgrade has no remembered rooms yet: whatever the user set stays."""
    setup.move_in_ha("Chosen before the upgrade")
    hass_storage.pop(rooms_store_key(setup.entry.entry_id), None)
    await setup.restart()
    assert setup.area_of_device() == "Chosen before the upgrade"


async def test_a_room_changed_on_the_hub_while_stopped_moves_the_device(setup):
    setup.move_in_ha("Chosen in HA")
    assert await setup.hass.config_entries.async_unload(setup.entry.entry_id)
    await setup.hass.async_block_till_done()
    setup.target["room"] = {**setup.target["room"], "id": "room-moved", "name": "Moved in the IKEA app"}
    await setup.start()
    assert setup.area_of_device() == "Moved in the IKEA app"


async def test_a_live_room_change_moves_the_device_and_is_remembered(setup):
    setup.move_in_ha("Chosen in HA")
    moved = {**setup.target, "room": {**setup.target["room"], "id": "room-live", "name": "Moved live"}}
    setup.target.update(moved)
    deliver(setup.listeners[-1], {"type": "deviceStateChanged", "data": moved})
    await setup.hass.async_block_till_done()
    assert setup.area_of_device() == "Moved live"

    # The user moves it again in HA; the live room was remembered, so the
    # restart right after does not take the device back to it.
    setup.move_in_ha("Chosen again")
    await setup.restart()
    assert setup.area_of_device() == "Chosen again"


async def test_new_devices_are_placed_without_suggested_area(hass, fake_hub, captured_listener, monkeypatch):
    """The placement must not depend on which part of a split device registers
    first: with suggested_area gone altogether, every device still starts in
    its hub room (over all its parts)."""
    real = dr.DeviceRegistry.async_get_or_create

    def without_suggested_area(self, *args, **kwargs):
        kwargs.pop("suggested_area", None)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(dr.DeviceRegistry, "async_get_or_create", without_suggested_area)
    s = Setup(hass, fake_hub(FIXTURE), captured_listener)
    await s.start()
    rooms_by_device: dict[tuple, set[str]] = {}
    for d in s.hub.devices:
        if d.get("room"):
            rooms_by_device.setdefault(device_identifier(d), set()).add(d["room"]["name"])
    areas = ar.async_get(hass)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), s.entry.entry_id)
    checked = 0
    for device in devices:
        for ident in device.identifiers & rooms_by_device.keys():
            assert device.area_id is not None, f"{ident} has no area"
            assert areas.async_get_area(device.area_id).name in rooms_by_device[ident]
            checked += 1
    assert checked >= 10, f"only {checked} devices with a room were checked"


async def test_a_device_missing_from_the_stored_rooms_is_placed_at_the_next_start(setup, hass_storage):
    """How a device paired while HA ran is treated: it may come up without an
    area (a split device whose part without a room registered first) and is
    not in the stored rooms, so the next start places it. This drives the
    store rule, not runtime discovery itself."""
    stored = hass_storage[rooms_store_key(setup.entry.entry_id)]["data"]["devices"]
    stored.pop(device_identifier(setup.target)[1])
    dr.async_get(setup.hass).async_update_device(setup.device().id, area_id=None)
    await setup.restart()
    assert setup.area_of_device() == setup.target["room"]["name"]


def _rename_room(hub, room_id: str, name: str) -> list[dict]:
    """Rename a room on the fake hub; returns its devices."""
    devices = [d for d in hub.devices if (d.get("room") or {}).get("id") == room_id]
    for d in devices:
        d["room"] = {**d["room"], "name": name}
    return devices


async def test_a_renamed_room_takes_along_only_the_devices_still_in_its_area(setup):
    """Same room id, new name, live and while stopped: a device still in the
    area named after the room follows to an area with the new name; a device
    the user put in another area stays there."""
    by_room: dict[str, list[dict]] = {}
    for d in setup.hub.devices:
        if d.get("room") and not d.get("relationId"):
            by_room.setdefault(d["room"]["id"], []).append(d)
    room_id, (first, second, *_) = next((r, ds) for r, ds in by_room.items() if len(ds) >= 2)
    follower, placed_by_user = setup.with_target(first), setup.with_target(second)
    placed_by_user.move_in_ha("Chosen in HA")

    # Only the follower's event comes in: the rename is the room's, not the device's.
    _rename_room(setup.hub, room_id, "Renamed live")
    deliver(setup.listeners[-1], {"type": "deviceStateChanged", "data": first})
    await setup.hass.async_block_till_done()
    assert follower.area_of_device() == "Renamed live"
    assert placed_by_user.area_of_device() == "Chosen in HA"

    # A later event without the room, from a room-mate whose model still has
    # the old name, is no rename back.
    for d in (second, first):
        deliver(setup.listeners[-1], {"type": "deviceStateChanged", "data": {
            "id": d["id"], "type": d["type"], "deviceType": d["deviceType"],
            "isReachable": d.get("isReachable", True)}})
        await setup.hass.async_block_till_done()
        assert follower.area_of_device() == "Renamed live"

    assert await setup.hass.config_entries.async_unload(setup.entry.entry_id)
    await setup.hass.async_block_till_done()
    _rename_room(setup.hub, room_id, "Renamed while stopped")
    await setup.start()
    assert follower.area_of_device() == "Renamed while stopped"
    assert placed_by_user.area_of_device() == "Chosen in HA"


def _stored(setup, hass_storage) -> dict:
    """The stored room per device identifier."""
    return hass_storage[rooms_store_key(setup.entry.entry_id)]["data"]["devices"]


async def test_an_upgrade_does_not_bring_a_deleted_area_back(setup, hass_storage):
    """The reporter's own case at the upgrade: an area deleted in HA, and no
    stored rooms yet. Nothing is placed or moved."""
    room = setup.target["room"]["name"]
    areas = ar.async_get(setup.hass)
    areas.async_delete(areas.async_get_area_by_name(room).id)
    hass_storage.pop(rooms_store_key(setup.entry.entry_id))
    await setup.restart()
    assert room not in setup.area_names()
    assert setup.area_of_device() is None


@pytest.mark.parametrize("damage", ["not a mapping", "newer version"])
async def test_unreadable_stored_rooms_move_nothing(setup, hass_storage, damage):
    """Data that is not a mapping, or a store a later version wrote (after a
    downgrade, which Store refuses to load): nothing is placed or moved."""
    room = setup.target["room"]["name"]
    areas = ar.async_get(setup.hass)
    areas.async_delete(areas.async_get_area_by_name(room).id)
    store = hass_storage[rooms_store_key(setup.entry.entry_id)]
    if damage == "newer version":
        store["version"] = 99
    else:
        store["data"] = ["not", "a", "mapping"]
    await setup.restart()
    assert room not in setup.area_names()
    assert setup.area_of_device() is None


async def test_a_device_gone_from_the_registry_is_forgotten(setup, hass_storage):
    """Removed from the hub and from Home Assistant: its stored room goes too."""
    identifier = device_identifier(setup.target)[1]
    assert identifier in _stored(setup, hass_storage)
    setup.hub.devices.remove(setup.target)
    dr.async_get(setup.hass).async_remove_device(setup.device().id)
    await setup.restart()
    assert identifier not in _stored(setup, hass_storage)


async def test_a_device_missing_from_the_hub_for_a_while_keeps_its_room(setup, hass_storage):
    """A device the hub leaves out of /devices for a while stays in the
    registry (the stale-device check only logs), so its stored room stays,
    and it is not new when it comes back: an area the user cleared stays
    clear."""
    identifier = device_identifier(setup.target)[1]
    dr.async_get(setup.hass).async_update_device(setup.device().id, area_id=None)
    setup.hub.devices.remove(setup.target)
    await setup.restart()
    assert identifier in _stored(setup, hass_storage)
    setup.hub.devices.append(setup.target)
    await setup.restart()
    assert setup.area_of_device() is None


async def test_removing_the_entry_deletes_its_stored_rooms(setup, hass_storage):
    key = rooms_store_key(setup.entry.entry_id)
    assert key in hass_storage
    assert await setup.hass.config_entries.async_remove(setup.entry.entry_id)
    await setup.hass.async_block_till_done()
    assert key not in hass_storage


async def test_a_device_moved_into_a_renamed_room_brings_the_rename_along(setup):
    """Within one reconnect gap a room is renamed and another device moved into
    it; the moved device's event may come first. The room's devices still in
    its old-named area follow the rename too."""
    by_room: dict[str, list[dict]] = {}
    for d in setup.hub.devices:
        if d.get("room") and not d.get("relationId"):
            by_room.setdefault(d["room"]["id"], []).append(d)
    room_id, (stays, *_) = next((r, ds) for r, ds in by_room.items() if len(ds) >= 2)
    mover = next(ds[0] for r, ds in by_room.items() if r != room_id)
    _rename_room(setup.hub, room_id, "Renamed")
    mover["room"] = dict(stays["room"])
    deliver(setup.listeners[-1], {"type": "deviceStateChanged", "data": mover})
    await setup.hass.async_block_till_done()
    assert setup.with_target(mover).area_of_device() == "Renamed"
    assert setup.with_target(stays).area_of_device() == "Renamed"
