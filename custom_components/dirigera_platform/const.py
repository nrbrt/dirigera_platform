DOMAIN = "dirigera_platform"
PLATFORM = "dirigera_platform"
DISCOVERY_COORDINATOR = "discovery_coordinator"

# How lights that belong to an IKEA device set are imported.
#
#   sets_only        one light entity per set; its lights are not imported on
#                    their own. Matches what the IKEA app shows.
#   sets_and_lights  the set, plus each light in it as its own entity.
#   lights_only      no set entities; every light is imported on its own.
#
# Lights that are in no set are imported with every choice.
CONF_DEVICE_SET_MODE = "device_set_mode"
DEVICE_SET_MODE_SETS_ONLY = "sets_only"
DEVICE_SET_MODE_SETS_AND_LIGHTS = "sets_and_lights"
DEVICE_SET_MODE_LIGHTS_ONLY = "lights_only"
DEFAULT_DEVICE_SET_MODE = DEVICE_SET_MODE_SETS_ONLY
LEGACY_DEVICE_SET_MODE = DEVICE_SET_MODE_SETS_AND_LIGHTS

# The model a device set's device carries, which tells set entities apart
# from light entities in the registries.
DEVICE_SET_MODEL = "Device Set"

# The choices in the order the config flow shows them. Their labels are in
# strings.json under selector.device_set_mode.
DEVICE_SET_MODES = (
    DEVICE_SET_MODE_SETS_ONLY,
    DEVICE_SET_MODE_SETS_AND_LIGHTS,
    DEVICE_SET_MODE_LIGHTS_ONLY,
)

# Minimum interval (seconds) between HA state pushes for high-frequency power
# sensors (current amps/active power/voltage). The hub pushes these over the
# WebSocket every ~8s, flooding the HA recorder (~10,800 writes/day/entity).
# The sensor's internal value is still updated on every push; only the HA
# notification (and its recorder write) is rate-limited. 0 disables throttling.
# See issue #40.
CONF_POWER_PUSH_THROTTLE = "power_push_throttle_seconds"
DEFAULT_POWER_PUSH_THROTTLE = 60

# Issue #47: users who run Matter and Dirigera side by side get every device
# twice in HA, because both integrations import the same hardware. Scenes are
# the exception, since Matter does not sync them. Letting the user pick which
# platforms this integration sets up makes it possible to import only what
# Matter does not already cover.
#
# Stored in entry.data (not entry.options) to match the two existing options.
# A missing key means "everything", so entries created before this option keep
# behaving exactly as they did.
CONF_ENABLED_PLATFORMS = "enabled_platforms"

# Kept as plain strings on purpose: homeassistant.const.Platform is a StrEnum,
# so these round-trip through the JSON config entry without a custom encoder.
ALL_PLATFORMS = [
    "switch",
    "binary_sensor",
    "light",
    "sensor",
    "cover",
    "fan",
    "scene",
]

DEFAULT_ENABLED_PLATFORMS = list(ALL_PLATFORMS)

# Issue #49: IKEA's Matter over Thread range and the older Zigbee range sit on
# the same hub. Someone who has already added the Matter devices to Home
# Assistant through the Matter integration gets them a second time from here,
# and has to disable each one by hand. This option drops them at the source.
#
# The hub marks the devices it commissioned over Matter by adding the Matter
# commissioning attributes below. Their values are blanked out (discriminator
# is -1, qrCode and setupCode are empty strings), so presence of the key is the
# signal and the value is not. Measured on hub firmware 26.4.x: present on all
# 14 Matter over Thread devices and on none of the 9 Zigbee ones.
#
# Note that this is an undocumented marker, not a protocol field the hub
# promises us, which is why the filter fails closed: a device is dropped only
# when the marker is positively there. Anything unrecognised keeps being
# imported rather than silently disappearing from someone's setup.
#
# Stored in entry.data like the options above. A missing key means "import
# everything", so entries created before this option behave exactly as before.
# Not to be confused with #47, which reads like the same request and is not.
# There are two different causes of duplicate devices:
#
#   #47  the hub acts as a Matter Bridge and exposes its ZIGBEE devices to
#        Matter ecosystems, so everything shows up twice except scenes. Those
#        devices carry no marker, so this option does nothing for that case;
#        CONF_ENABLED_PLATFORMS is the handle there.
#   #49  the Matter over Thread devices are commissioned into HA directly, so
#        only those duplicate. That is the case this option covers.
#
# The two are complementary and neither replaces the other. This is also why
# the user-facing label says "Matter over Thread" rather than "Matter".
CONF_EXCLUDE_MATTER = "exclude_matter_devices"
DEFAULT_EXCLUDE_MATTER = False

MATTER_ATTRIBUTE_KEYS = ("discriminator", "qrCode", "setupCode")

# Labels for the multi-select in the config flow.
PLATFORM_LABELS = {
    "switch": "Switches and outlets",
    "binary_sensor": "Binary sensors (motion, open/close)",
    "light": "Lights",
    "sensor": "Sensors (temperature, humidity, energy)",
    "cover": "Blinds and covers",
    "fan": "Air purifiers",
    "scene": "Scenes",
}

# The id of the hub room each device was last seen in, per config entry, so a restart
# only moves a device whose room changed on the hub (see
# hub_event_listener.sync_all_device_areas).
ROOMS_STORE_VERSION = 1


def rooms_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.rooms"
