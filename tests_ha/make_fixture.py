"""Turn a raw hub dump into a pintest fixture.

Input: JSON {"devices": [...], "scenes": [...]} as the hub returns /devices and
/scenes. Output: the same, anonymised with the integration's own anonymiser,
but with names, rooms and set names replaced by neutral placeholders that keep
which devices share a name, a room or a set. The anonymiser alone redacts every
name to the same string, and then the pintest could not see a name moving
between devices.

    python tests_ha/make_fixture.py raw.json > tests_ha/fixtures/<name>.json

Check the output before committing it: the anonymiser decides what leaves the
house, name_slots() below only decides which redacted names get a placeholder.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path

_ANONYMISE = Path(__file__).parent.parent / "custom_components" / "dirigera_platform" / "anonymise.py"
_spec = importlib.util.spec_from_file_location("anonymise", _ANONYMISE)
anonymise = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(anonymise)


def name_slots(item: dict) -> Iterator[tuple[dict, str, str]]:
    """Every place a user-given name sits in a hub device or scene: (container, key, kind).

    The one place that knows where names are. A raw item and its anonymised
    copy have the same shape, so walking both gives the slots in the same order.
    """
    attributes = item.get("attributes")
    if isinstance(attributes, dict) and "customName" in attributes:
        yield attributes, "customName", "Device"
    room = item.get("room")
    if isinstance(room, dict) and "name" in room:
        yield room, "name", "Room"
    for device_set in item.get("deviceSet") or []:
        if "name" in device_set:
            yield device_set, "name", "Set"
    info = item.get("info")
    if isinstance(info, dict) and "name" in info:
        yield info, "name", "Scene"


def build_fixture(raw: dict) -> dict:
    devices, scenes = raw["devices"], raw.get("scenes", [])
    # One call, so one id mapping: a scene's actions keep pointing at the same
    # devices, and a scene id can never equal a device's id.
    anonymised = anonymise.anonymise_devices(devices + scenes)

    placeholders: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    for raw_item, anon_item in zip(devices + scenes, anonymised, strict=True):
        for (raw_slot, key, kind), (anon_slot, _, _) in zip(
                name_slots(raw_item), name_slots(anon_item), strict=True):
            value = raw_slot[key]
            if not value:
                continue
            if (kind, value) not in placeholders:
                counters[kind] = counters.get(kind, 0) + 1
                placeholders[(kind, value)] = f"{kind} {counters[kind]}"
            anon_slot[key] = placeholders[(kind, value)]
    return {"devices": anonymised[:len(devices)], "scenes": anonymised[len(devices):]}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <raw hub dump.json>", file=sys.stderr)
        return 2
    raw = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    json.dump(build_fixture(raw), sys.stdout, indent=1, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
