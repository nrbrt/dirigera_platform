"""Remove devices the hub no longer has.

Home Assistant's quality-scale rule ``stale-devices``: when a device is removed
from the hub, the integration removes it from Home Assistant too, but only when
it is sure the device is gone. The hub's ``/devices`` returns the full list, so
a device whose identifier is on none of its entries is gone.

The comparison uses the raw ``/devices`` response from the plain library Hub,
not the filtered one HubX builds entities from. Devices that exist but are not
imported on purpose (a deselected platform, the Matter filter) are on that
list, so they are never stale. Deselected platforms keep being disabled by
sync_platform_entity_registry instead.

What counts is the identifier this integration gives a device, the same rule as
``device_identifier``: the relationId of a split device, otherwise its id. A
device identified by the id of one half of a split device is superseded: we no
longer create it, its entities sit on the relationId device. Upstream created
such a device per half, so a switch from upstream leaves them behind empty.

DRY_RUN: this release only logs what it would remove. Removal follows once
users have reported what the log shows on their hubs.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from homeassistant.helpers import device_registry as dr

logger = logging.getLogger("custom_components.dirigera_platform.stale_devices")

DOMAIN_KEY = "dirigera_platform"
DRY_RUN = True
# Never remove more than this share of our devices in one run: a hub after a
# factory reset, or a different hub on the same address, must not wipe the
# whole device list.
MAX_STALE_FRACTION = 0.5


def device_identifier(device: dict) -> str | None:
    """The identifier of the HA device a raw hub device belongs to.

    Same rule as ``device_identifier`` on the entities: the relationId groups
    the halves of a split device, otherwise the device id.
    """
    return device.get("relationId") or device.get("id")


def known_identifiers(devices: Any) -> set[str] | None:
    """Every identifier a device of ours can carry, from a raw /devices response.

    That is the device identifier (relationId or id) and the id of each device
    set a light belongs to. None when the response is not a usable list.
    """
    if not isinstance(devices, list) or not devices:
        return None
    known: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        identifier = device_identifier(device)
        if identifier:
            known.add(identifier)
        for device_set in device.get("deviceSet") or []:
            if isinstance(device_set, dict) and device_set.get("id"):
                known.add(device_set["id"])
    return known


def superseded_identifiers(devices: Any) -> dict[str, str]:
    """Half ids of split devices on the hub, mapped to their relationId.

    A device of ours carrying only such an id is a leftover: the hub still has
    the half, but this integration puts it on the relationId device.
    """
    if not isinstance(devices, list):
        return {}
    return {
        device["id"]: device["relationId"]
        for device in devices
        if isinstance(device, dict) and device.get("id") and device.get("relationId")
        and device["id"] != device["relationId"]
    }


def _our_identifiers(device: dr.DeviceEntry) -> list[str]:
    return [value for domain, value in device.identifiers if domain == DOMAIN_KEY]


def find_stale_devices(
    devices: Iterable[dr.DeviceEntry], known: set[str]
) -> list[dr.DeviceEntry]:
    """Devices with an identifier of ours that the hub no longer knows."""
    stale = []
    for device in devices:
        ours = _our_identifiers(device)
        if ours and not any(identifier in known for identifier in ours):
            stale.append(device)
    return stale


def split_stale(
    stale: Iterable[dr.DeviceEntry], superseded: dict[str, str]
) -> tuple[list[dr.DeviceEntry], list[dr.DeviceEntry]]:
    """Split stale devices into gone from the hub and superseded by a relationId."""
    gone, replaced = [], []
    for device in stale:
        if all(identifier in superseded for identifier in _our_identifiers(device)):
            replaced.append(device)
        else:
            gone.append(device)
    return gone, replaced


def is_stale(device: dr.DeviceEntry, known: set[str] | None) -> bool:
    """Whether the user may delete this device: only when we no longer create it."""
    if known is None:
        return False
    return bool(find_stale_devices([device], known))


def _carry_name_over(device_reg, entry_id: str, replaced: list[dr.DeviceEntry],
                     superseded: dict[str, str]) -> None:
    """Give the relationId device the user's own name of a superseded half.

    Only when the relationId device has no name of its own; the first half by
    identifier wins, which is the primary half (_1) that carries the hub name.
    """
    by_identifier = {
        identifier: device
        for device in dr.async_entries_for_config_entry(device_reg, entry_id)
        for identifier in _our_identifiers(device)
    }
    named = set()
    for device in sorted(replaced, key=lambda d: sorted(_our_identifiers(d))):
        if not device.name_by_user:
            continue
        target = by_identifier.get(superseded[_our_identifiers(device)[0]])
        if target is None or target.name_by_user or target.id in named:
            continue
        device_reg.async_update_device(target.id, name_by_user=device.name_by_user)
        named.add(target.id)


async def async_remove_stale_devices(hass, entry_id: str, hub, dry_run: bool = DRY_RUN) -> list[str]:
    """Compare the hub's device list with ours and remove what is gone.

    Returns the identifiers found stale (removed, or that would be removed in a
    dry run). Does nothing when the hub's answer is unusable or when too large a
    share of the devices would go.
    """
    try:
        raw = await hass.async_add_executor_job(hub.get, "/devices")
    except Exception as ex:
        logger.warning("Stale device check skipped, /devices failed: %s", ex)
        return []

    known = known_identifiers(raw)
    if known is None:
        logger.warning("Stale device check skipped, /devices gave no device list")
        return []

    device_reg = dr.async_get(hass)
    ours = dr.async_entries_for_config_entry(device_reg, entry_id)
    stale = find_stale_devices(ours, known)
    if not stale:
        logger.info("Stale device check: none of %d devices is stale", len(ours))
        return []

    superseded = superseded_identifiers(raw)
    gone, replaced = split_stale(stale, superseded)

    def _names(devices):
        return ", ".join(
            f"{device.name_by_user or device.name} ({', '.join(_our_identifiers(device))})"
            for device in devices
        )

    # A single gone device is always allowed, otherwise a one-device setup could
    # never clean up. Superseded devices don't count: their halves are on the
    # list with a relationId, so an incomplete list cannot make them look stale.
    if len(gone) > 1 and len(gone) > len(ours) * MAX_STALE_FRACTION:
        logger.warning(
            "Stale device check: %d of %d devices are not on the hub, more than "
            "half, so none of those is removed. Not on the hub: %s",
            len(gone), len(ours), _names(gone),
        )
        gone = []

    remove = gone + replaced
    if not remove:
        return []

    if dry_run:
        if gone:
            logger.warning(
                "Stale device check (dry run, nothing removed): %d of %d devices are "
                "no longer on the hub and would be removed: %s",
                len(gone), len(ours), _names(gone),
            )
        if replaced:
            logger.warning(
                "Stale device check (dry run, nothing removed): %d devices are left "
                "over from one device per half of a split device (as created before "
                "the switch to relationId grouping) and would be removed: %s",
                len(replaced), _names(replaced),
            )
    else:
        _carry_name_over(device_reg, entry_id, replaced, superseded)
        for device in remove:
            # Only detach our config entry: since 2026.9 a device can belong to
            # more than one entry, and HA deletes it once no entry is left.
            device_reg.async_update_device(device.id, remove_config_entry_id=entry_id)
        logger.info("Stale device check: removed %d devices: %s", len(remove), _names(remove))
    return [identifier for device in remove for identifier in _our_identifiers(device)]
