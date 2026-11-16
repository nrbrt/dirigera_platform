"""Turn a raw hub dump into a pintest fixture.

Input: JSON {"devices": [...], "scenes": [...]} as the hub returns /devices and
/scenes. Output: the same, anonymised with the integration's own anonymiser, but
with names, rooms and set names replaced by neutral placeholders that keep which
devices share a name, a room or a set. The anonymiser alone redacts every name to
the same string, and then the pintest could not see a name moving between devices.

    python tests_ha/make_fixture.py raw.json > tests_ha/fixtures/<name>.json
"""
import copy
import importlib.util
import json
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "anonymise",
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "dirigera_platform", "anonymise.py"),
)
anonymise = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(anonymise)


class Placeholders:
    def __init__(self, prefix):
        self.prefix, self.seen = prefix, {}

    def __call__(self, value):
        if value is None or value == "":
            return value
        return self.seen.setdefault(value, f"{self.prefix} {len(self.seen) + 1}")


def neutral_names(devices, scenes):
    dev_name, room_name, set_name, scene_name = (
        Placeholders("Device"), Placeholders("Room"), Placeholders("Set"), Placeholders("Scene"))
    devices = copy.deepcopy(devices)
    for d in devices:
        attrs = d.get("attributes") or {}
        if "customName" in attrs:
            attrs["customName"] = dev_name(attrs["customName"])
        if isinstance(d.get("room"), dict):
            d["room"]["name"] = room_name(d["room"].get("name"))
        for s in d.get("deviceSet") or []:
            s["name"] = set_name(s.get("name"))
    scenes = copy.deepcopy(scenes)
    for s in scenes:
        info = s.get("info") or {}
        if "name" in info:
            info["name"] = scene_name(info["name"])
    return devices, scenes


def restore(anon, named):
    """Copy the placeholder names onto the anonymised list (same order, same shape)."""
    for a, n in zip(anon, named):
        if "customName" in (n.get("attributes") or {}):
            a["attributes"]["customName"] = n["attributes"]["customName"]
        if isinstance(n.get("room"), dict) and isinstance(a.get("room"), dict):
            a["room"]["name"] = n["room"]["name"]
        for sa, sn in zip(a.get("deviceSet") or [], n.get("deviceSet") or []):
            sa["name"] = sn["name"]
    return anon


def main(path):
    raw = json.load(open(path))
    devices, scenes = neutral_names(raw["devices"], raw.get("scenes", []))
    # One call, so one id mapping: a scene's actions keep pointing at the
    # same devices, and a scene id can never equal a device's id.
    both = anonymise.anonymise_devices(devices + scenes)
    anon = restore(both[:len(devices)], devices)
    anon_scenes = both[len(devices):]
    for a, n in zip(anon_scenes, scenes):
        if "name" in (n.get("info") or {}):
            a["info"]["name"] = n["info"]["name"]
    json.dump({"devices": anon, "scenes": anon_scenes}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1])
