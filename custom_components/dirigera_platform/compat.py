"""Compatibility shims for Home Assistant API changes.

HA 2026.9 deprecates CONCENTRATION_MICROGRAMS_PER_CUBIC_METER and
CONCENTRATION_PARTS_PER_MILLION in favour of UnitOfDensity and UnitOfRatio,
with removal in 2027.8 (issue #52). The replacements only exist in recent
releases, so import them when present and fall back otherwise; the string
values are identical ("µg/m³", "ppm"), so existing sensors are unaffected.
"""
try:
    from homeassistant.const import UnitOfDensity, UnitOfRatio

    MICROGRAMS_PER_CUBIC_METER = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    PARTS_PER_MILLION = UnitOfRatio.PARTS_PER_MILLION
except ImportError:  # Home Assistant before UnitOfDensity/UnitOfRatio
    from homeassistant.const import (
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER as MICROGRAMS_PER_CUBIC_METER,
        CONCENTRATION_PARTS_PER_MILLION as PARTS_PER_MILLION,
    )

