"""Config flow: URL + token, reauth, reconfigure."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.const import DOMAIN

from .conftest import API, ENTRY_DATA, INFO, PROPERTY, SERVER, TOKEN

OTHER_INFO = {**INFO, "property": {"id": "other-property", "name": "Other"}}


@pytest.fixture(autouse=True)
def no_setup():
    """The flow is under test, not the entry setup."""
    with patch(
        "custom_components.hotel_tv.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_ok(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A valid URL (with /api/v1 and a trailing slash) and token create the entry."""
    aioclient_mock.get(f"{API}/info", json=INFO)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": f" {SERVER}/api/v1/ ", "token": f"{TOKEN} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hotel Demo"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == PROPERTY
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("mock", "errors"),
    [
        (
            {
                "status": 401,
                "json": {"error": {"code": "unauthorized", "message": "x"}},
            },
            {"token": "invalid_auth"},
        ),
        ({"status": 500, "text": "boom"}, {"base": "cannot_connect"}),
        ({"status": 404, "text": "<html>not found</html>"}, {"base": "not_hotel_tv"}),
        ({"json": {"hello": "world"}}, {"base": "not_hotel_tv"}),
    ],
)
async def test_user_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Refused token, broken server, not a Hotel TV server."""
    aioclient_mock.get(f"{API}/info", **mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": SERVER, "token": TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def test_user_invalid_url(hass: HomeAssistant) -> None:
    """Not a URL."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "hoteltv.local", "token": TOKEN}
    )
    assert result["errors"] == {"url": "invalid_url"}


async def test_user_already_configured_updates(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The same property again: abort, but take the new URL and token."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get("http://10.0.0.5:3210/api/v1/info", json=INFO)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://10.0.0.5:3210", "token": "htv_new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://10.0.0.5:3210"
    assert entry.data["token"] == "htv_new"


async def test_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A new token for the same property is stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{API}/info", json=INFO)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "htv_fresh"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "htv_fresh"


async def test_reauth_wrong_property(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token of another property is refused and not stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{API}/info", json=OTHER_INFO)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "htv_other"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_property"
    assert entry.data["token"] == TOKEN


async def test_reconfigure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A new address of the same property; the token is kept when left empty."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get("https://tv.hotel.example/api/v1/info", json=INFO)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://tv.hotel.example"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == "https://tv.hotel.example"
    assert entry.data["token"] == TOKEN
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {TOKEN}"


async def test_reconfigure_wrong_property(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Another property's server is refused."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get("http://other:3210/api/v1/info", json=OTHER_INFO)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://other:3210", "token": "htv_x"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_property"
    assert entry.data["url"] == SERVER
