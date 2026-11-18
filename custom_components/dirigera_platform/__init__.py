"""Platform for IKEA dirigera hub integration."""
from __future__ import annotations

import json
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
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_ENABLED_PLATFORMS,
    DEFAULT_ENABLED_PLATFORMS,
    CONF_EXCLUDE_MATTER,
    DEVICE_SET_MODEL,
    DEFAULT_EXCLUDE_MATTER,
    PLATFORM,
    DISCOVERY_COORDINATOR,
    ROOMS_STORE_VERSION,
    rooms_store_key,
)
from .hub_event_listener import hub_event_listener
from .hub_writer import start_hub_writer
from .stale_devices import (
    async_remove_stale_devices,
    excluded_matter_identifiers,
    find_excluded_matter_devices,
    is_stale,
    known_identifiers,
)
from .dirigera_lib_patch import is_matter_device
from .anonymise import anonymise_devices
from .device_discovery import DeviceDiscoveryCoordinator, set_discovery_coordinator
from .device_sets import DeviceSetIndex, resolve_device_set_mode, sets_wanted

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


def sync_platform_entity_registry(hass, entry, platforms, hidden_unique_ids=frozenset()) -> None:
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

    hidden_unique_ids are entities left out within a selected platform: the
    lights the device set mode does not import. They are handled here,
    in the same pass, rather than by the light platform: both filters use
    disabled_by INTEGRATION, and two passes would undo each other on every
    start (this one re-enabling what the other disabled, which also makes
    Home Assistant reload the entry).
    """
    registry = er.async_get(hass)
    domains = {platform.value for platform in platforms}

    def wanted(reg_entry) -> bool:
        if reg_entry.domain not in domains:
            return False
        return reg_entry.domain != "light" or reg_entry.unique_id not in hidden_unique_ids

    disabled = 0
    re_enabled = 0

    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        selected = wanted(reg_entry)
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
            "Platform and device set filter: disabled %d entities, re-enabled %d; "
            "disabled %d devices, re-enabled %d",
            disabled, re_enabled, dev_disabled, dev_re_enabled,
        )


def _device_set_entity_ids(hass, entry) -> set:
    """Unique ids of this entry's device set entities.

    Found by their device's model rather than the device set index, so that
    "Lights only" also hides a set that disappeared while Home Assistant was
    stopped: no light is in it any more, so the index does not know it.
    """
    entity_reg = er.async_get(hass)
    return {
        reg_entry.unique_id
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if device.model == DEVICE_SET_MODEL
        for reg_entry in er.async_entries_for_device(entity_reg, device.id, include_disabled_entities=True)
        if reg_entry.domain == "light"
    }


def _sync_device_registry(hass, entry, entity_reg, wanted) -> tuple[int, int]:
    """Hide devices whose every entity belongs to a platform we no longer import.

    wanted is the predicate of sync_platform_entity_registry: an entity is
    kept when its platform is selected and the device set mode imports it.
    A light inside a set is a device with one light entity, so under "Sets
    only" the device goes too, as in the IKEA app.

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
        keep = any(wanted(e) for e in entities)
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
        hub = dirigera.Hub(token, ip)
        json_resp = hub.get("/devices")
        # Same anonymiser as the diagnostics download: one place decides what
        # leaves the house. The old inline sanitiser here left device and room
        # names, the hub's coordinates and its serial number in the dump.
        json_resp = anonymise_devices(
            [d for d in json_resp if isinstance(d, dict)]
        )
        logger.info(json.dumps(json_resp, indent=2))
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
    hub = HubX(
        hass_data[CONF_TOKEN],
        hass_data[CONF_IP_ADDRESS],
        exclude_matter=hass_data.get(CONF_EXCLUDE_MATTER, DEFAULT_EXCLUDE_MATTER),
    )

    # The one write path to this hub (hub_writer.py). Its worker lives as long
    # as the entry, through Home Assistant's shutdown too.
    start_hub_writer(hass, entry, hub)

    # Lets get all kinds that we are interested in one go and create the devices
    # such that the platform can go ahead and add the associated sensors
    platform = ikea_gateway()
    # issue #39: store the gateway per config entry, not in a shared global slot.
    # A second hub entry used to clobber the first here, so one hub's devices
    # vanished and the two entries collided on duplicate IDs (multi-hub support).
    hass.data[DOMAIN][entry.entry_id]["gateway"] = platform
    logger.debug("Starting make_devices...")
    try:
        await platform.make_devices(
            hass,
            hass_data[CONF_IP_ADDRESS],
            hass_data[CONF_TOKEN],
            hass_data.get(CONF_EXCLUDE_MATTER, DEFAULT_EXCLUDE_MATTER),
        )
    except (ConnectionError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to IKEA Dirigera hub at {hass_data[CONF_IP_ADDRESS]}: {err}"
        ) from err
    
    # #75: with "Skip Matter devices" on, the devices this integration
    # imported for Matter devices before are left behind. Remove them, but
    # only those the hub still marks as Matter (the same test the filter
    # uses), so the match is certain; a device that is gone from the hub is
    # the stale-device check's business, which only logs.
    if hass_data.get(CONF_EXCLUDE_MATTER, DEFAULT_EXCLUDE_MATTER):
        await _async_remove_excluded_matter_devices(hass, entry, hass_data)

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
    # The lights the device set mode leaves out. The light platform sets
    # up from the same index, so both agree on what is hidden.
    device_set_index = DeviceSetIndex.from_lights(platform.lights)
    hass.data[DOMAIN][entry.entry_id]["device_set_index"] = device_set_index
    device_set_mode = resolve_device_set_mode(hass_data)
    hidden_lights = device_set_index.hidden_unique_ids(device_set_mode)
    if not sets_wanted(device_set_mode):
        hidden_lights |= _device_set_entity_ids(hass, entry)
    sync_platform_entity_registry(hass, entry, platforms, hidden_lights)
    # The devices that exist before the platforms set up, for the area sync:
    # it tells new devices from known ones (see sync_all_device_areas).
    devices_before_setup = frozenset(
        device.id for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id))
    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    # Now lets start the event listener
    hub_basic = Hub(hass_data[CONF_TOKEN], hass_data[CONF_IP_ADDRESS])

    hub_events = hub_event_listener(
        hub_basic, hass, discovery, entry.entry_id,
        rooms_store=Store(hass, ROOMS_STORE_VERSION, rooms_store_key(entry.entry_id)),
    )
    hub_events.start()
    try:
        # Bring renames and room moves made in the IKEA app while Home
        # Assistant was stopped into the device registry. Neither touches what
        # the user set in Home Assistant (#72 and its area follow-up).
        await hub_events.sync_all_device_names()
        await hub_events.sync_all_device_areas(devices_before_setup)
    except Exception:
        # Setup is about to fail — without this, the listener thread kept
        # running and every ConfigEntryNotReady retry started another one.
        await hass.async_add_executor_job(hub_events.stop)
        raise
    # Per-entry storage instead of a module global: a second hub entry no
    # longer clobbers the first listener, and unload stops the right one.
    hass.data[DOMAIN][entry.entry_id]["hub_events"] = hub_events
    # One GET /devices every few minutes instead of polling entities (#207).
    hub_events.start_safety_net()

    # Catches devices that left the hub while Home Assistant was stopped;
    # deviceRemoved events cover the ones that go while it runs. In the
    # background, so setup does not wait for a second /devices fetch.
    entry.async_create_background_task(
        hass,
        async_remove_stale_devices(hass, entry.entry_id, hub_basic),
        "dirigera_platform stale device check",
    )

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
        hub_events.stop_safety_net()
        await hass.async_add_executor_job(hub_events.stop)

    hass_data = dict(entry.data)
    hub = HubX(
        hass_data[CONF_TOKEN],
        hass_data[CONF_IP_ADDRESS],
        exclude_matter=hass_data.get(CONF_EXCLUDE_MATTER, DEFAULT_EXCLUDE_MATTER),
    )

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
    """Let the user delete a device only when we no longer create it: the hub
    no longer has it, or the Matter filter keeps it out (#75).

    A device that is still created comes back on the next start, so deleting
    it would only look like it worked. When the hub cannot be asked, keep it.
    """
    data = config_entry.data
    # With "Skip Matter devices" on, a Matter device on the hub is one we no
    # longer create, so the user may delete it (#75). The plain Hub listed it
    # as known and the delete was refused.
    hub = HubX(data[CONF_TOKEN], data[CONF_IP_ADDRESS],
               exclude_matter=data.get(CONF_EXCLUDE_MATTER, DEFAULT_EXCLUDE_MATTER))
    try:
        known = known_identifiers(await hass.async_add_executor_job(hub.get, "/devices"))
    except Exception as ex:
        logger.warning("Cannot check the hub before removing a device: %s", ex)
        return False
    if known is None:
        logger.warning("Cannot check the hub before removing a device: no device list")
        return False
    return is_stale(device_entry, known)


async def _async_remove_excluded_matter_devices(hass, entry, hass_data) -> list[str]:
    """Remove our devices that stand for hub devices the Matter filter keeps
    out (#75). Their entities go with them; a device of Home Assistant's own
    Matter integration is another device and is not touched."""
    try:
        raw = await hass.async_add_executor_job(
            Hub(hass_data[CONF_TOKEN], hass_data[CONF_IP_ADDRESS]).get, "/devices")
    except Exception as ex:
        logger.warning("Skip Matter devices: cannot read the hub, nothing removed: %s", ex)
        return []
    excluded = excluded_matter_identifiers(raw, is_matter_device)
    if not excluded:
        return []
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    removed = []
    for device in find_excluded_matter_devices(
            dr.async_entries_for_config_entry(device_reg, entry.entry_id), excluded):
        # Enough in the log to see afterwards what went, should a user report
        # a device that should have stayed.
        entities = [e.entity_id for e in er.async_entries_for_device(
            entity_reg, device.id, include_disabled_entities=True)]
        logger.warning(
            "Skip Matter devices is on: removing %s (hub id %s, entities %s), which the hub "
            "marks as a Matter device and this integration no longer imports (#75)",
            device.name_by_user or device.name,
            ", ".join(sorted(i for d, i in device.identifiers if d == DOMAIN)),
            ", ".join(entities) or "none")
        device_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
        removed.append(device.id)
    return removed


async def async_remove_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    """Delete what this entry stored outside the registries: the rooms last seen on its hub."""
    await Store(hass, ROOMS_STORE_VERSION, rooms_store_key(entry.entry_id)).async_remove()
