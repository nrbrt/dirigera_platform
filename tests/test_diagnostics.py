"""
Diagnostics download: Settings > Devices & services > the hub > Download
diagnostics. Asked for in the RFC for 0.5 so users with devices we do not own
can share their hub's raw /devices list.

Nothing is collected or sent by itself: Home Assistant only calls this when the
user clicks. The file must not contain the hub's address or token, and the
device list goes through the same anonymiser as dump_data.

Loaded standalone with stubbed Home Assistant and dirigera modules.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

PKG = "dirigera_platform_diag_under_test"
_base = os.path.join(os.path.dirname(__file__), "..", "custom_components", "dirigera_platform")

_const = types.ModuleType("homeassistant.const")
_const.CONF_IP_ADDRESS = "ip_address"
_const.CONF_TOKEN = "token"
_loader = types.ModuleType("homeassistant.loader")


class _Integration:
    version = "0.4.1b1"


async def _get_integration(hass, domain):
    return _Integration()


_loader.async_get_integration = _get_integration
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules["homeassistant.const"] = _const
sys.modules["homeassistant.loader"] = _loader

_hub = {"devices": None, "error": None, "calls": []}
_dirigera = types.ModuleType("dirigera")


class _Hub:
    def __init__(self, token, ip):
        _hub["calls"].append((token, ip))

    def get(self, route):
        assert route == "/devices"
        if _hub["error"]:
            raise _hub["error"]
        return _hub["devices"]


_dirigera.Hub = _Hub
sys.modules.setdefault("dirigera", _dirigera)

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [_base]
sys.modules[PKG] = _pkg
for name in ("const", "anonymise", "diagnostics"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", os.path.join(_base, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{name}"] = mod
    spec.loader.exec_module(mod)
diag = sys.modules[f"{PKG}.diagnostics"]


@pytest.fixture(autouse=True)
def _fake_hub(monkeypatch):
    # Other test modules put their own dirigera stub in sys.modules; patch the
    # name this module actually calls.
    monkeypatch.setattr(diag, "Hub", _Hub)

IP = "172.16.66.193"
TOKEN = "eyJhbGciOiJFUzI1NiJ9.secret-token"
WHEEL = "29b4ad10-9efc-406f-948e-70a1d59eb921"


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _Entry:
    data = {"ip_address": IP, "token": TOKEN, "hide_device_set_bulbs": True,
            "enabled_platforms": ["light", "sensor"]}


def _run():
    return asyncio.run(diag.async_get_config_entry_diagnostics(_Hass(), _Entry()))


def _reset(devices=None, error=None):
    _hub.update(devices=devices, error=error, calls=[])


def test_contains_anonymised_devices_and_the_version():
    _reset([{"id": f"{WHEEL}_3", "relationId": WHEEL, "deviceType": "genericSwitch",
             "attributes": {"customName": "Wiel trap", "model": "BILRESA scroll wheel"}}])
    out = _run()
    assert out["integration_version"] == "0.4.1b1"
    assert out["devices"][0]["id"] == "device-01_3"
    assert out["devices"][0]["attributes"]["model"] == "BILRESA scroll wheel"
    assert _hub["calls"] == [(TOKEN, IP)]


def test_address_token_and_names_are_not_in_the_file():
    _reset([{"id": f"{WHEEL}_3", "relationId": WHEEL,
             "attributes": {"customName": "Wiel trap"}}])
    text = json.dumps(_run())
    for secret in (IP, TOKEN, WHEEL, "Wiel trap"):
        assert secret not in text, secret


def test_options_are_included_without_address_and_token():
    _reset([])
    options = _run()["options"]
    assert options["hide_device_set_bulbs"] is True
    assert options["enabled_platforms"] == ["light", "sensor"]
    assert options["ip_address"] != IP and options["token"] != TOKEN


def test_counts_hub_devices_and_physical_devices():
    _reset([
        {"id": f"{WHEEL}_3", "relationId": WHEEL, "attributes": {}},
        {"id": f"{WHEEL}_6", "relationId": WHEEL, "attributes": {}},
        {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee_1", "attributes": {}},
    ])
    out = _run()
    assert out["hub_device_count"] == 3
    assert out["physical_device_count"] == 2


def test_a_hub_error_is_reported_without_the_address():
    _reset(error=ConnectionError(f"HTTPSConnectionPool(host='{IP}', port=8443): timed out"))
    out = _run()
    assert out["devices"] == []
    assert "timed out" in out["error"]
    assert IP not in json.dumps(out)
