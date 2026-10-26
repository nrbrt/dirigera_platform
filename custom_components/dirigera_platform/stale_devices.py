"""Remove devices the hub no longer has.

Home Assistant's quality-scale rule ``stale-devices``: when a device is removed
from the hub, the integration removes it from Home Assistant too, but only when
it is sure the device is gone. The hub's ``/devices`` returns the full list, so
a device whose identifier is on none of its entries is gone.

The comparison uses the raw ``/devices`` response from the plain library Hub,
not the filtered one HubX builds entities from. Devices that exist but are not
imported on purpose (a deselected platform, the Matter filter, the second half
of a split device) are on that list, so they are never stale. Deselected
platforms keep being disabled by sync_platform_entity_registry instead.

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


def known_identifiers(devices: Any) -> set[str] | None:
    """Every identifier a device of ours can carry, from a raw /devices response.

    That is the device id, the relationId of a split device, and the id of each
    device set a light belongs to. None when the response is not a usable list.
    """
    if not isinstance(devices, list) or not devices:
        return None
    known: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        if device.get("id"):
            known.add(device["id"])
        if device.get("relationId"):
            known.add(device["relationId"])
        for device_set in device.get("deviceSet") or []:
            if isinstance(device_set, dict) and device_set.get("id"):
                known.add(device_set["id"])
    return known


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


def is_stale(device: dr.DeviceEntry, known: set[str] | None) -> bool:
    """Whether the user may delete this device: only when the hub lost it."""
    if known is None:
        return False
    return bool(find_stale_devices([device], known))


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

    names = ", ".join(
        f"{device.name_by_user or device.name} ({', '.join(_our_identifiers(device))})"
        for device in stale
    )
    # A single stale device is always allowed, otherwise a one-device setup
    # could never clean up.
    if len(stale) > 1 and len(stale) > len(ours) * MAX_STALE_FRACTION:
        logger.warning(
            "Stale device check: %d of %d devices are not on the hub, more than "
            "half, so nothing is removed. Not on the hub: %s",
            len(stale), len(ours), names,
        )
        return []

    if dry_run:
        logger.warning(
            "Stale device check (dry run, nothing removed): %d of %d devices are "
            "no longer on the hub and would be removed: %s",
            len(stale), len(ours), names,
        )
    else:
        for device in stale:
            # Only detach our config entry: since 2026.9 a device can belong to
            # more than one entry, and HA deletes it once no entry is left.
            device_reg.async_update_device(device.id, remove_config_entry_id=entry_id)
        logger.info("Stale device check: removed %d devices: %s", len(stale), names)
    return [identifier for device in stale for identifier in _our_identifiers(device)]
