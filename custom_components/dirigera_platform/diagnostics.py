"""Diagnostics download for a hub (Settings > Devices & services > Download diagnostics).

Home Assistant only calls this when the user clicks; nothing is collected or
sent otherwise. The file holds the integration version, the entry's options and
the hub's raw /devices list, anonymised by the same code as dump_data. The hub's
address and token never end up in it.
"""
from __future__ import annotations

from typing import Any

from dirigera import Hub
from homeassistant.const import CONF_IP_ADDRESS, CONF_TOKEN
from homeassistant.loader import async_get_integration

from .anonymise import REDACTED, anonymise_devices
from .const import DOMAIN


def _fetch_devices(token: str, ip: str) -> list[dict]:
    return Hub(token, ip).get("/devices")


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    ip = entry.data.get(CONF_IP_ADDRESS, "")
    token = entry.data.get(CONF_TOKEN, "")
    integration = await async_get_integration(hass, DOMAIN)

    options = {
        key: (REDACTED if key in (CONF_IP_ADDRESS, CONF_TOKEN) else value)
        for key, value in entry.data.items()
    }

    error = None
    try:
        raw = await hass.async_add_executor_job(_fetch_devices, token, ip)
    except Exception as err:  # the file is still useful without the device list
        raw = []
        error = f"{type(err).__name__}: {err}"
        for secret in (ip, token):
            if secret:
                error = error.replace(secret, REDACTED)

    raw = [device for device in (raw or []) if isinstance(device, dict)]
    return {
        "integration_version": str(integration.version),
        "options": options,
        "hub_device_count": len(raw),
        "physical_device_count": len({d.get("relationId") or d.get("id") for d in raw}),
        "error": error,
        "devices": anonymise_devices(raw),
    }
