import json

import httpx
import pytest

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools import vpn as vpn_module
from intelligent_home_agent.tools.vpn import Vpn, country_name


class Gluetun:
    """gluetun's control server: its VPN settings, status and public IP."""

    def __init__(self, exits=None):
        self.calls = []
        self.status = "running"
        self.country = ""
        self.exits = exits or {"Spain": {"country": "Spain", "city": "Madrid"}}
        self.lookups = 0

    def __call__(self, request):
        assert request.headers["X-API-Key"] == "secret"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/v1/vpn/settings":
            country = body["provider"]["server_selection"]["countries"][0]
            if country not in self.exits:
                return httpx.Response(400, text=f"country {country} not valid")
            self.country = country
            return httpx.Response(200, text="restarted")
        if request.url.path == "/v1/vpn/status" and request.method == "PUT":
            self.status = body["status"]
            return httpx.Response(200, json={"outcome": body["status"]})
        if request.url.path == "/v1/vpn/status":
            return httpx.Response(200, json={"status": self.status})
        if request.url.path == "/v1/publicip/ip":
            # The address is looked up again once the tunnel is up: empty at first.
            self.lookups += 1
            if self.lookups < 2:
                return httpx.Response(200, json={"public_ip": ""})
            exit_ = self.exits[self.country]
            return httpx.Response(
                200, json={"public_ip": "192.0.2.3", "timezone": "Europe/Madrid", **exit_}
            )
        return httpx.Response(404)


@pytest.fixture
def gluetun(monkeypatch):
    monkeypatch.setattr(vpn_module, "POLL_SECONDS", 0)
    return Gluetun()


def make_vpn(gluetun):
    http = httpx.AsyncClient(transport=httpx.MockTransport(gluetun))
    return Vpn(http, "http://vpn:8000/", "secret")


async def test_connect_waits_until_the_internet_sees_that_country(gluetun):
    exit_ = await make_vpn(gluetun).connect("Spain")

    assert exit_.country == "Spain" and exit_.city == "Madrid"
    assert exit_.timezone == "Europe/Madrid" and exit_.locale == "es-ES"
    assert gluetun.calls[:2] == [
        ("PUT", "/v1/vpn/settings", {"provider": {"server_selection": {"countries": ["Spain"]}}}),
        ("PUT", "/v1/vpn/status", {"status": "running"}),
    ]


async def test_a_country_the_vpn_does_not_serve_is_refused(gluetun):
    with pytest.raises(ToolError, match="can't go to Atlantis"):
        await make_vpn(gluetun).connect("Atlantis")


async def test_an_exit_elsewhere_stops_the_vpn(gluetun, monkeypatch):
    monkeypatch.setattr(vpn_module, "CONNECT_TIMEOUT_SECONDS", 0.05)
    gluetun.exits["Spain"] = {"country": "France", "city": "Paris"}

    with pytest.raises(ToolError, match="didn't come out in Spain"):
        await make_vpn(gluetun).connect("Spain")

    assert gluetun.status == "stopped"


async def test_disconnect_and_status(gluetun):
    vpn = make_vpn(gluetun)

    assert await vpn.running()
    await vpn.disconnect()
    assert not await vpn.running()


def test_a_country_is_a_plain_english_name():
    assert country_name("  United   States ") == "United States"
    for bad in ("", "es", "Spain; rm", "x" * 60):
        with pytest.raises(ToolError):
            country_name(bad)
