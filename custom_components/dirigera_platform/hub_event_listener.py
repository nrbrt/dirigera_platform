import asyncio
import contextlib
import threading
import logging 
import time 
import json
import re 
import websocket
import ssl
from typing import Any
import datetime
from dateutil import parser
from pydantic import TypeAdapter
from dirigera import Hub

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import device_registry as dr, entity_registry as er, area_registry as ar


def get_device_by_identifier(device_reg, identifier, config_entry_id):
    """Look up a device by identifier without the deprecated async_get_device.

    Since 2026.9 identifiers are no longer unique across config entries, so
    the lookup is scoped to our entry. Older releases lack the new method and
    keep using async_get_device.
    """
    by_identifier = getattr(device_reg, "async_get_device_by_identifier", None)
    if by_identifier is not None and config_entry_id is not None:
        return by_identifier(identifier, config_entry_id)
    return device_reg.async_get_device({identifier})


logger = logging.getLogger("custom_components.dirigera_platform.hub_event_listener")

_DOMAIN = "dirigera_platform"

DEVICE_SET_EVENTS = ("deviceSetCreated", "deviceSetUpdated", "deviceSetDeleted")


def _push_state(entity) -> None:
    """Write an entity's state to Home Assistant, if it is in Home Assistant.

    Every light has an entity registered here, including a light that the
    device set mode leaves out of Home Assistant. A set takes its state from
    its first light, so that light's events are applied either way. Its own
    state is written only while it is added (hass is set) and enabled. A light
    that the mode disabled while Home Assistant runs is skipped too, since
    Home Assistant warns about writes to disabled entities.
    """
    # The registry also holds device wrappers (base_classes), which are not
    # entities: they have neither attribute and push to their own entities.
    if getattr(entity, "hass", True) is None or not getattr(entity, "enabled", True):
        return
    # Called on the event loop: since the 0.4.1 refactor every hub event is
    # applied there (on_message only parses). #76 was written for the
    # WebSocket thread and used the thread-safe schedule_update_ha_state.
    entity.async_schedule_update_ha_state(False)

DATE_TIME_FORMAT:str =  "%Y-%m-%dT%H:%M:%S.%fZ"

controller_trigger_last_time_map = {}
# HA device ids of remotes seen sending remotePressEvent (see _fire_controller_event).
remotes_with_press_events = set()
DEBOUNCE_DELTA = datetime.timedelta(seconds=1)
# Seconds between two runs of the safety net (#207): one GET /devices for the
# whole hub, applied like the reconnect resync. Entities are not polled one by
# one any more (that load made a hub with many devices unresponsive); this
# catches what an event did not bring. Five minutes is what the reporter of
# #207 ran with 150 entities, and one request per five minutes is nothing for
# the hub.
SAFETY_NET_INTERVAL = 5 * 60
# A scene trigger whose time is older than this, relative to the sceneUpdated
# message, belongs to an earlier press.
STALE_TRIGGER_DELTA = datetime.timedelta(seconds=5)
CLICK_PATTERNS = {
    "singlePress": "single_click",
    "longPress": "long_press",
    "doublePress": "double_click",
}

def _parse_hub_time(value):
    """Parse a hub timestamp to a tz-aware datetime, None if absent or unusable."""
    if value is None:
        return None
    try:
        parsed = parser.parse(value)
    except (ValueError, OverflowError, TypeError) as ex:
        logger.warning(f"Failed to parse hub time {value!r}: {ex}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed

def to_snake_case(name:str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()

# --- Applying hub attributes to an entity's model -----------------------------
#
# Which attributes an event may change used to be a fixed list per deviceType.
# Types missing from it (remotes, air purifiers) were dropped whole and missing
# attributes were dropped silently: #167, #153. The entity's own attributes model
# is the authority instead: an attribute is applied when the model has a field
# for it, and the value goes through that field's validation, so datetimes,
# times and enums get their real type rather than the raw JSON string.

_NO_FIELD = object()
_field_adapters = {}
_misfit_warned = set()


def _field_value(model, name, raw):
    """raw as the type of the model's field `name`, or _NO_FIELD when the model
    has no such field. Raises ValueError when raw does not fit the field."""
    fields = getattr(type(model), "model_fields", None)
    if not fields or name not in fields:
        return _NO_FIELD
    key = (type(model), name)
    adapter = _field_adapters.get(key)
    if adapter is None:
        adapter = _field_adapters[key] = TypeAdapter(fields[name].annotation)
    return adapter.validate_python(raw)


# --- #45: protect TOTAL_INCREASING energy sensors from spurious dips ---------
#
# Home Assistant reads any decrease on a TOTAL_INCREASING sensor as a counter
# reset and adds the whole total to long-term statistics again, so a single bad
# value permanently inflates the history. A GRILLPLATS reporting 5.982 kWh that
# dips to 5 kWh for 137 ms injected 5.982 kWh into the statistics three times in
# two weeks; on a plug sitting at 263 kWh one dip would inject 263 kWh.
#
# The resync (after a reconnect, #39, and the safety net, #207) replays raw /devices through this same
# event path, and that bypasses the electricalSensor merge that get_outlets()
# applies for split plugs (GRILLPLATS, TOFSMYGGA), so a stale or coarser value
# can reach the entity there.
#
# A genuine energy reset is not silent: the device advances
# timeOfLastEnergyReset. So only accept a decreasing total when that timestamp
# moved, or when the value drops to zero, which is how a reset is reported.
ENERGY_TOTAL_ATTRS = ("totalEnergyConsumed", "energyConsumedAtLastReset")

# How many consecutive held-back decreases before the lower value is accepted
# anyway. Without this a real reset that the hub failed to timestamp would
# freeze the sensor for good. An isolated glitch never gets here: in #45 the
# correct value follows within ~140 ms and clears the counter.
ENERGY_DECREASE_ACCEPT_AFTER = 3


def _as_utc_datetime(value):
    """Best-effort tz-aware datetime for a timeOfLastEnergyReset value.

    The value may arrive as an ISO string from the hub or as a datetime that an
    earlier update already parsed."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = parser.parse(value)
        except (ValueError, OverflowError, TypeError):
            return None
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _energy_reset_advanced(entity, attributes) -> bool:
    """True when this payload reports a newer energy reset than the one held.

    Must be evaluated before any attribute of the payload is written, because
    the total and the reset timestamp can arrive in the same dict and the
    iteration order of that dict is not something to depend on."""
    if "timeOfLastEnergyReset" not in attributes:
        return False
    incoming = _as_utc_datetime(attributes.get("timeOfLastEnergyReset"))
    if incoming is None:
        return False
    previous = _as_utc_datetime(
        getattr(entity._json_data.attributes, "time_of_last_energy_reset", None)
    )
    if previous is None:
        # Nothing to compare against, so do not stand in the way of the write.
        return True
    return incoming > previous


def _hold_back_energy_decrease(entity, key_attr, value_to_set) -> bool:
    """True when this decreasing energy total should not be written.

    Only called when the payload carries no newer timeOfLastEnergyReset."""
    if isinstance(value_to_set, bool) or not isinstance(value_to_set, (int, float)):
        return False
    previous = getattr(entity._json_data.attributes, key_attr, None)
    if isinstance(previous, bool) or not isinstance(previous, (int, float)):
        return False

    held_map = getattr(entity, "_energy_decrease_held", None)
    if held_map is None:
        held_map = {}
        entity._energy_decrease_held = held_map

    if value_to_set >= previous or value_to_set <= 0:
        held_map[key_attr] = 0
        return False

    held = held_map.get(key_attr, 0) + 1
    held_map[key_attr] = held
    if held >= ENERGY_DECREASE_ACCEPT_AFTER:
        logger.warning(
            f"{key_attr} stayed below the stored value ({value_to_set} < {previous}) for "
            f"{held} consecutive updates without a newer timeOfLastEnergyReset; "
            f"accepting it as a reset the hub did not timestamp"
        )
        held_map[key_attr] = 0
        return False

    logger.warning(
        f"Ignoring decrease of {key_attr} from {previous} to {value_to_set} with no newer "
        f"timeOfLastEnergyReset (issue #45); holding {held}/{ENERGY_DECREASE_ACCEPT_AFTER}"
    )
    return True

# --- A fetch of device state and the events that arrive while it is on its way --
#
# A fetched snapshot of device state is as old as the hub's answer. What a hub
# message writes to a model while the fetch is on its way is newer, and the
# snapshot must not put the older value back over it.
#
# Events are never kept waiting for a fetch: a slow or silent hub would delay a
# motion sensor for as long as its timeout. They are applied as they come, and
# whoever fetches opens a live_writes() for the length of the fetch. It records
# which fields and attributes of which entity were written meanwhile, and the
# fetcher leaves exactly those out of what it fetched:
#
# * a refresh of one device, which replaces the entity's model, carries the
#   values written meanwhile over to the new model (replace_model);
# * the resync (after a reconnect, or the safety net) leaves them out of the
#   device's row of the snapshot.
#
# Commands write models too (the library's setters, a light's accepted
# values). Which keys a setter writes is not visible from here, and comparing
# before and after misses a command that writes the value the model already
# had (follow-up review of #207). So a command notes its entity as a whole
# (note_command): the fetcher keeps all of that entity's attributes, for this
# one snapshot. The next resync or event brings the rest.
#
# Event loop only, like everything that touches a model.
_open_live_writes = []


class _LiveWrites:
    def __init__(self):
        self._written = {}  # entity -> (hub keys of device fields, of attributes)
        self._commanded = set()  # entities a command wrote to

    def _note(self, entity, attributes: bool, keys):
        if keys:
            self._written.setdefault(entity, (set(), set()))[attributes].update(keys)

    def commanded(self, entity) -> bool:
        """True when a command wrote to the entity's model since the fetch
        began: all of its attributes are newer than the fetched ones."""
        return entity in self._commanded

    def fields(self, entity) -> set:
        """Hub keys of the device's own fields written since the fetch began."""
        return self._written.get(entity, (frozenset(), frozenset()))[0]

    def attributes(self, entity) -> set:
        """Hub keys of the attributes written since the fetch began."""
        return self._written.get(entity, (frozenset(), frozenset()))[1]


@contextlib.contextmanager
def live_writes():
    """Record what hub messages write to entity models inside this block."""
    writes = _LiveWrites()
    _open_live_writes.append(writes)
    try:
        yield writes
    finally:
        _open_live_writes.remove(writes)


def note_command(entity) -> None:
    """A command for the entity: noted when its request goes out and again
    after the hub accepted it. Its attributes are newer than a fetch on its
    way, as an event's would be (#207: the safety net fetches every few
    minutes, so a command during it is no longer rare). A command that then
    fails has still cost that device the attributes of one snapshot; the
    next resync or event brings them. Event loop only."""
    for writes in _open_live_writes:
        writes._commanded.add(entity)


def replace_model(entity, fetched, newer):
    """Make `fetched` the entity's model. What hub messages wrote to the old
    model while it was fetched (`newer`, from live_writes) is newer than the
    hub's answer and is carried over. When a command wrote to the entity in
    that time, its whole attributes object is kept (see note_command)."""
    old = entity._json_data
    if newer.commanded(entity):
        try:
            fetched.attributes = old.attributes
        except Exception as ex:
            logger.warning(f"Could not keep the commanded attributes of {old.id}: {ex}")
    for source, target, keys in (
        (old, fetched, newer.fields(entity)),
        (getattr(old, "attributes", None), getattr(fetched, "attributes", None),
         newer.attributes(entity)),
    ):
        for key in keys:
            name = to_snake_case(key)
            try:
                setattr(target, name, getattr(source, name))
            except Exception as ex:
                logger.warning(f"Could not keep the live {name} of {old.id}: {ex}")
    entity._json_data = fetched


def _write_fields(entity, model, values, held_back=None):
    """Write every hub value the model has a field for, as the type of that
    field. held_back(key, key_attr, value) may keep one value out. Returns the
    hub keys that were written.

    The one place where hub state reaches a model, so also where a fetch that
    is on its way learns what became newer than its answer (live_writes)."""
    device_id = entity._json_data.id
    applied = set()
    for key, raw in values.items():
        key_attr = to_snake_case(key)
        try:
            value = _field_value(model, key_attr, raw)
            if value is _NO_FIELD:
                logger.debug(f"{key} is not on the model of {device_id}, ignoring")
                continue
            if held_back is not None and held_back(key, key_attr, value):
                continue
            setattr(model, key_attr, value)
            applied.add(key)
        except ValueError as ex:
            # Once per device and field, or a value shape the hub keeps
            # sending would log on every event.
            warn_key = (device_id, key_attr)
            if warn_key not in _misfit_warned:
                _misfit_warned.add(warn_key)
                logger.warning(f"Keeping the old {key_attr} of {device_id}, the hub sent {raw!r} which does not fit the model: {ex}")
        except Exception as ex:
            # One bad value must not drop the rest of the event.
            logger.warning(f"Failed to set {key} on {device_id}: {ex}")
    for writes in _open_live_writes:
        writes._note(entity, model is not entity._json_data, applied)
    return applied


def _apply_attributes(entity, attributes, own_device=True):
    """Write every hub attribute the entity's model has a field for.

    own_device is False for an event of another part of a split device that is
    routed to this entity (BILRESA _2, GRILLPLATS _2). Such a part adds what it
    measures (battery, energy), which is why it is routed here, but it does not
    erase or rename: its nulls and its customName (each part keeps its own name
    on the hub) are skipped. Returns the hub keys that were written.

    An entity with hub_attributes() says first what of the event applies to it
    (a light leaves out the stale echo of its own command)."""
    model = getattr(getattr(entity, "_json_data", None), "attributes", None)
    if model is None:
        return set()
    own_view = getattr(entity, "hub_attributes", None)
    if own_view is not None:
        attributes = own_view(attributes)
    if not own_device:
        attributes = {key: raw for key, raw in attributes.items()
                      if key != "customName" and raw is not None}
    # Read before anything is written (see #45).
    energy_reset_advanced = _energy_reset_advanced(entity, attributes)

    def held_back(key, key_attr, value):
        return (key in ENERGY_TOTAL_ATTRS and not energy_reset_advanced
                and _hold_back_energy_decrease(entity, key_attr, value))

    return _write_fields(entity, model, attributes, held_back)


# --- The device's own fields ---------------------------------------------------
#
# Next to `attributes` an event carries fields of the device itself:
# isReachable, room, lastSeen, isHidden, and the resync replays whole devices.
# They go through the device model the same way: a field is applied when the
# model has it, with that field's validation (a room becomes the model's Room).
#
# Two keys are not state of the device for this path: `attributes` is partial
# in an event and is applied field by field above, and `deviceSet` is not
# applied from events at all.
DEVICE_KEYS_WITH_THEIR_OWN_PATH = frozenset({"attributes", "deviceSet"})

# What the parts of a split device have in common. The hub keeps the room on
# one part only, and a part that drops off the network takes the others along.
# Everything else in the event of another part (its id, type, capabilities,
# links, lastSeen) describes that part, as its customName does.
DEVICE_KEYS_SHARED_BY_PARTS = frozenset({"isReachable", "room"})


def _apply_device_fields(entity, info, own_device=True):
    """Write the top-level fields of a hub device the entity's model has.

    For another part of a split device (own_device False) only what the parts
    share is taken, and never a null: it adds, it does not erase."""
    model = getattr(entity, "_json_data", None)
    if model is None:
        return set()
    fields = {key: raw for key, raw in info.items()
              if key not in DEVICE_KEYS_WITH_THEIR_OWN_PATH}
    if not own_device:
        fields = {key: raw for key, raw in fields.items()
                  if key in DEVICE_KEYS_SHARED_BY_PARTS and raw is not None}
    return _write_fields(entity, model, fields)


def _room_of(data) -> tuple[str, str]:
    """(id, name) of the device's room on the hub; ("", "") without one.

    A room is the same room by its id: renaming it in the IKEA app keeps
    the id, and must not move devices."""
    room = getattr(data, "room", None)
    return (room.id, room.name) if room is not None else ("", "")


def _device_view(entity):
    """What of a device the listener acts on when it changes: every model field
    but the attributes (a dict of field values), the id of its room and its
    name on the hub."""
    data = entity._json_data
    fields = getattr(type(data), "model_fields", None) or ()
    return (
        {name: getattr(data, name, None) for name in fields if name != "attributes"},
        _room_of(data)[0],
        getattr(getattr(data, "attributes", None), "custom_name", None),
    )


class registry_entry:
    def __init__(self, entity:any, cascade_entity:any = None):
        self._entity = entity
        self._cascade_entity = cascade_entity 
    
    @property
    def entity(self):
        return self._entity
    
    @property
    def cascade_entity(self):
        return self._cascade_entity
    
    @cascade_entity.setter
    def cascade_entity(self, value):
        self._cascade_entity = value 

    def __str__(self):
        str =  f"registry_entry: id {self._entity.unique_id}, cascade_entry : "
        if self._cascade_entity is None :
            str = str +  "None"
        else:
            str = str + f"{self._cascade_entity}"
        return str

class hub_event_listener(threading.Thread):
    # Per-hub device registries: {hub_key: {device_id: registry_entry}}.
    # hub_key is the hub's websocket_base_url, which a hub's entities and its
    # listener share (both are built from the same config-entry IP). Keeping a
    # registry per hub means stopping or unloading one hub no longer wipes
    # another hub's device registrations. See issue #39.
    device_registry = {}

    def register(hub_key: str, id: str, entry: registry_entry):
        bucket = hub_event_listener.device_registry.setdefault(hub_key, {})
        if id in bucket:
            return
        bucket[id] = entry

    def get_registry_entry(hub_key: str, id: str) -> registry_entry:
        return hub_event_listener.device_registry.get(hub_key, {}).get(id)

    def unregister(hub_key: str, id: str):
        hub_event_listener.device_registry.get(hub_key, {}).pop(id, None)

    def unregister_hub(hub_key: str):
        hub_event_listener.device_registry.pop(hub_key, None)

    def find_registry_entry(id: str) -> registry_entry:
        # Hub-agnostic lookup for callers without a hub reference (e.g. device
        # triggers). Device ids are unique across hubs, so the first match wins.
        for bucket in hub_event_listener.device_registry.values():
            if id in bucket:
                return bucket[id]
        return None

    def _own_registry(self) -> dict:
        return hub_event_listener.device_registry.get(self._hub_key, {})

    def _scene_sync(self):
        """This entry's SceneSync (scene.py), None without the scene platform."""
        data = getattr(self._hass, "data", None) or {}
        return data.get(_DOMAIN, {}).get(self._config_entry_id, {}).get("scenes")

    def _device_sets(self):
        """This entry's device set manager (light.py), None without the light platform."""
        data = getattr(self._hass, "data", None) or {}
        return data.get(_DOMAIN, {}).get(self._config_entry_id, {}).get("device_sets")

    def _schedule(self, fn, *args) -> None:
        """Run a device set manager method on the event loop, after the
        callback that is running now.

        The manager adds and removes entities and edits the registries. Since
        the 0.4.1 refactor its callers already run on the loop; queueing still
        matters, because a membership change must land after the state write
        of the same event (see parse_device_state_changed).
        """
        if self._request_to_stop or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(fn, *args)

    def parse_device_set_event(self, msg):
        """deviceSetCreated / deviceSetUpdated / deviceSetDeleted.

        The hub sends {id, name, icon} for the first two and {id} on delete.
        Lights joining or leaving a set arrive separately, as the deviceSet of
        a deviceStateChanged.
        """
        data = msg.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            logger.warning(f"discarding {msg.get('type')} without data/id: {msg}")
            return
        manager = self._device_sets()
        if manager is not None:
            self._schedule(manager.async_handle_set_event, msg["type"], data["id"], data.get("name"))
    
    # Dirigera hubs disconnect WebSocket clients after ~60 minutes of
    # "inactivity". Crucially, the hub does NOT count WebSocket protocol-level
    # ping frames (opcode 0x9) as activity — only application-level data
    # frames reset the timer. Setups with environment sensors (VINDSTYRKA,
    # ALPSTUGA) never hit this because they generate continuous events.
    # Setups with only door/window sensors or plugs go silent between state
    # changes and get disconnected.
    #
    # Fix: send a minimal application-level text frame well within that window.
    KEEPALIVE_INTERVAL = 15 * 60  # seconds — well below the hub's ~60 min timeout

    # The Dirigera hub can deliver the same deviceRemoved event more than once
    # (observed on a user's hub: two identical events ~20 ms apart). Each one
    # used to start its own stale-device reconciliation, so one removal produced
    # several /devices fetches and several warnings. A burst is coalesced into a
    # single pass with a short trailing-edge debounce.
    STALE_RECHECK_DEBOUNCE = 2.0  # seconds

    def __init__(self, hub : Hub, hass, discovery_coordinator=None, config_entry_id=None, rooms_store=None):
        super().__init__()
        # Scopes device-registry lookups to this hub's entry (#52).
        self._config_entry_id = config_entry_id
        self._hub : Hub = hub
        # Key into the per-hub device registry. Entities of this hub register
        # under the same key (their hub shares this websocket_base_url). See #39.
        self._hub_key = getattr(hub, "websocket_base_url", None)
        self._request_to_stop = False
        self._stop_event = threading.Event()
        self._hass = hass
        self._loop = asyncio.get_event_loop()
        self._discovery_coordinator = discovery_coordinator
        self._wsapp = None
        self._session_started_at = None
        self._keepalive_timer = None
        # True while a post-reconnect state resync is replaying /devices, so the
        # event path skips discovery for unknown devices (issue #39).
        self._resyncing = False
        # One resync at a time: a second reconnect waits for the first resync
        # and then fetches again.
        self._resync_lock = asyncio.Lock()
        # Ids of lights whose set membership a live event changed while a
        # resync fetch was on its way; None when no fetch is. The snapshot's
        # deviceSet for them is older and is left out (review 16-11-2026).
        self._membership_live = None
        # False until the first WebSocket open; used to tell an initial connect
        # (setup already fetched state) from a reconnect (must re-pull state).
        self._has_opened = False
        # True while a coalesced stale-device recheck is already pending, so a
        # burst of deviceRemoved events settles into one reconciliation.
        self._stale_check_pending = False
        # The id of the hub room each device was last seen in, per Home
        # Assistant device identifier, kept across restarts in rooms_store (a
        # Home Assistant Store, made by setup). See sync_all_device_areas.
        # Without a store (unit tests) nothing is remembered.
        self._rooms_seen: dict[str, str] = {}
        # The last known name of each hub room, by room id: a rename moves the
        # devices still in the area named after the old name.
        self._room_names: dict[str, str] = {}
        self._rooms_store = rooms_store

    def _update_device_area(self, device_id: str, room_name: str):
        """Follow a room change on the hub for one device (see _set_device_area).

        Runs inside the event handler: a failing lookup is logged here, so the
        rest of the event (state, name, the stored room) still goes through."""
        try:
            device_reg = dr.async_get(self._hass)
            device_entry = get_device_by_identifier(
                device_reg, ("dirigera_platform", device_id), self._config_entry_id
            )
        except Exception as ex:
            logger.error(f"Failed to look up device {device_id} for its area: {ex}")
            return
        if device_entry is None:
            logger.debug(f"Device {device_id} not found in HA device registry")
            return
        self._set_device_area(device_reg, device_entry, device_id, room_name)

    def _set_device_area(self, device_reg, device_entry, device_id: str, room_name: str) -> bool:
        """Put a device in the area named after its hub room; no room clears its area.

        Creates the area when there is none by that name. Only for a new
        device or a room that changed on the hub: otherwise the area is the
        user's (see sync_all_device_areas). Returns whether the device moved.
        """
        try:
            if not room_name:
                if device_entry.area_id is None:
                    return False
                logger.info(f"Removing area assignment from device {device_id}")
                device_reg.async_update_device(device_entry.id, area_id=None)
                return True
            area_reg = ar.async_get(self._hass)
            area_entry = area_reg.async_get_area_by_name(room_name)
            if area_entry is None:
                logger.info(f"Creating new area: {room_name}")
                area_entry = area_reg.async_create(room_name)
            if device_entry.area_id == area_entry.id:
                return False
            logger.info(f"Updating device {device_id} area to {room_name}")
            device_reg.async_update_device(device_entry.id, area_id=area_entry.id)
            return True
        except Exception as ex:
            logger.error(f"Failed to update device area for {device_id}: {ex}")
            return False

    async def _update_device_name(self, device_id: str, new_name: str):
        """Keep the device's own name in step with its name on the hub.

        Only the integration's name (`name`) is written. A name the user gave
        the device in Home Assistant (`name_by_user`) is theirs: Home Assistant
        shows it instead of `name`, and an integration must not change it.
        Until 0.4.0 the startup sync cleared it, so a name given in HA came
        back as the IKEA name after every restart (#72). A rename in the IKEA
        app still shows up for every device the user did not rename in HA
        (#19), and comes back for the others when they clear their own name.
        """
        try:
            device_reg = dr.async_get(self._hass)

            device_entry = get_device_by_identifier(
                device_reg, ("dirigera_platform", device_id), self._config_entry_id
            )
            if device_entry is None:
                logger.debug(f"Device {device_id} not found in HA device registry for name update")
                return

            if device_entry.name == new_name:
                logger.debug(f"Device {device_id} name already matches '{new_name}'")
                return

            logger.info(
                f"Updating device {device_id} name: '{device_entry.name}' -> '{new_name}'"
                + (f" (shown as the user's own name '{device_entry.name_by_user}')"
                   if device_entry.name_by_user else "")
            )
            device_reg.async_update_device(device_entry.id, name=new_name)
        except Exception as ex:
            logger.error(f"Failed to update device name for {device_id}: {ex}")

    async def sync_all_device_names(self):
        """Sync all device names from Dirigera customName to HA device registry.

        This should be called at startup after all entities are registered,
        to ensure HA device names match Dirigera names (set via IKEA app).
        """
        logger.info("Starting device name sync from Dirigera (%d registry entries)", len(self._own_registry()))
        synced_count = 0
        seen_identifiers = set()
        for device_id, registry_entry in self._own_registry().items():
            try:
                entity = registry_entry.entity
                if not hasattr(entity, '_json_data'):
                    logger.debug(f"Name sync: skipping {device_id} - no _json_data")
                    continue
                identifier = entity._json_data.relation_id or entity._json_data.id
                if identifier in seen_identifiers:
                    continue
                seen_identifiers.add(identifier)
                # Use device_name (consistent for split-devices) rather than
                # individual custom_name which can differ between sub-devices
                device_name = entity.device_name if hasattr(entity, 'device_name') else entity._json_data.attributes.custom_name
                if not device_name:
                    logger.debug(f"Name sync: skipping {device_id} - no name")
                    continue
                logger.debug(f"Name sync: {identifier} -> device_name='{device_name}'")
                await self._update_device_name(identifier, device_name)
                synced_count += 1
            except Exception as ex:
                logger.error(f"Failed to sync name for device {device_id}: {ex}")
        logger.info(f"Device name sync complete, synced {synced_count} devices")

    async def sync_all_device_areas(self, devices_before_setup: frozenset[str]):
        """Put new devices in their hub room, and move a known one only when its room on the hub changed.

        The area of a device belongs to the user. Up to and including
        0.4.1b1 every start put each device back in its IKEA room and created
        that area if it was gone, so a deleted area came back and a device
        moved to another area in Home Assistant moved back after every
        restart. Now the area only follows the hub when the room on the hub
        changes, the same rule the live room event follows; that needs the
        room each device was last seen in, so it is stored per hub entry.

        - A new device without an area goes to its hub room. New means
          created by this setup (its registry id is not in
          devices_before_setup), or not in a store that already existed: a
          device paired while Home Assistant ran is placed at the next
          start. suggested_area is not enough: the hub keeps the room of a
          split device on one part only, and whichever part registers first
          decides (MYGGSPRAY). Known limit: an area the user cleared on a
          device paired while Home Assistant ran, before the next start, is
          set once more at that start. Placing it when its registry entry is
          created would avoid that, but is not worth building before 0.5,
          where the room becomes a property of the whole device (RFC #77).
        - A known device moves when its hub room differs from the stored one
          (moved in the IKEA app while Home Assistant was stopped).
        - A room renamed in the IKEA app is the same room (same id): a device
          still in the area named after the old name follows to an area with
          the new name; a device the user put in another area stays.
        - Without a store yet (the first start of this version) a known
          device only has its room recorded.

        A device missing from the hub for a while keeps its stored room; one
        that is gone from the device registry is forgotten. A room first seen
        live (a device paired while Home Assistant runs) is recorded at the
        next start; a rename of it before then is not followed.
        """
        stored = await self._load_rooms()
        stored_devices = stored["devices"] if stored is not None else None
        stored_names = stored["room_names"] if stored is not None else {}
        rooms: dict[str, tuple[str, str]] = {}
        for registry_entry in self._own_registry().values():
            data = getattr(registry_entry.entity, "_json_data", None)
            if data is None:
                continue
            identifier = data.relation_id or data.id
            # Over all parts: the hub keeps the room on one part of a split device.
            room = _room_of(data)
            if room[0] or identifier not in rooms:
                rooms[identifier] = room

        device_reg = dr.async_get(self._hass)
        remembered = dict(stored_devices or {})
        placed = moved = 0
        for identifier, (room_id, room_name) in rooms.items():
            device_entry = get_device_by_identifier(
                device_reg, ("dirigera_platform", identifier), self._config_entry_id)
            if device_entry is None:
                continue
            is_new = device_entry.id not in devices_before_setup or (
                stored_devices is not None and identifier not in stored_devices)
            if is_new:
                if room_id and device_entry.area_id is None:
                    placed += self._set_device_area(device_reg, device_entry, identifier, room_name)
            elif stored_devices is not None and stored_devices[identifier] != room_id:
                logger.debug(f"Room of {identifier} changed from {stored_devices[identifier]!r} to {room_id!r} while stopped")
                moved += self._set_device_area(device_reg, device_entry, identifier, room_name)
            else:
                moved += self._follow_rename(device_reg, device_entry, identifier,
                                             stored_names.get(room_id), room_name)
            remembered[identifier] = room_id

        in_registry = {
            ident for device in dr.async_entries_for_config_entry(device_reg, self._config_entry_id)
            for domain, ident in device.identifiers if domain == "dirigera_platform"}
        self._rooms_seen = {i: r for i, r in remembered.items() if i in in_registry}
        names = {**stored_names, **{room_id: name for room_id, name in rooms.values() if room_id}}
        self._room_names = {r: names[r] for r in set(self._rooms_seen.values()) if r in names}
        if self._rooms_store is not None:
            await self._rooms_store.async_save(self._rooms_data())
        logger.info(f"Device area sync complete: {len(rooms)} devices, {placed} new placed, "
                    f"{moved} moved to a changed or renamed hub room")

    def _follow_rename(self, device_reg, device_entry, device_id: str, old_name: str | None, new_name: str) -> bool:
        """Move a device to the area of its renamed room when it is still in the
        area named after the old name. Returns whether it moved."""
        if not old_name or not new_name or old_name == new_name or device_entry.area_id is None:
            return False
        old_area = ar.async_get(self._hass).async_get_area_by_name(old_name)
        if old_area is None or old_area.id != device_entry.area_id:
            return False
        logger.debug(f"Room of {device_id} renamed from {old_name!r} to {new_name!r}")
        return self._set_device_area(device_reg, device_entry, device_id, new_name)

    def _room_renamed(self, room_id: str, new_name: str) -> None:
        """A live event shows a known room under a new name: every device of
        that room still in the area named after the old name follows."""
        old_name = self._room_names.get(room_id)
        self._room_names[room_id] = new_name
        try:
            device_reg = dr.async_get(self._hass)
            for device_id, device_room in self._rooms_seen.items():
                if device_room != room_id:
                    continue
                device_entry = get_device_by_identifier(
                    device_reg, ("dirigera_platform", device_id), self._config_entry_id)
                if device_entry is not None:
                    self._follow_rename(device_reg, device_entry, device_id, old_name, new_name)
        except Exception as ex:
            logger.error(f"Failed to follow the rename of room {room_id}: {ex}")
        self._save_rooms()

    def _rooms_data(self) -> dict:
        return {"devices": dict(self._rooms_seen), "room_names": dict(self._room_names)}

    def _save_rooms(self) -> None:
        """Written at once as a tracked task, not as a delayed save: a reload
        does not wait for a pending delayed write, so it would read the old
        room and move the device back. Room changes are rare."""
        if self._rooms_store is not None:
            self._hass.async_create_task(self._rooms_store.async_save(self._rooms_data()))

    async def _load_rooms(self) -> dict | None:
        """The stored rooms ({"devices": ..., "room_names": ...}); None without a
        store yet, or when it cannot be used (then nothing is moved, as on the
        first start of this version, and the store is rewritten)."""
        if self._rooms_store is None:
            return None
        try:
            data = await self._rooms_store.async_load()
        except Exception as ex:
            logger.warning(f"Could not read the stored rooms, moving no device this start; they are rewritten: {ex}")
            return None
        if data is None:
            return None
        if not (isinstance(data, dict) and isinstance(data.get("devices"), dict)
                and isinstance(data.get("room_names"), dict)):
            logger.warning("Stored rooms are not in the expected form, moving no device this start; they are rewritten")
            return None
        return data

    def on_error(self, ws:Any, ws_msg:str):
        logger.debug(f"on_error hub event listener {ws_msg}")
    
    def parse_scene_update(self, msg):
        # A sceneUpdated lists every controller trigger of the scene, each with
        # its own triggeredAt. Only a trigger that fired for this message is a
        # press; the others are history, e.g. another remote with the same
        # scene on that button. A trigger without triggeredAt never fired:
        # the hub leaves it out for a button or pattern not used yet (#65).
        # The scene-level lastTriggered is only used for firmware that sends
        # no triggeredAt at all; the hub sends two sceneUpdated per press and
        # the first still carries the previous lastTriggered.
        data = msg.get("data")
        if data is None or "triggers" not in data:
            logger.warning(f"discarding sceneUpdated without data/triggers: {msg}")
            return

        msg_time = _parse_hub_time(msg.get("time"))
        controller_triggers = [t for t in data["triggers"] if t.get("type") == "controller"]
        has_trigger_times = any(t.get("triggeredAt") for t in controller_triggers)
        for trigger in data["triggers"]:
            if trigger.get("type") != "controller":
                logger.debug(f"Trigger type : {trigger.get('type')} not controller ignoring...")
                continue

            details = trigger.get("trigger", {})
            if details.get("controllerType") != "shortcutController" or "deviceId" not in details:
                logger.debug(f"trigger not from a shortcutController, ignoring : {trigger}")
                continue

            if has_trigger_times:
                if not trigger.get("triggeredAt"):
                    logger.debug(f"trigger never fired, ignoring : {trigger}")
                    continue
                pressed_at = _parse_hub_time(trigger["triggeredAt"])
            else:
                pressed_at = _parse_hub_time(data.get("lastTriggered"))
            if pressed_at is None:
                # No trigger time at all: still fire, debounced on the message time.
                pressed_at = msg_time or datetime.datetime.now(datetime.timezone.utc)
            elif msg_time is not None and msg_time - pressed_at > STALE_TRIGGER_DELTA:
                logger.debug(f"trigger last fired at {pressed_at}, not part of this update, ignoring...")
                continue

            self._fire_controller_event(details["deviceId"], details.get("clickPattern"), pressed_at, from_scene=True)

        # Some bulbs only send colorMode in deviceStateChanged after a scene,
        # without the actual color values, so the scene's actions are applied
        # as state. Only when the scene ran for this message: a sceneUpdated
        # also arrives when a scene is edited, and its actions are a target,
        # not what the devices do.
        ran_at = _parse_hub_time(data.get("lastTriggered"))
        if msg_time is None or ran_at is None or msg_time - ran_at <= STALE_TRIGGER_DELTA:
            self._apply_scene_actions(msg)
        else:
            logger.debug(f"scene last ran at {ran_at}, not applying its actions")

    def _apply_scene_actions(self, msg):
        """Apply attributes from scene actions to the corresponding entities."""
        # _apply_message already stops after stop(); kept so this method is
        # safe on its own as well (#76's test calls it directly).
        if self._request_to_stop:
            return
        if "data" not in msg or "actions" not in msg["data"]:
            return

        for action in msg["data"]["actions"]:
            if action.get("type") != "device":
                continue

            device_id = action.get("deviceId")
            attributes = action.get("attributes")

            if not device_id or not attributes:
                continue

            if device_id not in self._own_registry():
                logger.debug(f"Scene action device {device_id} not in registry, skipping")
                continue

            registry_value = hub_event_listener.get_registry_entry(self._hub_key, device_id)
            if registry_value.__class__.__name__ != "registry_entry":
                continue

            entity = registry_value.entity

            applied = _apply_attributes(entity, attributes)
            updated = bool(applied)

            if updated:
                # No shutdown guard here: this runs on the event loop, and
                # _apply_message already returns once stop() was called (#42).
                try:
                    _push_state(entity)
                    if registry_value.cascade_entity is not None:
                        _push_state(registry_value.cascade_entity)
                    logger.debug(f"Scene action: HA state updated for {device_id}")
                except Exception as ex:
                    # One entity must not keep the scene's other actions from applying.
                    logger.warning(f"Scene action: failed to update state for {device_id}: {ex}")

    def parse_remote_press_event(self, msg):
        """
        Parse remotePressEvent messages from lightController remotes like STYRBAR and RODRET.
        These remotes send direct press events without needing scene configuration.
        """
        data = msg.get("data")
        if data is None or "id" not in data or "clickPattern" not in data:
            logger.warning(f"discarding remotePressEvent without data/id/clickPattern: {msg}")
            return

        # The hub's own time, so both paths debounce on one clock.
        pressed_at = _parse_hub_time(msg.get("time")) or datetime.datetime.now(datetime.timezone.utc)
        self._fire_controller_event(data["id"], data["clickPattern"], pressed_at)

    def _fire_controller_event(self, device_id, click_pattern, pressed_at, from_scene=False):
        """
        Fire one dirigera_platform_event per physical press. Both the
        remotePressEvent and the sceneUpdated path end here.

        A remote that sends remotePressEvent has one source of truth for its
        presses, so the scene path stands down for it: the two cannot be
        matched by time, because for a long press the scene fires when the
        hold is recognised and remotePressEvent only on release (#65). The
        scene path stays for remotes that never send remotePressEvent. Until a
        remote's first remotePressEvent after a restart, the debounce below
        still merges the two. pressed_at is a tz-aware hub timestamp.
        """
        trigger_type = CLICK_PATTERNS.get(click_pattern)
        if trigger_type is None:
            logger.debug(f"click_pattern : {click_pattern} not in list of types...ignoring")
            return

        # Multi-button controllers: device_id xxx_2 means button 2 of xxx_1
        device_id_for_registry = device_id
        match = re.search('(([0-9]|[a-z]|-)*)_([0-9])+', device_id)
        if match is not None:
            device_id_for_registry = f"{match.groups()[0]}_1"
            trigger_type = f"button{int(match.groups()[2])}_{trigger_type}"

        registry_value = hub_event_listener.get_registry_entry(self._hub_key, device_id_for_registry)
        if registry_value.__class__.__name__ != "registry_entry":
            logger.debug(f"id : {device_id_for_registry} not a controller in the registry, ignoring...")
            return
        entity = registry_value.entity
        ha_device_id = entity.registry_entry.device_id
        if from_scene and ha_device_id in remotes_with_press_events:
            logger.debug(f"{ha_device_id} sends remotePressEvent, ignoring its scene trigger")
            return
        if not from_scene:
            remotes_with_press_events.add(ha_device_id)

        # Debounce: the hub reports one press more than once (IKEA bug
        # workaround). Only a fired press is stored, so the map never moves
        # back in time.
        unique_key = f"{entity.registry_entry.device_id}_{trigger_type}"
        last_fired = controller_trigger_last_time_map.get(unique_key)
        if last_fired is not None and pressed_at - last_fired < DEBOUNCE_DELTA:
            logger.debug(f"Debouncing duplicate event for {unique_key}")
            return
        controller_trigger_last_time_map[unique_key] = pressed_at

        event_data = {
            "type": trigger_type,
            "device_id": entity.registry_entry.device_id,
            ATTR_ENTITY_ID: entity.registry_entry.entity_id
        }
        self._hass.bus.async_fire(event_type="dirigera_platform_event", event_data=event_data)
        logger.debug(f"Event fired.. {event_data}")

    def _schedule_stale_recheck(self):
        """Coalesce a burst of deviceRemoved events into one reconciliation.

        The first event of a burst arms a short timer; events arriving within
        the window are dropped, and the single reconciliation fires on the
        trailing edge.
        """
        if self._stale_check_pending:
            return
        self._stale_check_pending = True
        self._loop.call_later(self.STALE_RECHECK_DEBOUNCE, self._run_coalesced_stale_check)

    def _run_coalesced_stale_check(self):
        self._stale_check_pending = False
        # The timer can outlive the listener.
        if self._request_to_stop:
            return
        # Imported here: the tests load this module standalone.
        from .stale_devices import async_remove_stale_devices
        # A background task of the config entry: unloading the entry cancels
        # it, so a /devices fetch that is still on its way never comes back to
        # compare against an entry that is gone.
        entry = self._hass.config_entries.async_get_entry(self._config_entry_id)
        if entry is None:
            return
        entry.async_create_background_task(
            self._hass,
            async_remove_stale_devices(self._hass, self._config_entry_id, self._hub),
            "dirigera_platform stale device recheck",
        )

    def on_message(self, ws:Any, ws_msg:str):
        """WebSocket thread: parse the message and hand it to the event loop.

        Nothing else happens on this thread. The entity models, the registry
        and Home Assistant itself belong to the event loop; this thread used
        to write the same models the loop reads and writes.
        """
        logger.debug(f"rcvd message : {ws_msg}")
        try:
            msg = json.loads(ws_msg)
        except ValueError:
            logger.warning(f"discarding hub message that is not JSON: {ws_msg}")
            return
        try:
            self._loop.call_soon_threadsafe(self._apply_message, msg)
        except RuntimeError:
            # The event loop is closed: Home Assistant is shutting down and
            # nobody is left to tell (#42).
            pass

    def _registered_id(self, id, device_type):
        """The id under which the entity for a hub device is registered: its
        own, or for a part of a split device without an entity of its own the
        part its events are routed to. None when there is none."""
        registry = self._own_registry()
        if id in registry:
            return id

        # For electricalSensor events (GRILLPLATS/TOFSMYGGA _2 -> _1)
        if device_type == "electricalSensor" and id.endswith("_2"):
            outlet_id = id[:-2] + "_1"
            if outlet_id in registry:
                logger.debug(f"Routing electricalSensor {id} events to outlet {outlet_id}")
                return outlet_id

        # For split-device environmentSensors (TIMMERFLOTTE: temp + humidity)
        # Try _1 suffix pattern first, then search by relationId prefix
        if device_type == "environmentSensor":
            if id.endswith("_2"):
                sibling_id = id[:-2] + "_1"
                if sibling_id in registry:
                    logger.debug(f"Routing environmentSensor {id} events to sibling {sibling_id}")
                    return sibling_id
            # Try finding any registered device sharing the same base ID (relationId)
            base_id = id.rsplit("_", 1)[0] if "_" in id else id
            for reg_id in registry:
                if reg_id.startswith(base_id) and reg_id != id:
                    logger.debug(f"Routing environmentSensor {id} events to related {reg_id}")
                    return reg_id
        return None

    def _apply_message(self, msg):
        """Event loop: the one place where a hub message changes anything."""
        # A message handed over before stop() runs after it; the listener's
        # entities are being torn down by then.
        if self._request_to_stop:
            return
        try:
            if not isinstance(msg, dict) or "type" not in msg:
                logger.debug(f"'type' not found in incoming message, discarding : {msg}")
                return 
            
            if msg['type'] == "sceneUpdated":
                logger.debug(f"Found sceneUpdated message... ")
                return self.parse_scene_update(msg)

            if msg['type'] == "remotePressEvent":
                logger.debug(f"Found remotePressEvent message... ")
                return self.parse_remote_press_event(msg)

            if msg['type'] in DEVICE_SET_EVENTS:
                return self.parse_device_set_event(msg)

            # Handle deviceAdded events - trigger dynamic discovery
            if msg['type'] == "deviceAdded":
                if "data" in msg and "id" in msg['data']:
                    device_id = msg['data']['id']
                    device_type = msg['data'].get('deviceType', msg['data'].get('type'))
                    if device_type and self._discovery_coordinator is not None:
                        logger.info(f"Device added event received: {device_id} (type: {device_type})")
                        self._hass.async_create_task(
                            self._discovery_coordinator.discover_device(device_id, device_type)
                        )
                    else:
                        logger.debug(f"deviceAdded event without discovery coordinator or type: {msg}")
                return

            # deviceRemoved: rerun the stale-device comparison against a fresh
            # /devices instead of removing this id blindly. A split device only
            # goes once its last half is gone from the hub. The hub may deliver
            # the same removal more than once; the recheck is coalesced so one
            # removal is one reconciliation (see _schedule_stale_recheck).
            if msg['type'] == "deviceRemoved":
                if "data" in msg and "id" in msg['data']:
                    logger.info(f"Device removed event received: {msg['data']['id']}")
                    self._schedule_stale_recheck()
                return

            if msg['type'] != "deviceStateChanged":
                logger.debug(f"discarding non state message: {msg}")
                return 

            if "data" not in msg or "id" not in msg['data']:
                logger.info(f"discarding message as  key 'data' or 'data/id' not found: {msg}")
                return  
            
            info = msg['data'] 
            id = info['id']
            event_id = id

            device_type = None
            if "deviceType" in info:
                device_type = info["deviceType"]
            elif "type" in info:
                device_type = info["type"]
            else:
                logger.warning("expected type or deviceType in JSON, none found, ignoring...")
                return

            logger.debug(f"device type of message {device_type}")

            # Its own entity, or for a part of a split device the entity its
            # events are routed to.
            id = self._registered_id(event_id, device_type)
            if id is None:
                id = event_id
                # During a resync (reconnect or safety net) we replay every device, so an
                # unknown id here is just a device with no entity — skip it
                # silently rather than firing discovery for it on every
                # reconnect. See issue #39.
                if self._resyncing:
                    return
                # Unknown device - try to discover it
                if self._discovery_coordinator is not None:
                    # Same split as at startup: remotes are recognised by
                    # type, every other device by deviceType.
                    discovery_type = "controller" if info.get("type") == "controller" else device_type
                    logger.debug(f"Unknown device detected: {id} (type: {discovery_type}), triggering discovery")
                    self._hass.async_create_task(
                        self._discovery_coordinator.discover_device(id, discovery_type)
                    )
                else:
                    logger.info(f"discarding message as device for id: {id} not found for msg: {msg}")
                return

            registry_value = hub_event_listener.get_registry_entry(self._hub_key, id)
            entity = registry_value.entity
            # False for another part of a split device routed to this entity.
            own_device = event_id == entity._json_data.id

            has_attributes = info.get("attributes") is not None
            fields_before, room_before, name_before = _device_view(entity)

            _apply_device_fields(entity, info, own_device=own_device)
            if has_attributes:
                _apply_attributes(entity, info["attributes"], own_device=own_device)

            if self._resyncing:
                # The model is the hub's again; what changed while the
                # WebSocket was down is not a change happening now (a blind
                # that moved meanwhile is not moving, #207).
                resynced = getattr(entity, "state_resynced", None)
                if resynced is not None:
                    resynced()

            # What has to follow is read from the device before and after, not
            # from which keys the event happened to carry.
            fields_after, room_after, name_after = _device_view(entity)
            ha_device = entity._json_data.relation_id or id

            if room_after != room_before:
                # The Home Assistant area follows the room on the hub when the
                # room changes, and only then: a user may keep a device in
                # another area, and every reconnect replays every room.
                logger.debug(f"Room of {id} changed from {room_before!r} to {room_after!r}")
                room_name = _room_of(entity._json_data)[1]
                if room_after and self._room_names.get(room_after, room_name) != room_name:
                    # Moved into a room that was renamed as well: its other
                    # devices follow the rename first.
                    self._room_renamed(room_after, room_name)
                self._update_device_area(ha_device, room_name)
                self._rooms_seen[ha_device] = room_after
                if room_after:
                    self._room_names[room_after] = room_name
                self._save_rooms()
            elif room_after and info.get("room") is not None:
                # Same room, and this event carries it: renamed in the IKEA
                # app when its name differs from the one last seen. Only an
                # event with the room counts: the model of a room-mate whose
                # event has not come yet still holds the old name, and would
                # read as a rename back (review 16-11).
                room_name = _room_of(entity._json_data)[1]
                if self._room_names.get(room_after, room_name) != room_name:
                    self._room_renamed(room_after, room_name)

            if name_after != name_before and name_after is not None:
                self._hass.async_create_task(
                    self._update_device_name(ha_device, name_after)
                )

            # An event with attributes always writes state (a light's command
            # record and the push throttle decide from there); without them
            # only when the device itself changed, e.g. its reachability.
            # _push_state skips a light the device set mode keeps out of Home
            # Assistant (#73/#76).
            if has_attributes or fields_after != fields_before:
                _push_state(entity)

                if registry_value.cascade_entity is not None:
                    # Cascade the update: a device set takes its state from
                    # this light, whether or not the light is imported itself.
                    logger.debug(f"Cascading to cascade entity : {registry_value.cascade_entity.unique_id}")
                    _push_state(registry_value.cascade_entity)

            # A light joined or left a device set (#73/#76). Plain state events
            # carry no deviceSet; this one has deviceSet [{id, name, icon}] or [].
            # The manager ignores ids that are not lights it tracks. Queued
            # after the state write above: joining a set under "Sets only"
            # disables the light, and Home Assistant warns about a write that
            # reaches an entity after it was disabled.
            manager = self._device_sets() if "deviceSet" in info else None
            if manager is not None and self._membership_live is not None and not self._resyncing:
                self._membership_live.add(event_id)
            if manager is not None:
                self._schedule(
                    manager.async_handle_membership, event_id, info["deviceSet"], entity._json_data.room
                )

        except Exception:
            # Visible at default log level: a swallowed DEBUG here used to hide
            # every event-processing bug behind silently-stale entities.
            logger.warning(f"error processing hub event: {msg}", exc_info=True)

    def _send_keepalive(self):
        """Send an application-level text frame to reset the hub's inactivity timer.

        An empty string may be ignored by the hub as a zero-length frame.
        Send a minimal JSON message instead — if the hub rejects it, the
        WebSocket stays open (it's not a protocol error, just an unknown message).
        """
        if self._wsapp and not self._request_to_stop:
            try:
                self._wsapp.send('{"type":"ping"}')
                logger.info("WebSocket keepalive sent")
            except Exception as ex:
                logger.warning(f"WebSocket keepalive failed: {ex}")
        if not self._request_to_stop:
            self._keepalive_timer = threading.Timer(
                self.KEEPALIVE_INTERVAL, self._send_keepalive)
            self._keepalive_timer.daemon = True
            self._keepalive_timer.start()

    def _start_keepalive(self):
        """Start the periodic keepalive timer."""
        self._stop_keepalive()
        self._keepalive_timer = threading.Timer(
            self.KEEPALIVE_INTERVAL, self._send_keepalive)
        self._keepalive_timer.daemon = True
        self._keepalive_timer.start()
        logger.info(f"WebSocket keepalive timer started ({self.KEEPALIVE_INTERVAL}s interval)")

    def _stop_keepalive(self):
        """Cancel the keepalive timer if running."""
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None

    def _on_close(self, ws, close_status_code, close_msg):
        # Log cleanly when the hub closes the WebSocket. Dirigera sends
        # status 1000 with message "disconnected due to inactivity" when
        # it drops idle connections — this makes that visible at INFO level.
        session_duration = None
        if self._session_started_at is not None:
            session_duration = time.time() - self._session_started_at
        if session_duration is not None:
            logger.info(f"Dirigera WebSocket closed (code={close_status_code}, msg={close_msg}, session_duration={session_duration:.0f}s)")
        else:
            logger.info(f"Dirigera WebSocket closed (code={close_status_code}, msg={close_msg})")

    def _on_open(self, ws):
        self._session_started_at = time.time()
        logger.info("Dirigera WebSocket opened")
        self._start_keepalive()
        # On a *reconnect* (not the first open), the hub only delivers state
        # changes going forward — anything that changed while we were
        # disconnected is never replayed. Dirigera hubs drop the WebSocket
        # fairly often (every few minutes on some setups), so without this
        # entities silently accumulate stale state until they happen to change
        # again. Re-pull all device state on reconnect. See issue #39.
        if self._has_opened:
            logger.info("WebSocket reconnected — scheduling device state resync")
            self._loop.call_soon_threadsafe(
                lambda: self._hass.async_create_task(self._resync_all_states())
            )
        self._has_opened = True

    # --- the safety net (#207) ------------------------------------------------

    async def run_safety_net(self) -> None:
        """One run of the safety net (SAFETY_NET_INTERVAL). Setup schedules it
        with Home Assistant's async_track_time_interval."""
        if self._request_to_stop:
            return
        if self._resync_lock.locked():
            # A resync is going (a reconnect, or the last run on a slow hub):
            # it brings the same, and a second one would only queue behind it.
            logger.debug("Safety net skipped: a resync is still going")
            return
        async with self._resync_lock:
            # No prune: while connected a deleted set sends deviceSetDeleted,
            # and a set the user empties for a while keeps its entity.
            await self._resync_devices("safety net", prune_empty_sets=False)
            # Then the scenes, in the same run and lock. A failure of either
            # fetch is logged and leaves the other alone.
            await self._sync_scenes()

    async def _sync_scenes(self) -> None:
        sync = self._scene_sync()
        if sync is None:
            return
        try:
            await sync.async_refresh()
        except Exception as ex:  # pylint: disable=broad-except
            logger.warning(f"Scene sync (safety net) failed: {ex}")

    async def _resync_all_states(self, reason: str = "reconnect", prune_empty_sets: bool = True):
        """Re-pull current state for all devices and apply it like events:
        after a reconnect, so changes missed during the disconnect gap are
        caught up (#39), and as the safety net every SAFETY_NET_INTERVAL, for
        what an event did not bring (#207). One /devices fetch (no per-device
        hammering); discovery is suppressed during the replay, and an entity
        with state_resynced() is told its model is a snapshot.

        The snapshot is as old as the moment the hub built it. Events that
        arrive while it is fetched are applied at once, as always, and what
        they wrote is left out of the snapshot: it is newer (see live_writes).
        A device no event touched gets its whole row."""
        async with self._resync_lock:
            await self._resync_devices(reason, prune_empty_sets)

    async def _resync_devices(self, reason: str, prune_empty_sets: bool) -> None:
        """The resync itself; the caller holds _resync_lock."""
        self._membership_live = set()
        try:
            with live_writes() as newer:
                try:
                    devices = await self._hass.async_add_executor_job(self._hub.get, "/devices")
                except Exception as ex:
                    logger.warning(f"State resync ({reason}) failed to fetch /devices: {ex}")
                    return
            if not devices:
                return
            self._resyncing = True
            try:
                for device in devices:
                    self._apply_message({"type": "deviceStateChanged",
                                         "data": self._without_newer(device, newer)})
            finally:
                self._resyncing = False
        finally:
            # Also when the run is cancelled (unload), which is no Exception.
            self._membership_live = None
        # Every few minutes for the safety net: not worth an info line.
        log = logger.info if reason == "reconnect" else logger.debug
        log(f"State resync ({reason}) applied for {len(devices)} devices")
        # Queued behind the membership updates of the replay (#76): a set
        # no light is in any more was deleted while we were disconnected.
        manager = self._device_sets()
        if manager is not None and prune_empty_sets:
            self._schedule(manager.async_prune_empty_sets)

    # What says which device a row is; never left out of it.
    _ROW_IDENTITY = frozenset({"id", "type", "deviceType"})

    def _without_newer(self, device, newer):
        """A snapshot row without what was written to its entity after the
        snapshot was asked for."""
        if not isinstance(device, dict) or "id" not in device:
            return device
        if self._membership_live and device["id"] in self._membership_live and "deviceSet" in device:
            device = {key: value for key, value in device.items() if key != "deviceSet"}
        entry = self._own_registry().get(self._registered_id(
            device["id"], device.get("deviceType", device.get("type"))))
        if entry is None:
            return device
        fields = newer.fields(entry.entity) - self._ROW_IDENTITY
        attributes = newer.attributes(entry.entity)
        # Per entity, so a commanded outlet also drops the attributes of its
        # _2 electricalSensor row (routed to it) for this snapshot.
        commanded = newer.commanded(entry.entity)
        if not fields and not attributes and not commanded:
            return device
        row = {key: value for key, value in device.items() if key not in fields}
        if commanded:
            row.pop("attributes", None)
        elif isinstance(row.get("attributes"), dict):
            row["attributes"] = {key: value for key, value in row["attributes"].items()
                                 if key not in attributes}
        return row

    def create_listener(self):
        try:
            logger.info("Starting dirigera hub event listener")
            self._wsapp = websocket.WebSocketApp(
                self._hub.websocket_base_url,
                header={"Authorization": f"Bearer {self._hub.token}"},
                on_message=self.on_message,
                on_error=self.on_error,
                on_open=self._on_open,
                on_close=self._on_close)
            # ping_interval sends WebSocket ping frames to keep the connection alive.
            # Without this, the Dirigera hub disconnects after ~5 minutes of inactivity.
            # Note: some Dirigera firmware versions still disconnect after ~1-2 hours
            # even with pings — the hub's inactivity timer ignores ping frames.
            self._wsapp.run_forever(
                sslopt={"cert_reqs": ssl.CERT_NONE},
                ping_interval=30,
                ping_timeout=10)
        except Exception as ex:
            logger.error("Error creating event listener...")
            logger.error(ex)
        finally:
            self._stop_keepalive()

    def stop(self):
        # NOTE: blocks until the thread exits — call from the event loop via
        # hass.async_add_executor_job (see async_unload_entry).
        logger.info("Listener request for stop..")

        self._request_to_stop = True
        self._stop_event.set()
        try:
            #self._hub.stop_event_listener()
            if self._wsapp is not None:
                self._wsapp.close()
        except:
            pass
        self.join()
        hub_event_listener.unregister_hub(self._hub_key)
        logger.info("Listener stopped..")

    def run(self):
        while True:
            # Blocking call — returns when the WebSocket connection ends
            # (either clean close from hub, our stop() call, or exception).
            self.create_listener()
            logger.debug("Listener thread complete...")
            if self._request_to_stop:
                break
            logger.warning("Failed to create listener or listener exited, sleeping 10 seconds before retrying")
            # Interruptible: stop() sets the event so an unload/reload does
            # not have to sit out the retry backoff. (This used to be two
            # stacked time.sleep(10) calls — 20s, with the log saying 10.)
            if self._stop_event.wait(timeout=10):
                break