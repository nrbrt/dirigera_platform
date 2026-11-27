"""
Anonymising the hub's /devices list for a diagnostics download or a dump.

Users are asked to post this data in public issues (RFC for 0.5: which hub
devices form one physical device). It must keep the structure that matters
(which ids share a relationId, the _N suffixes, links between devices) and
drop what says something about the user or their home: ids, serial numbers,
the names they gave devices, rooms and groups, the hub's coordinates and sun
times, whether they are home, and Matter pairing codes.

The old dump_data sanitiser left customName, room names, the hub's coordinates
and the hub's serial number (equal to its id) in the output.

The module is pure and loaded standalone.
"""
import copy
import importlib.util
import json
import os

_path = os.path.join(os.path.dirname(__file__), "..", "custom_components",
                     "dirigera_platform", "anonymise.py")
_spec = importlib.util.spec_from_file_location("anonymise_under_test", _path)
an = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(an)

HUB = "09c8d8af-5d91-45b4-9186-c720ee2ed2a7"
WHEEL = "29b4ad10-9efc-406f-948e-70a1d59eb921"
PLUG = "f6059d0a-76eb-4aaf-9b35-c05eb294a6cc"
ROOM = "0e75b931-ee50-4bcb-9fb4-d81c8bd30feb"
SET = "5a1b2c3d-0000-4000-8000-123456789abc"


def _devices():
    return [
        {
            "id": f"{HUB}_1", "relationId": HUB, "type": "gateway", "deviceType": "gateway",
            "attributes": {
                "customName": "Woning", "model": "DIRIGERA Hub for smart products",
                "serialNumber": HUB, "timezone": "Europe/Amsterdam",
                "nextSunSet": "2026-11-10T15:56:00.000Z", "nextSunRise": "2026-11-11T06:53:00.000Z",
                "sunPosition": "sunUp", "homestate": "home",
                "coordinates": {"latitude": 52.17, "longitude": 5.28, "accuracy": -1},
            },
            "deviceSet": [], "remoteLinks": [],
        },
        {
            "id": f"{WHEEL}_3", "relationId": WHEEL, "type": "controller", "deviceType": "genericSwitch",
            "attributes": {"customName": "Wiel trap", "model": "BILRESA scroll wheel",
                           "serialNumber": "103597000011F48F", "switchLabel": "button",
                           "qrCode": "MT:Y.K90AFN00KA0648G00", "setupCode": "34970112332",
                           "firmwareVersion": "1.9.15"},
            "room": {"id": ROOM, "name": "Overloop 2e verdieping", "color": "ikea_red_no_39"},
            "deviceSet": [], "remoteLinks": [],
        },
        {
            "id": f"{WHEEL}_6", "relationId": WHEEL, "type": "controller", "deviceType": "genericSwitch",
            "attributes": {"customName": "Wiel trap", "model": "BILRESA scroll wheel"},
            "room": {"id": ROOM, "name": "Overloop 2e verdieping"},
            "deviceSet": [], "remoteLinks": [],
        },
        {
            "id": f"{PLUG}_1", "relationId": PLUG, "type": "outlet", "deviceType": "outlet",
            "attributes": {"customName": "Koffie", "model": "TRETAKT Smart plug"},
            "deviceSet": [{"id": SET, "name": "Keuken"}],
            "remoteLinks": [f"{WHEEL}_3"],
        },
    ]


def _out():
    return an.anonymise_devices(_devices())


def test_no_original_identifier_or_private_value_survives():
    text = json.dumps(_out())
    for secret in (HUB, WHEEL, PLUG, ROOM, SET, "103597000011F48F", "Woning", "Wiel trap",
                   "Koffie", "Keuken", "Overloop", "52.17", "5.28", "15:56", "06:53",
                   "MT:Y.K90AFN00KA0648G00", "34970112332", '"home"'):
        assert secret not in text, secret


def test_split_device_keeps_its_structure():
    out = _out()
    wheel_3, wheel_6 = out[1], out[2]
    base = wheel_3["relationId"]
    assert base == wheel_6["relationId"]
    assert wheel_3["id"] == f"{base}_3"
    assert wheel_6["id"] == f"{base}_6"
    assert base.startswith("device-")


def test_hub_ids_are_numbered_in_order():
    out = _out()
    assert out[0]["id"] == "device-01_1"
    assert out[1]["id"] == "device-02_3"
    assert out[3]["id"] == "device-03_1"


def test_links_between_devices_follow_the_same_mapping():
    out = _out()
    assert out[3]["remoteLinks"] == [out[1]["id"]]


def test_rooms_and_sets_get_their_own_neutral_ids():
    out = _out()
    assert out[1]["room"]["id"] == "room-01"
    assert out[2]["room"]["id"] == "room-01"
    assert out[3]["deviceSet"][0]["id"] == "set-01"


def test_what_matters_for_grouping_is_kept():
    out = _out()
    attrs = out[1]["attributes"]
    assert attrs["model"] == "BILRESA scroll wheel"
    assert attrs["switchLabel"] == "button"
    assert attrs["firmwareVersion"] == "1.9.15"
    assert out[1]["room"]["color"] == "ikea_red_no_39"
    assert out[0]["attributes"]["timezone"] == "Europe/Amsterdam"


def test_private_values_are_marked_not_dropped():
    out = _out()
    assert out[1]["attributes"]["customName"] == an.REDACTED
    assert out[1]["attributes"]["serialNumber"] == an.REDACTED
    assert out[0]["attributes"]["coordinates"] == an.REDACTED


def test_input_is_not_changed():
    devices = _devices()
    before = copy.deepcopy(devices)
    an.anonymise_devices(devices)
    assert devices == before


def test_an_id_that_is_not_a_uuid_is_mapped_too():
    out = an.anonymise_devices([{"id": "abc_2", "relationId": "abc", "attributes": {}}])
    assert out[0]["id"] == "device-01_2"
    assert out[0]["relationId"] == "device-01"


def test_a_uuid_in_an_unexpected_place_is_replaced():
    other = "11111111-2222-4333-8444-555555555555"
    out = an.anonymise_devices([{"id": f"{PLUG}_1", "attributes": {"note": f"linked to {other}"}}])
    assert other not in json.dumps(out)
