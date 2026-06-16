"""
Device Discovery Coordinator for Dirigera Platform.

This module handles dynamic discovery of new devices that are added to the
Dirigera hub after Home Assistant has started. When the WebSocket event listener
receives an event for an unknown device ID, it triggers this coordinator to
fetch the device info and create the appropriate entity.
"""
from __future__ import annotations
import logging
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from homeassistant import core

if TYPE_CHECKING:
    from .dirigera_lib_patch import HubX

logger = logging.getLogger("custom_components.dirigera_platform")

# Mapping from Dirigera deviceType to our internal platform type
DEVICE_TYPE_TO_PLATFORM = {
    "light": "light",
    "outlet": "switch",
    "airPurifier": "fan",
    "blinds": "cover",
    "environmentSensor": "sensor",
    "controller": "sensor",
    "motionSensor": "binary_sensor",
    "occupancySensor": "binary_sensor",  # MYGGSPRAY
    "lightSensor": "sensor",  # MYGGSPRAY illuminance
    "openCloseSensor": "binary_sensor",
    "waterSensor": "binary_sensor",
}


class DeviceDiscoveryCoordinator:
    """
    Coordinates discovery of new devices and their registration with Home Assistant.

    Each platform (light, sensor, switch, etc.) registers a callback that can be
    used to add new entities. When a new device is detected via WebSocket events,
    this coordinator fetches the device info and calls the appropriate callback.
    """

    def __init__(self, hass: core.HomeAssistant, hub: "HubX"):
        self._hass = hass
        self._hub = hub
        self._platform_callbacks: Dict[str, Callable] = {}
        self._known_device_ids: set = set()
        self._pending_discovery: set = set()  # Devices currently being discovered

    def register_platform_callback(self, platform: str, callback: Callable) -> None:
        """
        Register a callback for adding entities to a specific platform.

        Args:
            platform: The platform name (light, sensor, switch, etc.)
            callback: The async_add_entities function from that platform
        """
        logger.debug(f"Registering discovery callback for platform: {platform}")
        self._platform_callbacks[platform] = callback

    def register_known_device(self, device_id: str) -> None:
        """
        Register a device ID as known (already has an entity in HA).

        Args:
            device_id: The device ID from Dirigera
        """
        self._known_device_ids.add(device_id)

    def is_known_device(self, device_id: str) -> bool:
        """
        Check if a device ID is already known.

        Args:
            device_id: The device ID to check

        Returns:
            True if the device is already registered, False otherwise
        """
        return device_id in self._known_device_ids

    async def discover_device(self, device_id: str, device_type: str) -> bool:
        """
        Discover and register a new device.

        This is called when the WebSocket listener receives an event for an
        unknown device. It fetches the full device info from the API and
        creates the appropriate entity.

        Args:
            device_id: The ID of the new device
            device_type: The deviceType from the WebSocket event

        Returns:
            True if the device was successfully discovered and added
        """
        # Avoid duplicate discovery attempts
        if device_id in self._pending_discovery:
            logger.debug(f"Device {device_id} discovery already in progress, skipping")
            return False

        if device_id in self._known_device_ids:
            logger.debug(f"Device {device_id} already known, skipping discovery")
            return False

        self._pending_discovery.add(device_id)

        try:
            logger.info(f"Discovering new device: {device_id} (type: {device_type})")

            # Determine which platform handles this device type
            platform = DEVICE_TYPE_TO_PLATFORM.get(device_type)
            if platform is None:
                logger.warning(f"Unknown device type '{device_type}' for device {device_id}, cannot discover")
                return False

            # Check if we have a callback for this platform
            if platform not in self._platform_callbacks:
                logger.warning(f"No callback registered for platform '{platform}', cannot add device {device_id}")
                return False

            # Fetch the full device info from the API
            device_data = await self._hass.async_add_executor_job(
                self._hub.get, f"/devices/{device_id}"
            )

            if device_data is None:
                logger.error(f"Failed to fetch device data for {device_id}")
                return False

            logger.debug(f"Fetched device data for {device_id}: {device_data.get('attributes', {}).get('customName', 'unnamed')}")

            # Create the entity based on device type
            entity = await self._create_entity(device_type, device_data)
            if entity is None:
                logger.error(f"Failed to create entity for device {device_id}")
                return False

            # Add the entity via the platform callback
            callback = self._platform_callbacks[platform]
            callback([entity])

            # Sensor device types that also report battery_percentage
            # produce a companion battery diagnostic at startup (see
            # sensor.py:async_setup_entry). Mirror that here so devices
            # added at runtime (e.g. a freshly paired BADRING) get the
            # battery sensor without requiring a HA restart.
            self._add_battery_companion(device_type, entity)

            # Mark as known
            self._known_device_ids.add(device_id)
            logger.info(f"Successfully discovered and added device: {device_id} ({device_data.get('attributes', {}).get('customName', 'unnamed')})")

            return True

        except Exception as ex:
            logger.error(f"Error discovering device {device_id}: {ex}")
            return False

        finally:
            self._pending_discovery.discard(device_id)

    async def _create_entity(self, device_type: str, device_data: Dict[str, Any]) -> Optional[Any]:
        """
        Create an entity object for the given device type and data.

        Each device type has two layers:
        1. Device wrapper (ikea_*_device) - holds hass, hub, raw data
        2. HA entity (ikea_*_sensor) - the actual Home Assistant entity

        Args:
            device_type: The Dirigera device type
            device_data: The full device data from the API

        Returns:
            The HA entity object, or None if creation failed
        """
        try:
            # Import here to avoid circular imports
            from .light import ikea_bulb
            from .base_classes import (
                # Device wrapper classes
                ikea_blinds_device,
                ikea_starkvind_air_purifier_device,
                ikea_outlet_device,
                ikea_vindstyrka_device,
                ikea_controller_device,
                ikea_open_close_device,
                ikea_motion_sensor_device,
                ikea_water_sensor_device,
                ikea_light_sensor_device,
                # HA entity classes (wrap the device wrappers)
                ikea_outlet_switch_sensor,
                ikea_blinds_sensor,
                ikea_starkvind_air_purifier_fan,
                ikea_motion_sensor,
                ikea_open_close_sensor,
                ikea_water_sensor,
                ikea_light_sensor_lux,
            )
            from dirigera.devices.light import dict_to_light
            from dirigera.devices.outlet import dict_to_outlet
            from dirigera.devices.air_purifier import dict_to_air_purifier
            from dirigera.devices.blinds import dict_to_blind
            from dirigera.devices.open_close_sensor import dict_to_open_close_sensor
            from dirigera.devices.water_sensor import dict_to_water_sensor
            from dirigera.devices.environment_sensor import dict_to_environment_sensor
            from dirigera.devices.light_sensor import dict_to_light_sensor
            from .dirigera_lib_patch import (
                dict_to_controller,
                dict_to_motion_sensor_x,
            )

            if device_type == "light":
                # Light: ikea_bulb IS the HA entity (no separate wrapper)
                light = dict_to_light(device_data, self._hub)
                return ikea_bulb(self._hub, light)

            elif device_type == "outlet":
                # Outlet: wrapper -> HA entity.
                # Use get_outlet_by_id() so the energy-attribute merge from the
                # linked electricalSensor (GRILLPLATS / TOFSMYGGA) is applied to
                # outlets discovered at runtime via ADD events. Without this,
                # only outlets present at integration startup get power/energy
                # data; runtime-added plugs require an integration restart.
                # See issue #31.
                outlet_id = device_data.get("id")
                try:
                    # get_outlet_by_id() chains to a blocking requests call
                    # (hub.get("/devices")); run it in the executor so we don't
                    # block the event loop during runtime discovery. See issue #37.
                    outlet = await self._hass.async_add_executor_job(
                        self._hub.get_outlet_by_id, outlet_id
                    )
                except Exception as e:
                    logger.warning(
                        "get_outlet_by_id failed for runtime-discovered outlet "
                        "%s (%s); falling back to raw payload without energy "
                        "merge. Energy sensors may be missing until integration "
                        "restart.", outlet_id, e
                    )
                    outlet = dict_to_outlet(device_data, self._hub)
                device_wrapper = ikea_outlet_device(self._hass, self._hub, outlet)
                return ikea_outlet_switch_sensor(device_wrapper)

            elif device_type == "airPurifier":
                # Air Purifier: wrapper -> HA entity
                purifier = dict_to_air_purifier(device_data, self._hub)
                device_wrapper = ikea_starkvind_air_purifier_device(self._hass, self._hub, purifier)
                return ikea_starkvind_air_purifier_fan(device_wrapper)

            elif device_type == "blinds":
                # Blinds: wrapper -> HA entity
                blind = dict_to_blind(device_data, self._hub)
                device_wrapper = ikea_blinds_device(self._hass, self._hub, blind)
                return ikea_blinds_sensor(device_wrapper)

            elif device_type == "environmentSensor":
                # Environment sensor: creates multiple entities, handled differently
                # For now, skip - these need special handling (temp, humidity, PM25, etc.)
                logger.warning(f"Environment sensor discovery not yet implemented for {device_data.get('id')}")
                return None

            elif device_type == "controller":
                # Controller: creates battery sensor, handled in sensor.py
                # For now, skip - needs special handling
                logger.warning(f"Controller discovery not yet implemented for {device_data.get('id')}")
                return None

            elif device_type in ("motionSensor", "occupancySensor"):
                # Motion sensor: wrapper -> HA entity
                sensor = dict_to_motion_sensor_x(device_data, self._hub)
                device_wrapper = ikea_motion_sensor_device(self._hass, self._hub, sensor)
                return ikea_motion_sensor(device_wrapper)

            elif device_type == "lightSensor":
                # Light sensor (MYGGSPRAY illuminance): wrapper -> HA entity
                sensor = dict_to_light_sensor(device_data, self._hub)
                device_wrapper = ikea_light_sensor_device(self._hass, self._hub, sensor)
                return ikea_light_sensor_lux(device_wrapper)

            elif device_type == "openCloseSensor":
                # Open/close sensor: wrapper -> HA entity
                sensor = dict_to_open_close_sensor(device_data, self._hub)
                device_wrapper = ikea_open_close_device(self._hass, self._hub, sensor)
                return ikea_open_close_sensor(device_wrapper)

            elif device_type == "waterSensor":
                # Water sensor: wrapper -> HA entity
                sensor = dict_to_water_sensor(device_data, self._hub)
                device_wrapper = ikea_water_sensor_device(self._hass, self._hub, sensor)
                return ikea_water_sensor(device_wrapper)

            else:
                logger.warning(f"No entity creator for device type: {device_type}")
                return None

        except Exception as ex:
            logger.error(f"Error creating entity for device type {device_type}: {ex}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _add_battery_companion(self, device_type: str, primary_entity: Any) -> None:
        """Emit a battery diagnostic alongside the primary entity for sensor
        device types that report battery_percentage. lightSensor is excluded
        on purpose: today's only lightSensor is MYGGSPRAY's split half, whose
        battery is owned by the paired motionSensor."""
        if device_type not in ("waterSensor", "motionSensor", "occupancySensor", "openCloseSensor"):
            return
        wrapper = getattr(primary_entity, "_device", None)
        if wrapper is None or getattr(wrapper, "battery_percentage", None) is None:
            return
        sensor_cb = self._platform_callbacks.get("sensor")
        if sensor_cb is None:
            logger.debug("No 'sensor' callback registered, skipping battery companion")
            return
        from .base_classes import battery_percentage_sensor
        sensor_cb([battery_percentage_sensor(wrapper)])


# Global instance - will be initialized in __init__.py
discovery_coordinator: Optional[DeviceDiscoveryCoordinator] = None


def get_discovery_coordinator() -> Optional[DeviceDiscoveryCoordinator]:
    """Get the global discovery coordinator instance."""
    return discovery_coordinator


def set_discovery_coordinator(coordinator: DeviceDiscoveryCoordinator) -> None:
    """Set the global discovery coordinator instance."""
    global discovery_coordinator
    discovery_coordinator = coordinator
