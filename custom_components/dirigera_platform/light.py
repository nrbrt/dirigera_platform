import asyncio
import logging

from dirigera import Hub
from dirigera.devices.light import Light

from homeassistant import config_entries, core
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)

from homeassistant.const import CONF_IP_ADDRESS, CONF_TOKEN
from homeassistant.core import HomeAssistantError, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, DEVICE_SET_MODEL
from .device_sets import (
    DeviceSet,
    DeviceSetIndex,
    light_wanted,
    resolve_device_set_mode,
    sets_wanted,
)
from .hub_event_listener import (
    get_device_by_identifier,
    hub_event_listener,
    live_writes,
    note_command,
    registry_entry,
    replace_model,
    to_snake_case,
)
from .hub_writer import writer_for
from .pending_command import PendingCommand

# Seconds after the on-PATCH of a bulb that was off before a lightLevel is
# sent (live hub 16-11-2026, TRADFRI: 0.75 s and later right 3 of 3).
LEVEL_AFTER_ON_S = 0.8

logger = logging.getLogger("custom_components.dirigera_platform")


def _bound_to_kelvin(bound):
    # Some bulbs (e.g. KAJPLATS) report their colour temperature range in
    # mireds (153..454) while colorTemperature itself is in Kelvin. This is the
    # rule dirigera's Light.set_color_temperature() uses for its range check,
    # so the range HA offers is exactly the range the library accepts.
    if bound is not None and 0 < bound < 1000:
        return round(1_000_000 / bound)
    return bound


def _kelvin_range(attributes):
    """(warmest, coldest) in Kelvin, whatever order and unit the bulb reports."""
    bounds = [_bound_to_kelvin(attributes.color_temperature_min),
              _bound_to_kelvin(attributes.color_temperature_max)]
    if None in bounds:
        return None, None
    return min(bounds), max(bounds)

async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    """Set up the lights and device sets of a config entry."""
    logger.debug("LIGHT Starting async_setup_entry")
    config = hass.data[DOMAIN][config_entry.entry_id]

    hub = Hub(config[CONF_TOKEN], config[CONF_IP_ADDRESS])

    all_lights = config["gateway"].lights
    logger.debug("Found {} total of all light entities to setup...".format(len(all_lights)))

    # The index was built by __init__.py for the registry sync.
    manager = DeviceSetManager(
        hass, config_entry, hub, config, config["device_set_index"], all_lights, async_add_entities
    )
    # The hub event listener finds the manager here.
    config["device_sets"] = manager
    manager.async_setup()

    # Register callback and known devices with discovery coordinator. A light
    # paired at runtime goes through the manager, so the mode applies to it.
    discovery = config.get("discovery")
    if discovery:
        discovery.register_platform_callback("light", manager.async_add_discovered)
        for light in all_lights:
            discovery.register_known_device(light._json_data.id)
        logger.debug(f"Registered {len(all_lights)} lights with discovery coordinator")

    logger.debug("LIGHT Complete async_setup_entry")


class DeviceSetManager:
    """Applies the device set mode to the light entities, at setup and while
    Home Assistant runs.

    Every light on the hub has an ikea_bulb, imported or not: the bulb is also
    the model its set reads state from. Which entities are imported is decided
    by the device set mode; the registry side (disabling what the mode leaves
    out, re-enabling what it takes back) is done by
    sync_platform_entity_registry in __init__.py, the same pass that applies
    the platform selection (#47).

    data is the entry's dict in hass.data, which holds the platforms set up.
    Every method runs on the event loop; the hub event listener calls them
    through call_soon_threadsafe.
    """

    def __init__(self, hass, config_entry, hub, data, index: DeviceSetIndex, bulbs, async_add_entities):
        self._hass = hass
        self._entry = config_entry
        self._hub = hub
        self._data = data
        self._mode = resolve_device_set_mode(data)
        self._index = index
        self._bulbs = {bulb.unique_id: bulb for bulb in bulbs}
        self._add_entities = async_add_entities
        self._sets = {}
        # Unique ids handed to async_add_entities. An entity instance can be
        # added once; one removed since comes back through the entry reload
        # Home Assistant schedules when its registry entry is re-enabled.
        self._added = set()

    @callback
    def async_setup(self) -> None:
        """Add the entities the mode imports."""
        logger.debug(f"Device set mode {self._mode}, {len(self._index.set_ids())} device_sets")
        if sets_wanted(self._mode):
            self._add([self._make_set_entity(set_id) for set_id in self._index.set_ids()])
        self._add(self._wanted_bulbs())

    @callback
    def async_add_discovered(self, entities) -> None:
        """Discovery callback for lights paired while Home Assistant runs."""
        for bulb in entities:
            self._bulbs[bulb.unique_id] = bulb
            data = bulb._json_data
            self.async_handle_membership(bulb.unique_id, data.device_set, data.room)
        self._add(self._wanted_bulbs())

    @callback
    def async_handle_membership(self, light_id, device_sets, room=None) -> None:
        """A light's deviceSet changed: it joined a set, left one, or both."""
        if light_id not in self._bulbs:
            return
        change = self._index.set_membership(light_id, device_sets, room)
        if not change.changed:
            return
        logger.info(f"Light {light_id} device set: left {change.left}, joined {change.joined}")
        if change.renamed:
            self._async_renamed(change.renamed)
        for set_id in (change.left, change.joined):
            if set_id is not None:
                self._async_members_changed(set_id)
        if change.left or change.joined:
            self._async_apply_mode()

    @callback
    def async_handle_set_event(self, event_type, set_id, name=None) -> None:
        """deviceSetCreated / deviceSetUpdated / deviceSetDeleted."""
        logger.info(f"{event_type} for device set {set_id} ({name})")
        if event_type == "deviceSetDeleted":
            self._async_delete_set(set_id)
        elif self._index.get(set_id) is None:
            # No entity yet: a set has no light to take its state and
            # capabilities from until the first one joins.
            self._index.create(set_id, name)
        elif self._index.rename(set_id, name):
            self._async_renamed(set_id)

    @callback
    def async_prune_empty_sets(self) -> None:
        """Drop sets no light is in any more (deleted while disconnected)."""
        for set_id in self._index.empty_set_ids():
            logger.info(f"Device set {set_id} has no lights after the resync, removing it")
            self._async_delete_set(set_id)

    # --- internals --------------------------------------------------------

    def _add(self, entities) -> None:
        new = [e for e in entities if e.unique_id not in self._added]
        if not new:
            return
        self._added.update(e.unique_id for e in new)
        self._add_entities(new)

    def _wanted_bulbs(self) -> list:
        return [
            bulb for light_id, bulb in self._bulbs.items()
            if light_wanted(self._mode, self._index.in_set(light_id))
        ]

    def _member_bulbs(self, set_id) -> list:
        """The ikea_bulb entities of a set's current members, controller first."""
        device_set = self._index.get(set_id)
        if device_set is None:
            return []
        return [self._bulbs[light_id] for light_id in device_set.members if light_id in self._bulbs]

    def _make_set_entity(self, set_id):
        controller = self._bulbs[self._index.controller_id(set_id)]
        entity = ikea_bulb_device_set(self._hub, self._index.get(set_id), controller,
                                      members=lambda: self._member_bulbs(set_id))
        self._sets[set_id] = entity
        return entity

    def _async_members_changed(self, set_id) -> None:
        """Create the set's entity, or point it at its current first light."""
        entity = self._sets.get(set_id)
        controller = self._bulbs.get(self._index.controller_id(set_id))
        if entity is None:
            if sets_wanted(self._mode) and controller is not None:
                entity = self._make_set_entity(set_id)
                self._add([entity])
            return
        entity.set_controller(controller)

    def _async_renamed(self, set_id) -> None:
        # The set entity takes its name from its device, so renaming the
        # device renames the entity too, and Home Assistant writes its state.
        name = self._index.get(set_id).name
        device_reg = dr.async_get(self._hass)
        device = get_device_by_identifier(device_reg, (DOMAIN, set_id), self._entry.entry_id)
        if device is not None and device.name != name:
            # The device's own name, never the name the user gave it (#72).
            device_reg.async_update_device(device.id, name=name)

    def _async_delete_set(self, set_id) -> None:
        former = self._index.delete(set_id)
        entity = self._sets.pop(set_id, None)
        if entity is not None:
            entity.set_controller(None)
        hub_event_listener.unregister(self._hub.websocket_base_url, set_id)
        self._added.discard(set_id)

        # The hub deleted the set, and a new set gets a new id, so there is
        # nothing to keep: remove its entity and detach its device rather than
        # disabling them. Removing the registry entry also takes the live
        # entity out of Home Assistant.
        entity_reg = er.async_get(self._hass)
        entity_id = entity_reg.async_get_entity_id("light", DOMAIN, set_id)
        if entity_id is not None:
            entity_reg.async_remove(entity_id)
        device_reg = dr.async_get(self._hass)
        device = get_device_by_identifier(device_reg, (DOMAIN, set_id), self._entry.entry_id)
        if device is not None:
            device_reg.async_update_device(device.id, remove_config_entry_id=self._entry.entry_id)

        if former:
            self._async_apply_mode()

    def _async_apply_mode(self) -> None:
        """Bring the light entities in line with the mode after a change."""
        # Imported here: __init__.py imports this module through ikea_gateway.
        from . import sync_platform_entity_registry

        platforms = self._data.get("platforms")
        if platforms is not None:
            # Disabling a light's registry entry takes it out of Home
            # Assistant. Re-enabling one makes Home Assistant reload the entry
            # about 30 s later, which adds it again.
            sync_platform_entity_registry(
                self._hass, self._entry, platforms,
                self._index.hidden_unique_ids(self._mode),
            )
        # A light that was never added can be added right away.
        self._add(self._wanted_bulbs())


def brightness_to_hub_level(brightness) -> int:
    """HA brightness (0-255) to the hub's lightLevel (1-100).

    Rounded, not truncated, both ways: truncating turned a scene's 158 into
    61 % and back into 155. Clamped because HA brightness 1-2 rounds to 0."""
    return max(1, min(100, round(brightness * 100 / 255)))


def hub_level_to_brightness(level):
    """The hub's lightLevel (1-100) to HA brightness (0-255)."""
    if level is None:
        return None
    return round(level * 255 / 100)


def hub_value(attribute, value):
    """`value` as the hub stores and reports it for this model attribute.

    The hub keeps colour values in the bulb's own units, so what it reports
    after a PATCH is not what was sent (measured on a live hub, 10-11-2026):

    * colour temperature in whole mireds, rounded DOWN: 2700 K comes back as
      2702 (370.4 -> 370 mireds), 2900 as 2906 (344.8 -> 344), 3200 as 3205;
    * hue in 1/65536 of the circle, rounded to NEAREST: 30 comes back as
      29.9981689453125, 30.0035 as 30.003662109375;
    * saturation in 1/254, rounded DOWN: 0.5 stays 0.5, 0.5039 (127.99/254)
      becomes 0.5, 0.998 becomes 253/254.

    This is the one place that knows; the record of a pending command and the
    value the model assumes both come from here, so the hub's echo matches and
    the model never holds a value the hub does not have. Everything else
    (isOn, lightLevel, colorMode) is stored as sent."""
    if attribute == "color_temperature":
        return int(1_000_000 / int(1_000_000 / value))
    if attribute == "color_hue":
        return round(value * 65536 / 360) * 360 / 65536
    if attribute == "color_saturation":
        return int(value * 254) / 254
    return value


def light_turn_on_patch(can_receive, kwargs, kelvin_range):
    """The PATCH body that turns a bulb on with what HA asked for, as one request (#71).

    Each attribute goes in its own item of the list. The hub applies all items
    of one PATCH, while one item holding isOn and lightLevel together comes
    back 202 with lightLevel silently dropped (Tekno74 in #71). Hue and
    saturation stay together in one item, as the library has always sent them.
    The order is isOn, then colour, then lightLevel (see below).

    Returns (data, updates): the PATCH body and the model values it stands
    for, as the hub will store them (clamped, rounded, and through
    hub_value). Those are the values the hub's echo is compared with, and
    what the model assumes once the hub accepted the PATCH.

    isOn is always part of the body: a lightLevel sent on its own to a bulb
    that is off is not applied by the hub (measured 10-11-2026).
    """
    items = [{"isOn": True}]
    updates = {"is_on": True}

    if ATTR_COLOR_TEMP_KELVIN in kwargs and "colorTemperature" in can_receive:
        ct = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
        low, high = kelvin_range
        if low is not None and high is not None:
            ct = max(low, min(high, ct))
        items.append({"colorTemperature": ct})
        updates["color_temperature"] = hub_value("color_temperature", ct)
        updates["color_mode"] = "temperature"

    if ATTR_HS_COLOR in kwargs and "colorHue" in can_receive and "colorSaturation" in can_receive:
        hue, saturation = kwargs[ATTR_HS_COLOR]
        hue = max(0.0, min(360.0, float(hue)))
        saturation = max(0.0, min(1.0, float(saturation) / 100))  # 0-1 at IKEA
        items.append({"colorHue": hue, "colorSaturation": saturation})
        updates["color_hue"] = hub_value("color_hue", hue)
        updates["color_saturation"] = hub_value("color_saturation", saturation)
        updates["color_mode"] = "color"

    # lightLevel last. On TRADFRI bulbs (live hub, 9-11-2026) a
    # colorTemperature item after lightLevel left the level part-way:
    # 6 of 6 runs wrong (asked 85, got 55; asked 24, got 55), against 6 of 6
    # right with colorTemperature first. A level alone always arrived.
    if ATTR_BRIGHTNESS in kwargs and "lightLevel" in can_receive:
        level = brightness_to_hub_level(int(kwargs[ATTR_BRIGHTNESS]))
        items.append({"lightLevel": level})
        updates["light_level"] = level

    return [{"attributes": item} for item in items], updates


class ikea_bulb(LightEntity):
    def __init__(self, hub, json_data : Light) -> None:
        logger.debug("ikea_bulb ctor...")
        self._hub = hub
        self._json_data = json_data
        # What this bulb was last told and the hub has not confirmed yet.
        self._pending = PendingCommand(self._call_later, self._hub_values_stand)
        # A level for a bulb that was just turned on waits (see async_turn_on):
        # the loop time before which a lightLevel is not sent, and a counter
        # so a newer command drops a level that is still waiting.
        self._level_not_before = 0.0
        self._command_seq = 0

        # Register the device for updates
        hub_event_listener.register(self._hub.websocket_base_url, self._json_data.id, registry_entry(self))

        self.set_state()

    def set_state(self):
        # The colour modes this bulb supports, from what it can receive.
        logger.debug("Set State of bulb..")
        color_modes = []
        for cap in self._json_data.capabilities.can_receive:
            if cap == "lightLevel":
                color_modes.append(ColorMode.BRIGHTNESS)
            elif cap == "colorTemperature":
                color_modes.append(ColorMode.COLOR_TEMP)
            elif cap == "colorHue" or cap == "colorSaturation":
                # colorHue and colorSaturation both map to HS — add it once
                if ColorMode.HS not in color_modes:
                    color_modes.append(ColorMode.HS)

        # Based on documentation here
        # https://developers.home-assistant.io/docs/core/entity/light#color-modes
        if len(color_modes) > 1 and ColorMode.BRIGHTNESS in color_modes:
            # If there are more color modes which means we have either temperature
            # or HueSaturation. then lets make sure BRIGHTNESS is not part of it
            # as per above documentation
            color_modes.remove(ColorMode.BRIGHTNESS)

        self._supported_color_modes = color_modes or [ColorMode.ONOFF]

    def hub_attributes(self, attributes: dict) -> dict:
        """What of a hub event's attributes applies to this bulb.

        The listener calls this before it writes the model, for every source
        of hub state (events, scene actions, the reconnect snapshot)."""
        if "colorMode" not in attributes:
            # Not every bulb reports colorMode. The colour an event carries
            # then says which kind was written last.
            if "colorHue" in attributes or "colorSaturation" in attributes:
                attributes = {**attributes, "colorMode": "color"}
            elif "colorTemperature" in attributes:
                attributes = {**attributes, "colorMode": "temperature"}
        # A value that differs from what this bulb was just told is the hub's
        # old or intermediate state, or a change made right after the command.
        # It is kept, and applied by _hub_values_stand when the hub does not
        # confirm the command (see pending_command.py).
        return {
            key: value for key, value in attributes.items()
            if not self._pending.holds_back(to_snake_case(key), value)
        }

    @property
    def should_poll(self) -> bool:
        return False 
    
    @property
    def unique_id(self):
        return self._json_data.id

    @property
    def available(self):
        return self._json_data.is_reachable

    @property
    def device_info(self) -> DeviceInfo:

        return DeviceInfo(
            identifiers={("dirigera_platform", self._json_data.relation_id or self._json_data.id)},
            name=self.name,
            manufacturer=self._json_data.attributes.manufacturer,
            model=self._json_data.attributes.model,
            sw_version=self._json_data.attributes.firmware_version,
            suggested_area=self._json_data.room.name if self._json_data.room is not None else None,
        )

    @property
    def name(self):  
        if self._json_data.attributes.custom_name is None or len(self._json_data.attributes.custom_name) == 0:
            return self.unique_id
        return self._json_data.attributes.custom_name
    
    @property
    def brightness(self):
        return hub_level_to_brightness(self.light_level)

    @property
    def light_level(self):
        # This is the state of the HUB so in 1-100 range
        return self._json_data.attributes.light_level

    @light_level.setter
    def light_level(self, value):
        self._json_data.attributes.light_level = brightness_to_hub_level(value)

    @property
    def max_color_temp_kelvin(self):
        # Coldest, e.g. 4000K. Usually reported as colorTemperatureMin.
        return _kelvin_range(self._json_data.attributes)[1]

    @property
    def min_color_temp_kelvin(self):
        # Warmest, e.g. 2202K. Usually reported as colorTemperatureMax.
        return _kelvin_range(self._json_data.attributes)[0]

    @property
    def color_temp_kelvin(self):
        # Current color temperature in Kelvin
        return self._json_data.attributes.color_temperature

    @property
    def color_temperature(self):
        return self._json_data.attributes.color_temperature
    
    @color_temperature.setter
    def color_temperature(self, value):
        self._json_data.attributes.color_temperature = value 
    
    @property
    def hs_color(self):
        # A white or dimmable bulb has no hue or saturation (None): no colour
        # then, instead of a TypeError (a device set reads every member).
        if self.color_hue is None or self.color_saturation is None:
            return None
        return ( self.color_hue, self.color_saturation * 100)

    @property
    def color_hue(self):
        return self._json_data.attributes.color_hue 
    
    @color_hue.setter
    def color_hue(self, value) :
        self._json_data.attributes.color_hue = value
    
    @property
    def color_saturation(self):
        return self._json_data.attributes.color_saturation
    
    @color_saturation.setter
    def color_saturation(self, value):
        self._json_data.attributes.color_saturation = value 
    
    @property
    def is_on(self):
        return self._json_data.attributes.is_on

    @property
    def supported_color_modes(self):
        return self._supported_color_modes

    @property
    def color_mode(self):
        # Derived from the model: the hub's own colorMode ("temperature" |
        # "color", attributes.color_mode, dirigera >= 1.6.0, issue #50), which
        # a command and an event without colorMode write there as well (see
        # light_turn_on_patch and hub_attributes). A mode the bulb does not
        # support is never reported: scenes and events carry colour keys for
        # brightness-only devices too (e.g. the TRADFRI driver). Without a
        # usable colorMode the HS-first rule decides.
        supported = self._supported_color_modes
        hub_mode = getattr(self._json_data.attributes, "color_mode", None)
        if hub_mode == "temperature" and ColorMode.COLOR_TEMP in supported:
            return ColorMode.COLOR_TEMP
        if hub_mode == "color" and ColorMode.HS in supported:
            return ColorMode.HS
        for mode in (ColorMode.HS, ColorMode.COLOR_TEMP, ColorMode.BRIGHTNESS):
            if mode in supported:
                return mode
        return ColorMode.ONOFF

    async def async_update(self):
        try:
            logger.debug("async update called on bulb..")
            # The fetched model replaces the one hub events write to, and it is
            # as old as the hub's answer: what an event wrote to this bulb while
            # the fetch was on its way is newer and is carried over.
            with live_writes() as newer:
                fetched = await self.hass.async_add_executor_job(self._hub.get_light_by_id, self._json_data.id)
            replace_model(self, fetched, newer)
            self.set_state()
        except Exception as ex:
            logger.error("error encountered running update on : {}".format(self.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")
        
    def _call_later(self, delay, fn, *args):
        # The running loop, not self.hass.loop: a light the device set mode
        # keeps out of Home Assistant has no hass, but still gets commands
        # through its set (#73/#76).
        loop = self.hass.loop if self.hass is not None else asyncio.get_running_loop()
        return loop.call_later(delay, fn, *args)


    def _hub_values_stand(self, values):
        """`values` (model attribute -> value) were reported by the hub while
        a command for them was pending, and the command was not confirmed or
        failed: they are the state of the light."""
        if not values:
            return
        for key, value in values.items():
            setattr(self._json_data.attributes, key, value)
        # Write this light's state, and that of the device set that follows
        # it: this change does not pass the listener, whose cascade otherwise
        # updates the set (review of the merge with #76, 16-11-2026). The light
        # itself only while it is in Home Assistant and enabled; under "Sets
        # only" it is not, and the set is the only entity that shows it.
        hub = getattr(self, "_hub", None)
        entry = hub_event_listener.get_registry_entry(hub.websocket_base_url, self._json_data.id) if hub is not None else None
        for target in (self, getattr(entry, "cascade_entity", None)):
            if target is not None and getattr(target, "hass", None) is not None and getattr(target, "enabled", True):
                target.async_schedule_update_ha_state(False)

    async def async_will_remove_from_hass(self):
        self._pending.clear()

    async def _send(self, data, updates):
        """PATCH this light through the hub's writer; `updates` are the model
        values the PATCH stands for."""
        command = None

        def started():
            # Recorded when the PATCH goes out, not when it joined the queue
            # (what the hub says until then is the light's state before the
            # command), and not later: the hub's echo can be here before the
            # PATCH call returns.
            nonlocal command
            command = self._pending.begin(updates)
            # Newer than a resync fetch that is on its way now: its snapshot
            # must not reach the pending record (kept, then written back when
            # no echo comes), also when the hub accepts only after it (#207).
            note_command(self)

        try:
            await writer_for(self._hub).patch_device(self._json_data.id, data, started=started)
        except BaseException:  # a cancelled call too
            if command is not None:
                self._hub_values_stand(self._pending.failed(command))
            raise
        for key, value in self._pending.accepted(command).items():
            setattr(self._json_data.attributes, key, value)
        # And newer than a fetch that began while the PATCH was on its way.
        # Same rule as ikea_base_device._command (#207).
        note_command(self)
        # Write this light's state, and that of the device set that follows
        # it: this change does not pass the listener, whose cascade otherwise
        # updates the set (review of the merge with #76, 16-11-2026). The light
        # itself only while it is in Home Assistant and enabled; under "Sets
        # only" it is not, and the set is the only entity that shows it.
        hub = getattr(self, "_hub", None)
        entry = hub_event_listener.get_registry_entry(hub.websocket_base_url, self._json_data.id) if hub is not None else None
        for target in (self, getattr(entry, "cascade_entity", None)):
            if target is not None and getattr(target, "hass", None) is not None and getattr(target, "enabled", True):
                target.async_schedule_update_ha_state(False)

    async def async_turn_on(self, **kwargs):
        logger.debug("light turn_on...")
        logger.debug(kwargs)

        try:
            # One PATCH per light, through the hub's writer (#71). Three
            # separate PATCHes per light, sent in parallel for every light of
            # an area, often left bulbs on the wrong level or temperature.
            data, updates = light_turn_on_patch(
                self._json_data.capabilities.can_receive,
                kwargs,
                (self.min_color_temp_kelvin, self.max_color_temp_kelvin),
            )
            # A bulb that is off takes a lightLevel only once it is on. Sent
            # straight after isOn (same PATCH, or right after the hub reports
            # the bulb on, ~0.34 s), the level ends part-way: asked 30, got
            # 50, 3 of 3 runs and 6 of 6 after the on-event (live hub,
            # 16-11-2026, TRADFRI). Measured from the start of the on-PATCH:
            # 0.45 s 0 of 3 right, 0.60 s 2 of 3, 0.75 s and 1.0 s 3 of 3.
            # So from off the level goes in a second PATCH, ~1 s after the
            # first. The wait is outside the hub's writer: other bulbs are
            # not held up. Colour from off was right in one PATCH.
            # A level asked while the bulb is still coming on (a second
            # command within that second) waits as well, and a newer command
            # (on or off) drops a level that is still waiting, so an older
            # level never lands after a newer one (review 16-11-2026).
            self._command_seq += 1
            seq = self._command_seq
            loop = asyncio.get_running_loop()
            from_off = not self._json_data.attributes.is_on
            level_after = None
            if len(data) > 1 and "lightLevel" in data[-1]["attributes"] \
                    and (from_off or loop.time() < self._level_not_before):
                level_after = ([data.pop()], {"light_level": updates.pop("light_level")})
            logger.debug(f"PATCH {self._json_data.id}: {data}" + (f" then {level_after[0]}" if level_after else ""))
            await self._send(data, updates)
            if from_off:
                # Counted from the end of the on-PATCH, which may have waited
                # in the hub's writer behind other bulbs (~0.25 s on its own).
                self._level_not_before = loop.time() + LEVEL_AFTER_ON_S
            if level_after is not None:
                delay = self._level_not_before - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if seq != self._command_seq:
                    logger.debug(f"PATCH {self._json_data.id}: level {level_after[0]} dropped, a newer command came")
                    return
                await self._send(*level_after)
        except Exception as ex:
            logger.error("error encountered turning on : {}".format(self.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")

    async def async_turn_off(self, **kwargs):
        logger.debug("light turn_off...")
        self._command_seq += 1  # a level still waiting from a turn_on is dropped
        try:
            await self._send([{"attributes": {"isOn": False}}], {"is_on": False})
        except Exception as ex:
            logger.error("error encountered turning off : {}".format(self.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")

# IKEA Device Set
# The hub accepts a PATCH for the whole set, but there is no state to GET for
# it, and the hub pushes no events for it either: it reports each light in the
# set. As in the IKEA app, the set shows the state of its first light (the
# controller). The controller's events reach the set through the cascade in
# hub_event_listener, whether or not the light is imported itself.

class ikea_bulb_device_set(LightEntity):
    # The entity is named after its device, the set. With a name of its own,
    # Home Assistant joins the device name and the entity name, and a rename
    # could leave the two out of step ("Kitchen 2 2").
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, hub, device_set: DeviceSet, controller: ikea_bulb, members=None) -> None:
        logger.debug(f"Setting up device_set {device_set.id} with first bulb {controller.unique_id}")
        self._hub = hub
        self._device_set = device_set
        # Callable giving the set's current ikea_bulb entities (the manager
        # knows them); without one, the controller alone.
        self._members = members or (lambda: [self._controller] if self._controller is not None else [])
        self._controller = None
        hub_event_listener.register(self._hub.websocket_base_url, self.unique_id, registry_entry(self))
        self.set_controller(controller)

    def set_controller(self, bulb) -> None:
        """Follow another first light (or none when the set is empty), and
        the set's current members.

        Every member gets the set as its cascade: the hub event listener
        writes this set's state whenever ANY member's state changes, since the
        set's state is derived from all of them (step 4 of the 0.4.1 plan).
        Also when the controller stays the same: a light joined or left.
        """
        self._controller = bulb
        self._follow_members()
        if self.hass is not None and getattr(self, "enabled", True):
            self.async_write_ha_state()

    def _follow_members(self) -> None:
        registry = hub_event_listener.device_registry.get(self._hub.websocket_base_url, {})
        current = set() if self._controller is None else {
            getattr(bulb, "unique_id", None) for bulb in self._members()}
        for light_id, entry in registry.items():
            if light_id != self.unique_id and getattr(entry, "cascade_entity", None) is self \
                    and light_id not in current:
                entry.cascade_entity = None
        for light_id in current:
            entry = registry.get(light_id)
            if entry is not None:
                entry.cascade_entity = self

    @property
    def should_poll(self):
        return False

    @property
    def unique_id(self):
        return self._device_set.id

    # --- state derived from the members (step 4, 16-11-2026) ----------------
    # As Home Assistant's own light group: on when any member is on,
    # brightness and colour temperature the mean of the members that are on.
    # Hue/saturation and the colour mode come from the first member that is
    # on, the controller first: a mean of hues means nothing. Until 0.4.1 the
    # set showed only its first light, so a set with one light on and the
    # first off showed off; and which light is first can change on a restart.

    def _ordered(self) -> list:
        members = list(self._members())
        if self._controller in members:
            members.remove(self._controller)
            members.insert(0, self._controller)
        return members

    def _lit(self) -> list:
        # As Home Assistant's light group, an unavailable light does not
        # count: an unreachable bulb keeps its last isOn (review 16-11-2026).
        return [m for m in self._ordered()
                if getattr(m, "available", False) and getattr(m, "is_on", False)]

    def _lit_in(self, mode) -> list:
        """The lights that are on and in this colour mode: a bulb in colour
        mode still holds an old colour temperature, and the other way round."""
        return [m for m in self._lit() if getattr(m, "color_mode", None) == mode]

    @staticmethod
    def _mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values)) if values else None

    @property
    def available(self):
        return any(getattr(m, "available", False) for m in self._members())

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={("dirigera_platform", self._device_set.id)},
            name=self._device_set.name,
            manufacturer="IKEA",
            model=DEVICE_SET_MODEL,
            sw_version="1.0",
            suggested_area=self._device_set.room.name if self._device_set.room is not None else None,
        )

    def _from_controller(self, attr, default=None):
        """The controller's value, or default while the set has no light."""
        return default if self._controller is None else getattr(self._controller, attr)

    @property
    def is_on(self):
        if not self._members():
            return None
        return bool(self._lit())

    @property
    def brightness(self):
        lit = self._lit()
        if not lit:
            return self._from_controller("brightness")
        return self._mean(getattr(m, "brightness", None) for m in lit)

    @property
    def max_color_temp_kelvin(self):
        return self._from_controller("max_color_temp_kelvin")

    @property
    def min_color_temp_kelvin(self):
        return self._from_controller("min_color_temp_kelvin")

    @property
    def color_temp_kelvin(self):
        mean = self._mean(getattr(m, "color_temp_kelvin", None) for m in self._lit_in(ColorMode.COLOR_TEMP))
        if mean is None:
            return self._from_controller("color_temp_kelvin")
        # Members of another type may reach beyond the controller's range,
        # which is the range the set reports.
        low, high = self.min_color_temp_kelvin, self.max_color_temp_kelvin
        if low is not None and high is not None:
            mean = max(low, min(high, mean))
        return mean

    @property
    def hs_color(self):
        for member in self._lit_in(ColorMode.HS):
            value = getattr(member, "hs_color", None)
            if value is not None:
                return value
        return self._from_controller("hs_color")

    @property
    def supported_color_modes(self):
        return self._from_controller("supported_color_modes", {ColorMode.ONOFF})

    @property
    def color_mode(self):
        # The first light that is on in a mode the controller supports: a
        # member of another type may be in a mode the controller lacks, and
        # Home Assistant rejects a mode outside supported_color_modes. Else
        # the controller's own mode, whose values the controller then gives.
        supported = self.supported_color_modes or {ColorMode.ONOFF}
        for member in self._lit():
            mode = getattr(member, "color_mode", None)
            if mode in supported:
                return mode
        return self._from_controller("color_mode", ColorMode.ONOFF)

    async def _each_member(self, method: str, **kwargs):
        # The hub's /devices/set/{id} PATCH is not reliable: on guaifi's hub
        # (#71) bulbs ended on the wrong level or temperature, or stayed off,
        # whatever the body looked like, and one member ignored every set
        # command while it worked on its own. So a set sends each member its
        # own command, through the same per-light PATCH and per-hub writer as
        # a single bulb. Every member is tried; a failure is raised after.
        # The members are the set's lights NOW: membership changes while Home
        # Assistant runs (#73/#76), and a member may be a light the device set
        # mode keeps out of Home Assistant (its writes still go to the hub).
        lights = self._members()
        if not lights:
            raise HomeAssistantError(f"device set {self._device_set.name} has no lights", DOMAIN, "hub_exception")
        results = await asyncio.gather(
            *(getattr(light, method)(**kwargs) for light in lights),
            return_exceptions=True,
        )
        # The set shows its controller's state, and the controller's model now
        # holds what the hub accepted: show it at once instead of waiting for
        # an echo that a command record may hold back (regression R1 of #71).
        if self.hass is not None:
            self.async_write_ha_state()
        failed = [light._json_data.id for light, result in zip(lights, results) if isinstance(result, BaseException)]
        if failed:
            logger.error(f"device_set {self._device_set.name}: {method} failed for {', '.join(failed)}")
            raise HomeAssistantError(f"{method} failed for {', '.join(failed)}", DOMAIN, "hub_exception")

    async def async_turn_on(self, **kwargs):
        logger.debug(f"light device_set turn_on... {kwargs}")
        await self._each_member("async_turn_on", **kwargs)

    async def async_turn_off(self, **kwargs):
        logger.debug("light device_set turn_off...")
        await self._each_member("async_turn_off", **kwargs)
