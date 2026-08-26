DOMAIN = "dirigera_platform"
PLATFORM = "dirigera_platform"
CONF_HIDE_DEVICE_SET_BULBS = "hide_device_set_bulbs"
DISCOVERY_COORDINATOR = "discovery_coordinator"

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
