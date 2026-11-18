"""Imports scenes from Dirigera as scene entities in HA."""

import logging
from typing import Any

#from dirigera import Hub
from .dirigera_lib_patch import HubX, HackScene
#from dirigera.devices.scene import Scene as DirigeraScene
#from dirigera.devices.scene import Trigger, TriggerDetails, EndTriggerEvent

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PLATFORM
from .hub_writer import writer_for
from .icons import to_hass_icon, ikea_to_hass_icon

logger = logging.getLogger("custom_components.dirigera_platform")

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create scene entities from Dirigera scene."""
    logger.debug("async_setup_entry for scenes...")

    #Commented as hack for pydantic scene issue
    #Trigger.update_forward_refs()
    #TriggerDetails.update_forward_refs()
    #EndTriggerEvent.update_forward_refs()

    gateway = hass.data[DOMAIN][entry.entry_id]["gateway"]
    async_add_entities(gateway.scenes)
    # The safety net keeps them in line with the hub (#207).
    hass.data[DOMAIN][entry.entry_id]["scenes"] = SceneSync(
        hass, gateway.hub, gateway.scenes, async_add_entities)
    logger.debug("async_setup_entry complete for scenes...")


# The scenes this integration creates on the hub for remote buttons; they
# are not scene entities (unload deletes them).
INTEGRATION_SCENE_PREFIX = "dirigera_integration_empty_scene_"


def is_integration_scene(scene) -> bool:
    return scene.name.startswith(INTEGRATION_SCENE_PREFIX)


class SceneSync:
    """The scene entities of one config entry, kept in line with the hub.

    The safety net (#207) calls async_refresh every SAFETY_NET_INTERVAL with
    one GET /scenes. A renamed scene gets its new name and icon; a new one
    gets an entity, made as at setup; one the hub no longer lists becomes
    unavailable, and available again if it comes back. It is not removed:
    one answer of the hub is not certain enough to take away an entity the
    user's automations may use (the same caution as the safety net's device
    sets). Whether the hub sends an event for a scene rename is not known."""

    def __init__(self, hass: HomeAssistant, hub: HubX, entities, async_add_entities) -> None:
        self._hass = hass
        self._hub = hub
        self._entities = {entity.unique_id: entity for entity in entities}
        self._add_entities = async_add_entities

    async def async_refresh(self) -> None:
        scenes = await self._hass.async_add_executor_job(self._hub.get_scenes)
        self.apply(scenes)

    def apply(self, scenes) -> None:
        """Event loop: bring the entities in line with the hub's scenes."""
        listed = {scene.id: scene for scene in scenes if not is_integration_scene(scene)}
        new = []
        for scene_id, scene in listed.items():
            entity = self._entities.get(scene_id)
            if entity is None:
                entity = self._entities[scene_id] = ikea_scene(self._hub, scene)
                new.append(entity)
            else:
                entity.hub_listed(scene)
        for scene_id, entity in self._entities.items():
            if scene_id not in listed:
                entity.hub_unlisted()
        if new:
            logger.info(f"New scenes on the hub: {[entity.name for entity in new]}")
            self._add_entities(new)

class ikea_scene(Scene):
    """Implements scene entity for a Dirigera scene."""

    _attr_has_entity_name = True

    def __init__(self, hub: HubX, scene: HackScene) -> None:
        """Initialize."""
        self._hub = hub
        self._scene = scene 
        self._attr_available = True

    def hub_listed(self, scene: HackScene) -> None:
        """The hub lists this scene (SceneSync): take its name and icon."""
        changed = (not self._attr_available or scene.name != self._scene.name
                   or scene.icon != self._scene.icon)
        self._scene = scene
        self._attr_available = True
        if changed and self.hass is not None:
            self.async_write_ha_state()

    def hub_unlisted(self) -> None:
        """The hub no longer lists this scene (SceneSync)."""
        if self._attr_available:
            self._attr_available = False
            if self.hass is not None:
                self.async_write_ha_state()
    
    @property
    def unique_id(self):
        return self._scene.id 
    
    @property
    def name(self) -> str:
        """Return name from Dirigera."""
        #return self._dirigera_scene.info.name
        return self._scene.name 
    
    @property
    def icon(self) -> str:
        """Return suitable replacement icon."""
        #return to_hass_icon(self._dirigera_scene.info.icon)
        return ikea_to_hass_icon(self._scene.icon)
    
    async def async_activate(self, **kwargs: Any) -> None:
        """Trigger Dirigera Scene."""
        logger.debug("Activating scene '%s' (%s)", self.name, self.unique_id)
        await writer_for(self._hub).run(self._scene.trigger)

    async def async_update(self) -> None:
        """Fetch updated scene definition from Dirigera.

        Only for homeassistant.update_entity: Home Assistant's BaseScene sets
        _attr_should_poll = False, so scenes never polled (#207 assumed they
        did). The safety net (SceneSync) keeps names in line."""
        logger.debug("Updating scene '%s' (%s)", self.name, self.unique_id)
        try:
            # NB: assign to _scene (the attribute name/icon/activate read) —
            # this used to write to a never-read _dirigera_scene leftover,
            # so hub-side scene renames/icon changes never propagated.
            self._scene = await self.hass.async_add_executor_job(self._hub.get_scene_by_id, self.unique_id)
        except Exception as ex:
            logger.error("Error encountered on update of '%s' (%s)", self.name, self.unique_id)
            logger.error(ex)
            raise HomeAssistantError from ex