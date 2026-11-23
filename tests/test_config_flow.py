"""Config flow: pairing with a code, reauth (new code), reconfigure."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.hotel_tv.const import DOMAIN

from .conftest import ENTRY_DATA, PROPERTY, SERVER, signed

PAIR = f"{SERVER}/api/v1/ha/pair"
HA_URL = "http://192.168.0.20:8123"
ANSWER = {
    "ok": True,
    "connection_id": "88888888-8888-4888-8888-888888888888",
    "secret": "f" * 64,
    "token": "htv_paired_token",
    "property": {"id": PROPERTY, "name": "Hotel Demo"},
}


@pytest.fixture(autouse=True)
def no_setup():
    """The flow is under test, not the entry setup."""
    with patch(
        "custom_components.hotel_tv.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
async def ha_url(hass: HomeAssistant) -> None:
    """Home Assistant has a LAN URL; the webhook component is up."""
    hass.config.internal_url = HA_URL
    assert await async_setup_component(hass, "webhook", {})


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


def _pair_bodies(aioclient_mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    return [
        json.loads(data)
        for method, url, data, _h in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url) == PAIR
    ]


async def test_pair_ok(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    """URL + code: the server probes the webhook, then the entry is created."""
    client = await hass_client_no_auth()
    probes: list[tuple[int, Any]] = []

    async def _pair(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        body = json.loads(data)
        ha = body["ha"]
        path = ha["webhook_urls"][0].removeprefix(HA_URL)
        token = ha["probe_token"]
        # A ping signed with the probe token is answered with its nonce …
        raw, headers = signed(token, {"type": "ping", "nonce": "n1"})
        resp = await client.post(path, data=raw, headers=headers)
        probes.append((resp.status, await resp.json()))
        # … a wrong signature, or anything but a ping, is not.
        raw, headers = signed("wrong", {"type": "ping", "nonce": "n2"})
        resp = await client.post(path, data=raw, headers=headers)
        probes.append((resp.status, await resp.json()))
        raw, headers = signed(token, {"type": "info"})
        resp = await client.post(path, data=raw, headers=headers)
        probes.append((resp.status, await resp.json()))
        return AiohttpClientMockResponse(method, url, json=ANSWER)

    aioclient_mock.post(PAIR, side_effect=_pair)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": f" {SERVER}/api/v1/ ", "code": "abcd efgh"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hotel Demo"
    data = result["data"]
    assert data["url"] == SERVER
    assert data["token"] == "htv_paired_token"
    assert data["secret"] == "f" * 64
    assert data["connection_id"] == ANSWER["connection_id"]
    assert data["property_id"] == PROPERTY
    assert data["property_name"] == "Hotel Demo"
    assert result["result"].unique_id == PROPERTY

    assert probes == [
        (200, {"ok": True, "nonce": "n1"}),
        (401, {"ok": False, "error": "unauthorized"}),
        (401, {"ok": False, "error": "unauthorized"}),
    ]
    (body,) = _pair_bodies(aioclient_mock)
    assert body["code"] == "ABCD-EFGH"
    ha = body["ha"]
    assert ha["webhook_urls"] == [f"{HA_URL}/api/webhook/{data['webhook_id']}"]
    assert len(ha["probe_token"]) == 64
    assert ha["instance_id"]
    assert ha["version"]
    assert "location_name" in ha

    # The pairing handler is gone once the flow is done (HA answers an unknown
    # webhook with an empty 200).
    raw, headers = signed(ha["probe_token"], {"type": "ping", "nonce": "x"})
    resp = await client.post(
        f"/api/webhook/{data['webhook_id']}", data=raw, headers=headers
    )
    assert (await resp.text()) == ""


async def test_pair_offers_internal_then_external(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Both URLs, the internal one first."""
    hass.config.external_url = "https://ha.hotel.example"
    aioclient_mock.post(PAIR, json=ANSWER)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": SERVER, "code": "ABCD-EFGH"}
    )
    webhook_id = result["data"]["webhook_id"]
    (body,) = _pair_bodies(aioclient_mock)
    assert body["ha"]["webhook_urls"] == [
        f"{HA_URL}/api/webhook/{webhook_id}",
        f"https://ha.hotel.example/api/webhook/{webhook_id}",
    ]


@pytest.mark.parametrize(
    ("mock", "errors"),
    [
        (
            {"status": 404, "json": {"ok": False, "error": "invalid_code"}},
            {"base": "invalid_code"},
        ),
        (
            {"status": 410, "json": {"ok": False, "error": "expired_code"}},
            {"base": "expired_code"},
        ),
        (
            {"status": 429, "json": {"ok": False, "error": "rate_limited"}},
            {"base": "rate_limited"},
        ),
        (
            {"status": 502, "json": {"ok": False, "error": "unreachable"}},
            {"base": "unreachable"},
        ),
        ({"status": 500, "text": "boom"}, {"base": "cannot_connect"}),
        ({"status": 404, "text": "<html>not found</html>"}, {"base": "not_hotel_tv"}),
        ({"json": {"ok": True, "hello": "world"}}, {"base": "cannot_connect"}),
    ],
)
async def test_pair_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """The server's refusals become form errors; nothing is created."""
    aioclient_mock.post(PAIR, **mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": SERVER, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_pair_retry_after_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed attempt can be retried in the same flow (the webhook is free)."""
    aioclient_mock.post(PAIR, status=404, json={"ok": False, "error": "invalid_code"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": SERVER, "code": "AAAA-AAAA"}
    )
    assert result["errors"] == {"base": "invalid_code"}
    aioclient_mock.clear_requests()
    aioclient_mock.post(PAIR, json=ANSWER)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": SERVER, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("url", "errors"),
    [
        ("hoteltv.local", {"url": "invalid_url"}),
        ("http://tv.hotel.example", {"url": "insecure_url"}),
    ],
)
async def test_pair_bad_url(
    hass: HomeAssistant, url: str, errors: dict[str, str]
) -> None:
    """Not a URL; plain http outside the LAN."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": url, "code": "ABCD-EFGH"}
    )
    assert result["errors"] == errors


async def test_pair_without_ha_url(hass: HomeAssistant) -> None:
    """Home Assistant has no URL the server could call."""
    hass.config.internal_url = None
    hass.config.external_url = None
    with patch(
        "custom_components.hotel_tv.wire.get_url",
        side_effect=__import__(
            "homeassistant.helpers.network", fromlist=["NoURLAvailableError"]
        ).NoURLAvailableError,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": SERVER, "code": "ABCD-EFGH"}
        )
    assert result["errors"] == {"base": "no_ha_url"}


async def test_pair_again_updates_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The same property again: the entry takes the new connection."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post("http://10.0.0.5:3210/api/v1/ha/pair", json=ANSWER)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://10.0.0.5:3210", "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://10.0.0.5:3210"
    assert entry.data["token"] == "htv_paired_token"
    assert entry.data["secret"] == "f" * 64
    assert entry.data["webhook_id"] != ENTRY_DATA["webhook_id"]


async def test_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Reauth asks only for a code and pairs at the stored address."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post(PAIR, json=ANSWER)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert list(result["data_schema"].schema) == ["code"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "htv_paired_token"
    assert entry.data["secret"] == "f" * 64


async def test_reauth_legacy_token_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entry made with a typed token (no secret) is completed by reauth."""
    legacy = {
        k: v
        for k, v in ENTRY_DATA.items()
        if k not in ("secret", "connection_id", "webhook_id")
    }
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=legacy)
    entry.add_to_hass(hass)
    aioclient_mock.post(PAIR, json=ANSWER)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD-EFGH"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["secret"] == "f" * 64
    assert entry.data["webhook_id"]


async def test_reauth_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An expired code keeps the form; the entry is unchanged."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post(PAIR, status=410, json={"ok": False, "error": "expired_code"})
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "expired_code"}
    assert entry.data == ENTRY_DATA


async def test_reauth_wrong_property(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A code of another property is refused and not stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post(
        PAIR, json={**ANSWER, "property": {"id": "other", "name": "Other"}}
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_property"
    assert entry.data == ENTRY_DATA


async def test_reauth_other_property_entry_takes_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A code of another configured property re-pairs that property's entry."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other",
        data={**ENTRY_DATA, "property_id": "other", "secret": "old"},
    )
    other.add_to_hass(hass)
    aioclient_mock.post(
        PAIR, json={**ANSWER, "property": {"id": "other", "name": "Other"}}
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD-EFGH"}
    )
    assert result["reason"] == "other_property_updated"
    assert other.data["secret"] == "f" * 64
    assert entry.data == ENTRY_DATA


async def test_reconfigure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A new address of the same property, paired with a new code."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post("https://tv.hotel.example/api/v1/ha/pair", json=ANSWER)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://tv.hotel.example", "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == "https://tv.hotel.example"
    assert entry.data["token"] == "htv_paired_token"


async def test_reconfigure_wrong_property(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Another property's server is refused."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.post(
        "http://other:3210/api/v1/ha/pair",
        json={**ANSWER, "property": {"id": "other-property", "name": "Other"}},
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://other:3210", "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_property"
    assert entry.data["url"] == SERVER
