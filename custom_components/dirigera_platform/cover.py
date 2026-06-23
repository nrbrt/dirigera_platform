import logging

from homeassistant import config_entries, core

from .const import DOMAIN, PLATFORM, DISCOVERY_COORDINATOR
from .base_classes import ikea_blinds_sensor

logger = logging.getLogger("custom_components.dirigera_platform")

async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    logger.debug("BLINDS Starting async_setup_entry")
    """Setup sensors from a config entry created in the integrations UI."""
    
    devices = hass.data[DOMAIN][config_entry.entry_id]["gateway"].blinds
    
    blinds_sensors = [ikea_blinds_sensor(x) for x in devices]        
    logger.debug(f"Found {len(blinds_sensors)} blinds sensors to setup...")
    
    async_add_entities(blinds_sensors)

    # Register callback and known devices with discovery coordinator
    discovery = hass.data[DOMAIN][config_entry.entry_id].get("discovery")
    if discovery:
        discovery.register_platform_callback("cover", async_add_entities)
        for blind in devices:
            discovery.register_known_device(blind._json_data.id)
        logger.debug(f"Registered {len(devices)} blinds with discovery coordinator")

    logger.debug("BLINDS Complete async_setup_entry")