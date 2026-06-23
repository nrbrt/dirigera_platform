from __future__ import annotations
from typing import Any, Dict, List, Optional

from dirigera import Hub

from dirigera.devices.device import Attributes, Device
from dirigera.devices.outlet import Outlet, dict_to_outlet
from dirigera.devices.environment_sensor import EnvironmentSensor, dict_to_environment_sensor
from dirigera.hub.abstract_smart_home_hub import AbstractSmartHomeHub
from dirigera.devices.scene import Info, Icon, Trigger, TriggerDetails, ControllerType
import logging

logger = logging.getLogger("custom_components.dirigera_platform")


class HubX(Hub):
    def __init__(
        self, token: str, ip_address: str, port: str = "8443", api_version: str = "v1"
    ) -> None:
        super().__init__(token, ip_address, port, api_version)
        # issue #38 punt 1: batch-cache voor /devices. Tijdens make_devices() roepen
        # ~10 get_X-methoden elk self.get("/devices") aan -> 10x de volledige device-lijst
        # over een verse TLS-handshake. Met een actieve batch fetcht alleen de eerste; de
        # rest hergebruikt. Cache is UITSLUITEND actief binnen begin/end_devices_batch
        # (alleen make_devices), dus runtime-calls (get_*_by_id, reload) halen verse data op.
        self._devices_cache_active = False
        self._devices_cache = None
        self._devices_fetch_count = 0   # aantal échte /devices-fetches binnen de laatste batch

    def get(self, route: str):
        if route == "/devices" and self._devices_cache_active:
            if self._devices_cache is None:
                self._devices_fetch_count += 1
                self._devices_cache = super().get(route)
            return self._devices_cache
        return super().get(route)

    def begin_devices_batch(self) -> None:
        """Start het coalescen van /devices-fetches (één fetch voor de hele make_devices-run)."""
        self._devices_cache = None
        self._devices_fetch_count = 0
        self._devices_cache_active = True

    def end_devices_batch(self) -> None:
        """Stop coalescen en gooi de cache weg (runtime krijgt weer verse data)."""
        self._devices_cache_active = False
        self._devices_cache = None

    def get_controllers(self) -> List[ControllerX]:
        """
        Fetches all controllers registered in the Hub
        """
        devices = self.get("/devices")
        controllers = list(filter(lambda x: x["type"] == "controller", devices))
        return [dict_to_controller(controller, self) for controller in controllers]
    
    # Scenes are a problem so making a hack
    def get_scenes(self):
        """
        Fetches all controllers registered in the Hub
        """
        scenes = self.get("/scenes")
        #scenes = list(filter(lambda x: x["type"] == "scene", devices))
        
        return [HackScene.make_scene(self, scene) for scene in scenes]
    
    def get_scene_by_id(self, scene_id: str):
        """
        Fetches a specific scene by a given id
        """
        data = self.get(f"/scenes/{scene_id}")
        return HackScene.make_scene(self, data)
    
    def create_empty_scene(self, controller_id: str, clicks_supported:list):
        logger.debug(f"Creating empty scene for controller : {controller_id} with clicks : {clicks_supported}")
        for click in clicks_supported:
            scene_name = f'dirigera_integration_empty_scene_{controller_id}_{click}'
            info = Info(name=f'dirigera_integration_empty_scene_{controller_id}_{click}', icon=Icon.SCENES_CAKE)
            device_trigger = Trigger(type="controller", disabled=False,
                                     trigger=TriggerDetails(clickPattern=click, buttonIndex=0, deviceId=controller_id, controllerType=ControllerType.SHORTCUT_CONTROLLER))

            logger.debug(f"Creating empty scene : {info.name}")
            #self.create_scene(info=info, scene_type=SceneType.USER_SCENE,triggers=[device_trigger])
            data = {
                        "info": {"name" : scene_name, "icon" : "scenes_cake"},
                        "type": "customScene",
                        "triggers":[
                                        {
                                            "type": "controller", 
                                            "disabled": False, 
                                            "trigger": 
                                                {
                                                    "controllerType": "shortcutController",
                                                    "clickPattern": click,
                                                    "buttonIndex": 0,
                                                    "deviceId": controller_id
                                                }
                                        }
                                    ],
                "actions": []
            }
            
            self.post("/scenes/", data=data)
        
    def delete_empty_scenes(self):
        scenes = self.get_scenes()
        for scene in scenes:
            if scene.name.startswith("dirigera_integration_empty_scene_"):
                logger.debug(f"Deleting Scene id: {scene.id} name: {scene.name}...")
                self.delete_scene(scene.id)

    def get_outlets(self) -> list:
        """
        Fetches all outlets registered in the Hub.
        For split-device plugs (GRILLPLATS, TOFSMYGGA), merges energy attributes
        from the linked electricalSensor device into the outlet.
        """
        devices = self.get("/devices")
        outlets = list(filter(lambda x: x["type"] == "outlet", devices))
        electrical_sensors = list(filter(
            lambda x: x.get("deviceType") == "electricalSensor", devices
        ))

        # Build lookup: relationId -> electricalSensor attributes
        energy_by_relation = {}
        for sensor in electrical_sensors:
            rel_id = sensor.get("relationId")
            if rel_id:
                energy_by_relation[rel_id] = sensor.get("attributes", {})

        # Merge energy attributes into outlets that have a matching relationId
        energy_attrs = [
            ("currentActivePower", "current_active_power"),
            ("currentAmps", "current_amps"),
            ("currentVoltage", "current_voltage"),
            ("totalEnergyConsumed", "total_energy_consumed"),
            ("totalEnergyConsumedLastUpdated", "total_energy_consumed_last_updated"),
            ("energyConsumedAtLastReset", "energy_consumed_at_last_reset"),
            ("timeOfLastEnergyReset", "time_of_last_energy_reset"),
        ]

        for outlet in outlets:
            rel_id = outlet.get("relationId")
            if rel_id and rel_id in energy_by_relation:
                sensor_attrs = energy_by_relation[rel_id]
                for api_key, snake_key in energy_attrs:
                    if api_key in sensor_attrs and sensor_attrs[api_key] is not None:
                        outlet.setdefault("attributes", {})[api_key] = sensor_attrs[api_key]
                logger.debug(
                    f"Merged energy attributes from electricalSensor into outlet "
                    f"'{outlet.get('attributes', {}).get('customName', '?')}' "
                    f"(relationId: {rel_id})"
                )

        return [dict_to_outlet(outlet, self) for outlet in outlets]

    def get_outlet_by_id(self, id_: str) -> Outlet:
        """
        Fetches an outlet by ID, merging energy data from linked electricalSensor.
        """
        # Fetch all to enable relationId matching
        outlets = self.get_outlets()
        for outlet in outlets:
            if outlet.id == id_:
                return outlet
        raise ValueError(f"No outlet found with id {id_}")

    def get_environment_sensors(self) -> list:
        """
        Fetches all environment sensors registered in the Hub.
        For split-device sensors (TIMMERFLOTTE), merges attributes from
        multiple devices sharing the same relationId into a single sensor.
        TIMMERFLOTTE exposes temperature and humidity as separate devices.
        """
        devices = self.get("/devices")
        env_sensors = list(filter(
            lambda x: x.get("deviceType") == "environmentSensor", devices
        ))

        if not env_sensors:
            return []

        # Group by relationId to find split-device sensors
        by_relation = {}
        standalone = []
        for sensor in env_sensors:
            rel_id = sensor.get("relationId")
            if rel_id:
                if rel_id not in by_relation:
                    by_relation[rel_id] = []
                by_relation[rel_id].append(sensor)
            else:
                standalone.append(sensor)

        merged = []

        # Merge split-device sensors
        for rel_id, group in by_relation.items():
            if len(group) == 1:
                merged.append(group[0])
            else:
                # Multiple devices with same relationId — merge attributes
                base = group[0].copy()
                base_attrs = base.get("attributes", {})
                for other in group[1:]:
                    other_attrs = other.get("attributes", {})
                    for key, value in other_attrs.items():
                        if key not in base_attrs or base_attrs[key] is None:
                            base_attrs[key] = value
                base["attributes"] = base_attrs
                logger.debug(
                    f"Merged {len(group)} environment sensor devices for "
                    f"'{base_attrs.get('customName', '?')}' (relationId: {rel_id})"
                )
                merged.append(base)

        merged.extend(standalone)
        return [dict_to_environment_sensor(s, self) for s in merged]

    def get_environment_sensor_by_id(self, id_: str) -> EnvironmentSensor:
        """
        Fetches an environment sensor by ID, with relationId merging for split-device sensors.
        """
        # Use the full get_environment_sensors() to get merged results
        sensors = self.get_environment_sensors()
        for sensor in sensors:
            if sensor.id == id_:
                return sensor
        raise ValueError(f"No environment sensor found with id {id_}")

    def get_motion_sensors(self) -> List[MotionSensorX]:
        """
        Fetches all motion sensors registered in the Hub.
        Includes both motionSensor and occupancySensor device types.
        IKEA MYGGSPRAY sensors report as occupancySensor instead of motionSensor.
        """
        devices = self.get("/devices")
        sensors = list(filter(lambda x: x["deviceType"] in ("motionSensor", "occupancySensor"), devices))
        return [dict_to_motion_sensor_x(sensor, self) for sensor in sensors]

    def get_motion_sensor_by_id(self, id_: str) -> MotionSensorX:
        """
        Fetches a motion sensor by ID.
        Accepts both motionSensor and occupancySensor device types.
        """
        motion_sensor = self._get_device_data_by_id(id_)
        if motion_sensor["deviceType"] not in ("motionSensor", "occupancySensor"):
            raise ValueError("Device is not a MotionSensor or OccupancySensor")
        return dict_to_motion_sensor_x(motion_sensor, self)


class ControllerAttributesX(Attributes):
    is_on: Optional[bool] = None
    battery_percentage: Optional[int] = None
    switch_label: Optional[str] = None

class ControllerX(Device):
    dirigera_client: AbstractSmartHomeHub
    attributes: ControllerAttributesX

    def reload(self) -> ControllerX:
        data = self.dirigera_client.get(route=f"/devices/{self.id}")
        return ControllerX(dirigeraClient=self.dirigera_client, **data)

    def set_name(self, name: str) -> None:
        if "customName" not in self.capabilities.can_receive:
            raise AssertionError(
                "This controller does not support the set_name function"
            )

        data = [{"attributes": {"customName": name}}]
        self.dirigera_client.patch(route=f"/devices/{self.id}", data=data)
        self.attributes.custom_name = name

def dict_to_controller(
    data: Dict[str, Any], dirigera_client: AbstractSmartHomeHub
) -> ControllerX:
    return ControllerX(dirigeraClient=dirigera_client, **data)

class HackScene():

    def __init__(self, hub, id, name, icon):
        self.hub = hub
        self.id = id 
        self.name = name 
        self.icon = icon

    def parse_scene_json(json_data):
        id = json_data["id"]
        name = json_data["info"]["name"]
        icon = json_data["info"]["icon"]
        return id, name, icon 
    
    def make_scene(dirigera_client, json_data):
        id, name, icon = HackScene.parse_scene_json(json_data)
        return HackScene(dirigera_client, id, name, icon)
    
    def reload(self) -> HackScene:
        data = self.hub.get(f"/scenes/{self.id}")
        return HackScene.make_scene(self.hub, data)

    def trigger(self) -> None:
        self.hub.post(route=f"/scenes/{self.id}/trigger")

    def undo(self) -> None:
        self.hub.post(route=f"/scenes/{self.id}/undo")


# Motion sensor patch for MYGGSPRAY (occupancySensor)
# MYGGSPRAY sensors don't have is_on attribute, so we make it optional
class MotionSensorAttributesX(Attributes):
    battery_percentage: Optional[int] = None
    is_on: Optional[bool] = None  # Made optional for MYGGSPRAY compatibility
    light_level: Optional[float] = None
    is_detected: Optional[bool] = False


class MotionSensorX(Device):
    dirigera_client: AbstractSmartHomeHub
    attributes: MotionSensorAttributesX

    def reload(self) -> "MotionSensorX":
        data = self.dirigera_client.get(route=f"/devices/{self.id}")
        return MotionSensorX(dirigeraClient=self.dirigera_client, **data)

    def set_name(self, name: str) -> None:
        if "customName" not in self.capabilities.can_receive:
            raise AssertionError("This sensor does not support the set_name function")
        data = [{"attributes": {"customName": name}}]
        self.dirigera_client.patch(route=f"/devices/{self.id}", data=data)
        self.attributes.custom_name = name


def dict_to_motion_sensor_x(
    data: Dict[str, Any], dirigera_client: AbstractSmartHomeHub
) -> MotionSensorX:
    return MotionSensorX(dirigeraClient=dirigera_client, **data)