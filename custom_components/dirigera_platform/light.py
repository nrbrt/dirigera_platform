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

from .const import DEVICE_SET_MODEL, DOMAIN
from .device_sets import (
    DeviceSet,
    DeviceSetIndex,
    light_wanted,
    resolve_device_set_mode,
    sets_wanted,
)
from .hub_event_listener import get_device_by_identifier, hub_event_listener, registry_entry

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

    def _make_set_entity(self, set_id):
        controller = self._bulbs[self._index.controller_id(set_id)]
        entity = ikea_bulb_device_set(self._hub, self._index.get(set_id), controller)
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


class ikea_bulb(LightEntity):

    def __init__(self, hub, json_data : Light) -> None:
        logger.debug("ikea_bulb ctor...")
        self._hub = hub
        self._json_data = json_data
        self._ignore_update = False 

        # Register the device for updates
        hub_event_listener.register(self._hub.websocket_base_url, self._json_data.id, registry_entry(self))

        self.set_state()

    # When changing brightness a random update is sent by hub with brightness level before
    # the actual value is sent. So this is a hack to ignore that random update
    @property
    def should_ignore_update(self):
        return self._ignore_update
    
    def reset_ignore_update(self):
        self._ignore_update = False 

    def set_state(self):
        # Set Color capabilities
        logger.debug("Set State of bulb..")
        color_modes = []
        can_receive = self._json_data.capabilities.can_receive
        #logger.debug("Got can_receive in state")
        #logger.debug(can_receive)
        self._color_mode = ColorMode.ONOFF
        for cap in can_receive:
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

        if len(color_modes) == 0:
            logger.debug("Color modes array is zero, setting to UNKNOWN")
            self._supported_color_modes = [ColorMode.ONOFF]
        else:
            self._supported_color_modes = color_modes
            # Prefer the hub's own colorMode ("temperature" | "color") when the
            # bulb reports it (attributes.color_mode, dirigera >= 1.6.0, issue
            # #50). Without it, a dual-mode bulb in colour-temperature mode
            # reports hs after a restart, and HA reports color_temp_kelvin as
            # None until the next colour event. The HS-first rule below stays
            # as the fallback for bulbs that do not report colorMode.
            hub_mode = getattr(self._json_data.attributes, "color_mode", None)
            if hub_mode == "temperature" and ColorMode.COLOR_TEMP in self._supported_color_modes:
                self._color_mode = ColorMode.COLOR_TEMP
            elif hub_mode == "color" and ColorMode.HS in self._supported_color_modes:
                self._color_mode = ColorMode.HS
            elif ColorMode.HS in self._supported_color_modes:
                self._color_mode = ColorMode.HS
            elif ColorMode.COLOR_TEMP in self._supported_color_modes:
                self._color_mode = ColorMode.COLOR_TEMP
            elif ColorMode.BRIGHTNESS in self._supported_color_modes:
                self._color_mode = ColorMode.BRIGHTNESS

        #logger.debug("supported color mode set to:")
        #logger.debug(self._supported_color_modes)
        #logger.debug("color mode set to:")
        #logger.debug(self._color_mode)

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
        return self._color_mode
    
    @color_mode.setter
    def color_mode(self, value):
        self._color_mode = value 

    async def async_update(self):
        try:
            logger.debug("async update called on bulb..")
            self._json_data = await self.hass.async_add_executor_job(self._hub.get_light_by_id, self._json_data.id)
            self.set_state()
        except Exception as ex:
            logger.error("error encountered running update on : {}".format(self.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")
        
    async def async_turn_on(self, **kwargs):
        logger.debug("light turn_on...")
        logger.debug(kwargs)

        try:
            # Probably change
            self.reset_ignore_update()
            await self.hass.async_add_executor_job(self._json_data.set_light,True)

            if ATTR_BRIGHTNESS in kwargs:
                # brightness requested
                # The setter will move the HASS value of 0-100 to 1-255
                self.light_level = int(kwargs[ATTR_BRIGHTNESS])
                logger.debug("scaled brightness : {}".format(self.light_level))
                # update
                await self.hass.async_add_executor_job(self._json_data.set_light_level,self.light_level)
                self._ignore_update = True 

            if ATTR_COLOR_TEMP_KELVIN in kwargs and ColorMode.COLOR_TEMP in self._supported_color_modes:
                # color temp requested
                logger.debug("Request to set color temp...")
                ct = kwargs[ATTR_COLOR_TEMP_KELVIN]
                logger.debug("Set CT (Kelvin): {}".format(ct))
                # set_color_temperature() rejects values outside the bulb's
                # range, so keep the request inside it. It also updates the
                # local model, which the state write below reads.
                low, high = self.min_color_temp_kelvin, self.max_color_temp_kelvin
                if low is not None and high is not None:
                    ct = max(low, min(high, ct))
                await self.hass.async_add_executor_job(
                    self._json_data.set_color_temperature, ct
                )
                self._color_mode = ColorMode.COLOR_TEMP
                self._ignore_update = True

            if ATTR_HS_COLOR in kwargs and ColorMode.HS in self._supported_color_modes:
                logger.debug("Request to set color HS")
                hs_tuple = kwargs[ATTR_HS_COLOR]
                hue = hs_tuple[0]
                saturation = hs_tuple[1] / 100  # Saturation is 0 - 1 at IKEA

                # set_light_color updates the local model attributes itself
                await self.hass.async_add_executor_job(self._json_data.set_light_color, hue, saturation)
                self._color_mode = ColorMode.HS
                self._ignore_update = True
            self.async_schedule_update_ha_state(False)
        except Exception as ex:
            logger.error("error encountered turning on : {}".format(self.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")

    async def async_turn_off(self, **kwargs):
        logger.debug("light turn_off...")
        try:
            self.reset_ignore_update()
            await self.hass.async_add_executor_job(self._json_data.set_light,False)
            self.async_schedule_update_ha_state(False)
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

    def __init__(self, hub, device_set: DeviceSet, controller: ikea_bulb) -> None:
        logger.debug(f"Setting up device_set {device_set.id} with first bulb {controller.unique_id}")
        self._hub = hub
        self._device_set = device_set
        self._controller = None
        self._patch_url = f"/devices/set/{device_set.id}?deviceType=light"
        hub_event_listener.register(self._hub.websocket_base_url, self.unique_id, registry_entry(self))
        self.set_controller(controller)

    def set_controller(self, bulb) -> None:
        """Follow another light of the set, or none when the set is empty.

        Moves the cascade: the hub event listener pushes this set's state
        whenever the controller's state changes.
        """
        if bulb is self._controller:
            return
        hub_key = self._hub.websocket_base_url
        if self._controller is not None:
            old = hub_event_listener.get_registry_entry(hub_key, self._controller.unique_id)
            if old is not None and old.cascade_entity is self:
                old.cascade_entity = None
        self._controller = bulb
        if bulb is not None:
            new = hub_event_listener.get_registry_entry(hub_key, bulb.unique_id)
            if new is not None:
                new.cascade_entity = self
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def should_poll(self):
        return False

    @property
    def unique_id(self):
        return self._device_set.id

    @property
    def available(self):
        return self._controller is not None and self._controller.available

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
    def brightness(self):
        return self._from_controller("brightness")

    @property
    def max_color_temp_kelvin(self):
        return self._from_controller("max_color_temp_kelvin")

    @property
    def min_color_temp_kelvin(self):
        return self._from_controller("min_color_temp_kelvin")

    @property
    def color_temp_kelvin(self):
        return self._from_controller("color_temp_kelvin")

    @property
    def hs_color(self):
        return self._from_controller("hs_color")

    @property
    def is_on(self):
        return self._from_controller("is_on")

    @property
    def supported_color_modes(self):
        return self._from_controller("supported_color_modes", {ColorMode.ONOFF})

    @property
    def color_mode(self):
        return self._from_controller("color_mode", ColorMode.ONOFF)

    def patch_command(self, key_val : dict):
        data = [{"attributes" : key_val}]
        try:
            self._hub.patch(self._patch_url, data=data)
        except Exception as ex:
            logger.error("error encountered running update on : {}".format(self._device_set.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")

    # As indicated the turn on / brightness / color etc can be set by a patch command
    # the corresponding get doesnt work 
    async def async_turn_on(self, **kwargs):
        logger.debug("light device_set turn_on...")
        logger.debug(kwargs)

        try:
            # The set's state is its first bulb's, so the hub's echo for that
            # bulb is the only way it reaches HA. Unlike ikea_bulb, nothing is
            # written through here, so the echo must not be ignored: doing so
            # left the set and its first bulb on their old brightness, white
            # and colour while the hub had applied the new ones.
            self._controller.reset_ignore_update()
            await self.hass.async_add_executor_job(self.patch_command,{"isOn": True})

            if ATTR_BRIGHTNESS in kwargs:
                # brightness requested
                # HASS sends brightness in the range 0-255; the hub expects 1-100

                logger.debug("Request to device_set set brightness...")
                level = int(kwargs[ATTR_BRIGHTNESS])

                scaled = brightness_to_hub_level(level)
                logger.debug("Set brightness : {}".format(level))
                logger.debug("Set scaled brightness : {}".format(scaled))
                await self.hass.async_add_executor_job(self.patch_command, {"lightLevel" : scaled})

            if ATTR_COLOR_TEMP_KELVIN in kwargs and ColorMode.COLOR_TEMP in self._controller.supported_color_modes:
                # color temp requested
                # If request is white then brightness is passed
                logger.debug("Request to device_set set color temp...")
                ct = kwargs[ATTR_COLOR_TEMP_KELVIN]
                logger.debug("Set CT : {}".format(ct))
                await self.hass.async_add_executor_job(self.patch_command, {"colorTemperature" : ct})

            if ATTR_HS_COLOR in kwargs and ColorMode.HS in self._controller.supported_color_modes:
                logger.debug("Request to set color HS device_set")
                hs_tuple = kwargs[ATTR_HS_COLOR]
                hue = hs_tuple[0]
                saturation = hs_tuple[1] / 100  # Saturation is 0 - 1 at IKEA

                await self.hass.async_add_executor_job(self.patch_command,{ "colorHue" : hue, "colorSaturation" : saturation})

        except Exception as ex:
            logger.error("error encountered turning on device_set : {}".format(self._device_set.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")

    async def async_turn_off(self, **kwargs):
        self._controller.reset_ignore_update()
        logger.debug("light device_set turn_off...")
        try:
            await self.hass.async_add_executor_job(self.patch_command, {"isOn": False})
        except Exception as ex:
            logger.error("error encountered turning off device_set : {}".format(self._device_set.name))
            logger.error(ex)
            raise HomeAssistantError(ex, DOMAIN, "hub_exception")