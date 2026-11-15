"""Which IKEA device sets exist, which lights are in them, and what to import.

A device set is a group of lights that the hub controls as one: a single
PATCH /devices/set/{id} changes every light in it at the same time. The
hub never reports a state for the set itself, only for its lights, so a set
entity takes its state from one of its lights (the "controller", the first
member). That light's events must keep being processed even when the light is
not imported as its own entity.

Membership is not fixed. The hub pushes it while Home Assistant runs:

- deviceSetCreated / deviceSetUpdated / deviceSetDeleted carry {id, name, icon}
  (only {id} on delete);
- a light joining a set sends deviceStateChanged with deviceSet [{id, name,
  icon}], a light leaving one sends deviceSet []. Plain state events carry no
  deviceSet key at all.

A light is in at most one set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .const import (
    CONF_DEVICE_SET_MODE,
    DEVICE_SET_MODES,
    DEVICE_SET_MODE_LIGHTS_ONLY,
    DEVICE_SET_MODE_SETS_ONLY,
    LEGACY_DEVICE_SET_MODE,
)


def resolve_device_set_mode(data: dict) -> str:
    """The entry's device set mode.

    An entry without the key was created before the option existed and always
    got the sets and their lights, so it keeps getting exactly that. An unknown
    value is treated the same way rather than failing setup.
    """
    mode = data.get(CONF_DEVICE_SET_MODE)
    if mode in DEVICE_SET_MODES:
        return mode
    return LEGACY_DEVICE_SET_MODE


def sets_wanted(mode: str) -> bool:
    """Whether set entities are imported in this mode."""
    return mode != DEVICE_SET_MODE_LIGHTS_ONLY


def light_wanted(mode: str, in_set: bool) -> bool:
    """Whether a light is imported as its own entity in this mode."""
    return not in_set or mode != DEVICE_SET_MODE_SETS_ONLY


def _first_set(device_sets: Any) -> Optional[dict]:
    """The set a light's deviceSet list puts it in, None for no set."""
    for device_set in device_sets or []:
        if isinstance(device_set, dict) and device_set.get("id"):
            return device_set
    return None


@dataclass
class DeviceSet:
    id: str
    name: str
    # The room of the first light seen in the set, used as the suggested area.
    room: Any = None
    # Light ids in the order they joined. The first one is the controller.
    members: list = field(default_factory=list)


@dataclass(frozen=True)
class MembershipChange:
    """What a light's deviceSet update changed. All None means nothing did."""
    left: Optional[str] = None
    joined: Optional[str] = None
    # Set id whose name the payload changed (a rename missed while
    # disconnected comes back this way through the reconnect resync).
    renamed: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.left or self.joined or self.renamed)


class DeviceSetIndex:
    def __init__(self) -> None:
        self._sets: dict[str, DeviceSet] = {}
        self._light_set: dict[str, str] = {}

    @classmethod
    def from_lights(cls, lights: Iterable[Any]) -> "DeviceSetIndex":
        """Build the index from the hub's lights.

        Takes ikea_bulb entities or the dirigera Light models they wrap.
        """
        index = cls()
        for light in lights:
            data = getattr(light, "_json_data", light)
            index.set_membership(data.id, data.device_set, room=data.room)
        return index

    # --- lookups ----------------------------------------------------------

    def get(self, set_id: str) -> Optional[DeviceSet]:
        return self._sets.get(set_id)

    def set_ids(self) -> list:
        return list(self._sets)

    def in_set(self, light_id: str) -> bool:
        return light_id in self._light_set

    def controller_id(self, set_id: str) -> Optional[str]:
        """The light the set takes its state from: its first member."""
        device_set = self._sets.get(set_id)
        if device_set is None or not device_set.members:
            return None
        return device_set.members[0]

    def empty_set_ids(self) -> list:
        return [set_id for set_id, device_set in self._sets.items() if not device_set.members]

    def hidden_unique_ids(self, mode: str) -> set:
        """Unique ids of the light entities this mode leaves out.

        Both kinds use the hub id as unique_id (ikea_bulb, ikea_bulb_device_set).
        """
        hidden = set() if light_wanted(mode, in_set=True) else set(self._light_set)
        if not sets_wanted(mode):
            hidden |= set(self._sets)
        return hidden

    # --- changes ----------------------------------------------------------

    def create(self, set_id: str, name: Optional[str]) -> None:
        """Add a set (deviceSetCreated) that has no lights yet."""
        self._sets.setdefault(set_id, DeviceSet(id=set_id, name=name or set_id))

    def rename(self, set_id: str, name: Optional[str]) -> bool:
        """Rename a set (deviceSetUpdated). Returns whether the name changed."""
        device_set = self._sets.get(set_id)
        if device_set is None or not name or device_set.name == name:
            return False
        device_set.name = name
        return True

    def delete(self, set_id: str) -> list:
        """Drop a set (deviceSetDeleted). Returns the lights that were in it.

        Those lights are in no set from here on. The hub follows up with
        deviceSet [] for each of them, which is then a no-op.
        """
        device_set = self._sets.pop(set_id, None)
        if device_set is None:
            return []
        for light_id in device_set.members:
            self._light_set.pop(light_id, None)
        return list(device_set.members)

    def set_membership(self, light_id: str, device_sets: Any, room: Any = None) -> MembershipChange:
        """Apply a light's deviceSet list.

        Unchanged membership is a no-op, so replaying /devices after a
        reconnect does not churn anything. A set the index does not know yet
        (created while disconnected, or the first light seen at startup) is
        created from the payload.
        """
        target = _first_set(device_sets)
        new_id = target["id"] if target else None
        current = self._light_set.get(light_id)

        renamed = None
        if target is not None and new_id in self._sets and self.rename(new_id, target.get("name")):
            renamed = new_id

        if new_id == current:
            return MembershipChange(renamed=renamed)

        if current is not None:
            old = self._sets.get(current)
            if old is not None and light_id in old.members:
                old.members.remove(light_id)
            del self._light_set[light_id]

        if new_id is not None:
            device_set = self._sets.get(new_id)
            if device_set is None:
                device_set = self._sets[new_id] = DeviceSet(id=new_id, name=target.get("name") or new_id)
            if device_set.room is None:
                device_set.room = room
            device_set.members.append(light_id)
            self._light_set[light_id] = new_id

        return MembershipChange(left=current, joined=new_id, renamed=renamed)
