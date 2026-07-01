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
