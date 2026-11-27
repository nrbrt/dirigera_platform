"""Anonymise the hub's raw /devices list so a user can post it in an issue.

Used by the diagnostics download and by the dump_data service, so there is one
place that decides what leaves the house.

What stays: the structure that tells which hub devices form one physical device
(ids that share a relationId, their _N suffix, links between devices), models,
firmware, capabilities and every other attribute value.

What goes: every id (replaced by a neutral one that keeps its _N suffix), serial
numbers, the names a user gave devices, rooms and groups, the hub's coordinates
and sun times (which give away where the house is), whether anyone is home, and
Matter pairing codes. Any uuid left in some other value is replaced as well.

Pure: no Home Assistant imports, the input is not changed.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "**REDACTED**"

# Keys whose value is replaced wherever they occur.
REDACT_KEYS = frozenset({
    "customName",
    "name",
    "serialNumber",
    "coordinates",
    "nextSunRise",
    "nextSunSet",
    "sunPosition",
    "homestate",
    "qrCode",
    "setupCode",
    "discriminator",
    "ip_address",
    "token",
})

_SUFFIX = re.compile(r"(.*?)(_\d+)?")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class _Mapper:
    def __init__(self) -> None:
        self._maps: dict[str, dict[str, str]] = {"device": {}, "room": {}, "set": {}, "id": {}}

    def _neutral(self, kind: str, value: str) -> str:
        table = self._maps[kind]
        if value not in table:
            table[value] = f"{kind}-{len(table) + 1:02d}"
        return table[value]

    def device(self, value: str) -> str:
        """A hub device id: map the base, keep the _N suffix."""
        base, suffix = _SUFFIX.fullmatch(value).groups()
        return self._neutral("device", base) + (suffix or "")

    def room(self, value: str) -> str:
        return self._neutral("room", value)

    def set(self, value: str) -> str:
        return self._neutral("set", value)

    def loose_uuid(self, match: re.Match) -> str:
        """A uuid found inside some other value."""
        value = match.group(0)
        if value in self._maps["device"]:
            return self._maps["device"][value]
        return self._neutral("id", value)


def _walk(value: Any, mapper: _Mapper, parent: str | None) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in REDACT_KEYS:
                out[key] = REDACTED
            elif key == "id" and isinstance(item, str):
                if parent == "room":
                    out[key] = mapper.room(item)
                elif parent == "deviceSet":
                    out[key] = mapper.set(item)
                else:
                    out[key] = mapper.device(item)
            elif key == "relationId" and isinstance(item, str):
                out[key] = mapper.device(item)
            elif key == "remoteLinks" and isinstance(item, list):
                out[key] = [mapper.device(x) if isinstance(x, str) else _walk(x, mapper, key) for x in item]
            else:
                out[key] = _walk(item, mapper, key)
        return out
    if isinstance(value, list):
        return [_walk(item, mapper, parent) for item in value]
    if isinstance(value, str):
        return _UUID.sub(mapper.loose_uuid, value)
    return value


def anonymise_devices(devices: list[dict]) -> list[dict]:
    """Return an anonymised copy of the hub's /devices list."""
    mapper = _Mapper()
    # Number the hub devices in list order before anything else refers to them,
    # so a link to a device further down does not take an earlier number.
    for device in devices:
        if isinstance(device, dict):
            for key in ("relationId", "id"):
                if isinstance(device.get(key), str):
                    mapper.device(device[key])
    return [_walk(device, mapper, None) for device in devices]
