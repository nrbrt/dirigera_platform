"""Platform for IKEA dirigera hub integration."""
from __future__ import annotations

import logging

from dirigera import Hub 
from .dirigera_lib_patch import HubX

from .ikea_gateway import ikea_gateway

import voluptuous as vol

from homeassistant import config_entries, core
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.light import PLATFORM_SCHEMA
from homeassistant.const import CONF_IP_ADDRESS, CONF_TOKEN, Platform

# Import the device class from the component that you want to support
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    CONF_HIDE_DEVICE_SET_BULBS,
    CONF_ENABLED_PLATFORMS,
    DEFAULT_ENABLED_PLATFORMS,
    PLATFORM,
    DISCOVERY_COORDINATOR,
)
from .hub_event_listener import hub_event_listener
from .device_discovery import DeviceDiscoveryCoordinator, set_discovery_coordinator

PLATFORMS_TO_SETUP = [  Platform.SWITCH, 
                        Platform.BINARY_SENSOR, 
                        Platform.LIGHT, 
                        Platform.SENSOR, 
                        Platform.COVER, 
                        Platform.FAN,
                        Platform.SCENE]

# Issue #47: which of the platforms above this entry actually sets up. Missing
# key (every entry created before the option existed) means all of them.
def resolve_platforms(entry_data: dict) -> list[Platform]:
    """Platforms to set up for this entry, in the fixed order of PLATFORMS_TO_SETUP."""
    selected = entry_data.get(CONF_ENABLED_PLATFORMS)
    if not selected:
        # Also covers an empty list: an entry that sets up nothing at all is a
        # broken entry, not a valid choice, so fall back to everything.
        return list(PLATFORMS_TO_SETUP)
    return [p for p in PLATFORMS_TO_SETUP if p.value in selected]

logger = logging.getLogger("custom_components.dirigera_platform")


def sync_platform_entity_registry(hass, entry, platforms) -> None:
    """Hide the entities of platforms this entry no longer sets up (#47).

    Not forwarding a platform does not remove its entities. They stay in the
    entity registry and turn up as unavailable with restored=True, so they still
    appear in every entity picker and search. Measured on a live hub: switching
    off one platform left its entity sitting there exactly like that. Since the
    complaint in #47 is that duplicates make entity management harder, a filter
    that leaves ghosts behind does not actually solve anything.

    Disabling rather than removing is deliberate. Removing a registry entry takes
    the user's rename, area assignment and entity_id override with it, and
    re-selecting the platform cannot bring those back. Disabling hides the entity
    from the UI, survives a round trip intact, and is reversible.

    This only ever touches what it disabled itself: an entity the user switched
    off by hand keeps disabled_by USER and stays off, even when its platform is
    selected again. Entries are only written when the desired state differs, so
    a setup that changes nothing fires no registry events at all.
    """
    registry = er.async_get(hass)
    wanted = {platform.value for platform in platforms}
    disabled = 0
    re_enabled = 0

    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        selected = reg_entry.domain in wanted
        if not selected and reg_entry.disabled_by is None:
            registry.async_update_entity(
                reg_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            disabled += 1
        elif selected and reg_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)
            re_enabled += 1

    dev_disabled, dev_re_enabled = _sync_device_registry(hass, entry, registry, wanted)

    if disabled or re_enabled or dev_disabled or dev_re_enabled:
        logger.info(
            "Platform filter: disabled %d entities, re-enabled %d; "
            "disabled %d devices, re-enabled %d",
            disabled, re_enabled, dev_disabled, dev_re_enabled,
        )


def _sync_device_registry(hass, entry, entity_reg, wanted) -> tuple[int, int]:
    """Hide devices whose every entity belongs to a platform we no longer import.

    Disabling the entities alone was not enough, and that is what #47 actually
    asked for. Home Assistant keeps a device in the device registry as long as it
    has entities, disabled or not, so a filtered-out plug still shows up in the
    device list. The reporter runs the same hardware through Matter as well, so
    what he sees is a duplicate DEVICE, and disabling its entities leaves that
    duplicate exactly where it was. He had to delete the devices by hand.

    A device is only hidden when EVERY one of its entities sits on a platform
    that is not selected. That matters because one Dirigera device can span
    several platforms: a plug produces both a switch and its energy sensors, so
    deselecting sensors must not take the whole plug away.

    Disabled and not deleted, for the same reason as the entities: deleting a
    device entry discards its name, its area and its via_device links, and
    re-selecting the platform cannot restore them. And as with the entities, a
    device the user disabled by hand keeps disabled_by USER and is left alone.
    """
    device_reg = dr.async_get(hass)
    disabled = 0
    re_enabled = 0

    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        entities = er.async_entries_for_device(
            entity_reg, device.id, include_disabled_entities=True
        )
        if not entities:
            # No entities at all: nothing to base a decision on, so leave it be.
            continue
        keep = any(e.domain in wanted for e in entities)
        if not keep and device.disabled_by is None:
            device_reg.async_update_device(
                device.id, disabled_by=dr.DeviceEntryDisabler.INTEGRATION
            )
            disabled += 1
        elif keep and device.disabled_by == dr.DeviceEntryDisabler.INTEGRATION:
            device_reg.async_update_device(device.id, disabled_by=None)
            re_enabled += 1

    return disabled, re_enabled

# Validation of the user's configuration
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_IP_ADDRESS): cv.string,
        vol.Required(CONF_TOKEN): cv.string,
        vol.Optional(CONF_HIDE_DEVICE_SET_BULBS, default=True): cv.boolean
    }
)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    logger.debug("Starting async_setup...")
    #for k in config.keys():
    #    logger.debug(f"config key: {k} value: {config[k]}")
    logger.debug("Complete async_setup...")

    def handle_dump_data(call):
        import dirigera

        logger.info("=== START Devices JSON ===")
        # hass.data[DOMAIN] also holds the PLATFORM gateway object and the
        # discovery coordinator; after a reload the entry dict is re-inserted
        # last, so blindly taking keys()[0] picked the wrong object and the
        # service crashed. Select the config-entry dict explicitly.
        config_data = next(
            (v for v in hass.data[DOMAIN].values()
             if isinstance(v, dict) and CONF_IP_ADDRESS in v),
            None,
        )
        if config_data is None:
            logger.warning("dump_data: no configured hub entry found")
            return
        ip = config_data[CONF_IP_ADDRESS]
        token = config_data[CONF_TOKEN]
        
        logger.info("--------------")
        if ip == "mock":
            logger.info("{ MOCK JSON }")
        else:
            hub = dirigera.Hub(token, ip)
            json_resp = hub.get("/devices")
            logger.debug(f"TYPE IS {type(json_resp)}")
            #import json 
            #devices_json = json.loads(json_resp)
            # Sanitize the dump
                    
            master_id_map = {}
            id_counter = 1
            for device_json in json_resp:
                if "id" in device_json:
                    id_value = device_json["id"]
                    id_to_replace = id_counter 
                    
                    if id_value in master_id_map:
                        id_to_replace = master_id_map[id_value]
                    else:
                        id_counter = id_counter + 1
                        master_id_map[id_value] = id_to_replace
                    
                    device_json["id"] = id_to_replace
                    
                if "relationId" in device_json:
                    id_value = device_json["relationId"]
                    id_to_replace = id_counter

                    if id_value in master_id_map:
                        id_to_replace = master_id_map[id_value]
                    else:
                        id_counter = id_counter + 1
                        master_id_map[id_value] = id_to_replace

                    # used to overwrite "id" again, leaving relationId unsanitized
                    device_json["relationId"] = id_to_replace
                
                if "attributes" in device_json and "serialNumber" in device_json["attributes"]:
                    id_value = device_json["attributes"]["serialNumber"]
                    id_to_replace = id_counter 
                    
                    if id_value in master_id_map:
                        id_to_replace = master_id_map[id_value]
                    else:
                        id_counter = id_counter + 1
                        master_id_map[id_value] = id_to_replace
                    
                    device_json["attributes"]["serialNumber"] = id_to_replace
                
                if "room" in device_json and "id" in device_json["room"]:
                    id_value = device_json["room"]["id"]
                    id_to_replace = id_counter 
                    
                    if id_value in master_id_map:
                        id_to_replace = master_id_map[id_value]
                    else:
                        id_counter = id_counter + 1
                        master_id_map[id_value] = id_to_replace
                    
                    device_json["room"]["id"] = id_to_replace
                
                if "deviceSet" in device_json:
                    for device_set in device_json["deviceSet"]:
                        if "id" in device_set:
                            id_value = device_set["id"]
                            id_to_replace = id_counter 
                            
                            if id_value in master_id_map:
                                id_to_replace = master_id_map[id_value]
                            else:
                                id_counter = id_counter + 1
                                master_id_map[id_value] = id_to_replace
                            
                            device_set["id"]= id_to_replace
                
                # remoteLinks is a list of device-id strings. The old code
                # tested for the literal "remote_link" (never true), would
                # KeyError when the key was absent, and read a stale loop
                # variable from the deviceSet block above.
                if "remoteLinks" in device_json and device_json["remoteLinks"]:
                    sanitized_links = []
                    for remote_link in device_json["remoteLinks"]:
                        if remote_link in master_id_map:
                            id_to_replace = master_id_map[remote_link]
                        else:
                            id_to_replace = id_counter
                            id_counter = id_counter + 1
                            master_id_map[remote_link] = id_to_replace
                        sanitized_links.append(id_to_replace)
                    device_json["remoteLinks"] = sanitized_links
                
            logger.info(json_resp)
        logger.info("--------------")


    hass.services.async_register(DOMAIN, "dump_data", handle_dump_data)
    return True


async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up platform from a ConfigEntry."""
    logger.info("Staring async_setup_entry in init...")
    
    hass.data.setdefault(DOMAIN, {})
    hass_data = dict(entry.data)

    # for backward compatibility
    hide_device_set_bulbs : bool = True 
    if CONF_HIDE_DEVICE_SET_BULBS in hass_data:
         logger.debug("Found HIDE_DEVICE_SET *****  ")
         #logger.debug(hass_data)
         hide_device_set_bulbs = hass_data[CONF_HIDE_DEVICE_SET_BULBS]
    else:
        logger.debug("Not found HIDE_DEVICE_SET *****  ")
        # If its not with HASS update it
        hass_data[CONF_HIDE_DEVICE_SET_BULBS] = hide_device_set_bulbs

    ip = hass_data[CONF_IP_ADDRESS]
    # issue #39: back-fill unique_id on entries created before the duplicate
    # guard, so a future duplicate add is rejected too. Already-duplicated
    # entries remain (the user removes the extra one).
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=ip)
    # Register the options-update listener exactly once; async_on_unload also
    # cleans it up when setup fails. It used to be registered twice (once
    # manually, once here), so every options save triggered two reloads.
    entry.async_on_unload(entry.add_update_listener(options_update_listener))
    hass.data[DOMAIN][entry.entry_id] = hass_data

    hass_data = dict(entry.data)
    hub = HubX(hass_data[CONF_TOKEN], hass_data[CONF_IP_ADDRESS])
    
    # Lets get all kinds that we are interested in one go and create the devices
    # such that the platform can go ahead and add the associated sensors
    platform = ikea_gateway()
    # issue #39: store the gateway per config entry, not in a shared global slot.
    # A second hub entry used to clobber the first here, so one hub's devices
    # vanished and the two entries collided on duplicate IDs (multi-hub support).
    hass.data[DOMAIN][entry.entry_id]["gateway"] = platform
    logger.debug("Starting make_devices...")
    try:
        await platform.make_devices(hass,hass_data[CONF_IP_ADDRESS], hass_data[CONF_TOKEN])
    except (ConnectionError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to IKEA Dirigera hub at {hass_data[CONF_IP_ADDRESS]}: {err}"
        ) from err
    
    #await hass.async_add_executor_job(platform.make_devices,hass, hass_data[CONF_IP_ADDRESS], hass_data[CONF_TOKEN])

    # Initialize the discovery coordinator BEFORE platform setup
    # so platforms can register their callbacks during async_setup_entry
    discovery = DeviceDiscoveryCoordinator(hass, hub)
    # issue #39: per-entry discovery coordinator (was a shared global slot +
    # module global that a second hub clobbered).
    hass.data[DOMAIN][entry.entry_id]["discovery"] = discovery
    logger.debug("Device discovery coordinator initialized")

    # Setup the entities - each platform will register its callback with discovery coordinator
    # Store the resolved list on the entry BEFORE forwarding. async_unload_entry
    # must unload exactly what was loaded; if it re-resolved from the (possibly
    # just-changed) options instead, a platform the user switched off would
    # never be unloaded and its entities would linger as orphans. See issue #47.
    platforms = resolve_platforms(hass_data)
    hass.data[DOMAIN][entry.entry_id]["platforms"] = platforms
    logger.debug("Setting up platforms: %s", [p.value for p in platforms])
    # Before forwarding, not after: a platform that just got selected again must
    # have its entities re-enabled first, otherwise the platform sets up while
    # the registry still lists them as disabled and they stay missing until the
    # next reload.
    sync_platform_entity_registry(hass, entry, platforms)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    # Now lets start the event listener
    hub_basic = Hub(hass_data[CONF_TOKEN], hass_data[CONF_IP_ADDRESS])

    if hass_data[CONF_IP_ADDRESS] != "mock":
        hub_events = hub_event_listener(hub_basic, hass, discovery)
        hub_events.start()
        try:
            # Sync device names and areas from Dirigera to HA device registry
            # This ensures names and areas are set correctly after HA restart
            await hub_events.sync_all_device_names()
            await hub_events.sync_all_device_areas()
        except Exception:
            # Setup is about to fail — without this, the listener thread kept
            # running and every ConfigEntryNotReady retry started another one.
            await hass.async_add_executor_job(hub_events.stop)
            raise
        # Per-entry storage instead of a module global: a second hub entry no
        # longer clobbers the first listener, and unload stops the right one.
        hass.data[DOMAIN][entry.entry_id]["hub_events"] = hub_events

    logger.debug("Complete async_setup_entry...")

    return True

async def options_update_listener(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    logger.debug("**********In options_update_listener")
    logger.debug(config_entry)
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)

async def async_unload_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Unload a config entry."""
    # Called during re-load and delete
    logger.debug("Starting async_unload_entry")

    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})

    # Stop the listener. stop() joins the websocket thread (worst case the
    # full reconnect backoff), so run it in the executor — calling it
    # directly used to freeze the event loop during unload/reload.
    hub_events = entry_data.get("hub_events")
    if hub_events is not None:
        await hass.async_add_executor_job(hub_events.stop)

    hass_data = dict(entry.data)
    hub = HubX(hass_data[CONF_TOKEN], hass_data[CONF_IP_ADDRESS])

    # For each controller if there is an empty scene delete it
    logger.debug("In unload so forcing delete of scenes...")
    await hass.async_add_executor_job(hub.delete_empty_scenes)
    logger.debug("Done deleting empty scenes....")

    # all() over the gather result list itself — the old all([gather])
    # wrapped it in another list and was therefore always True.
    # Unload what was actually loaded, not what the current options say (issue
    # #47). The fallback covers an entry that failed before the platforms were
    # stored; unloading a platform that was never set up is a no-op.
    loaded_platforms = entry_data.get("platforms") or resolve_platforms(dict(entry.data))
    unload_ok = await hass.config_entries.async_unload_platforms(entry, loaded_platforms)

    # The options-update listener is removed via entry.async_on_unload.
    hass.data[DOMAIN].pop(entry.entry_id, None)
    logger.debug("Successfully popped entry")
    logger.debug("Complete async_unload_entry")

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    device_entry: config_entries.DeviceEntry,
) -> bool:

    logger.info("Got request to remove device")
    logger.info(config_entry)
    logger.info(device_entry)
    return True