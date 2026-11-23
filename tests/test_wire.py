"""The signed webhook: signing, server messages, TV messages, synced entities."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.bridge import merge_mapping
from custom_components.hotel_tv.signing import (
    PATH_WEBHOOK,
    ReplayGuard,
    compute_signature,
    parse_signature_header,
    signature_header,
    tv_key,
    verify_signature,
)

from .conftest import API, SECRET, TV101, TV102, WEBHOOK_ID, calls, signed, tv

URL = f"/api/webhook/{WEBHOOK_ID}"
OPTIONS = {"controls": {TV101: {"light.r101": "Лампа"}, TV102: {"switch.r102": ""}}}


# -- signing (pure) ------------------------------------------------------------


def test_tv_key() -> None:
    """hex(HMAC-SHA256(secret, "hotel_tv/tv/" + device_id))."""
    expected = hmac.new(
        b"s3cret", b"hotel_tv/tv/" + TV101.encode(), hashlib.sha256
    ).hexdigest()
    assert tv_key("s3cret", TV101) == expected
    assert tv_key("s3cret", TV102) != expected
    assert tv_key("other", TV101) != expected


def test_signature_roundtrip() -> None:
    """The base is t.id.METHOD.PATH.body; tolerance ±300 s; any change fails."""
    body = b'{"type":"ping"}'
    header = signature_header("k", 1000, "id-1", "post", PATH_WEBHOOK, body)
    expected = hmac.new(
        b"k", b"1000.id-1.POST./hotel_tv/webhook." + body, hashlib.sha256
    ).hexdigest()
    assert header == f"t=1000,v1={expected}"
    assert compute_signature("k", 1000, "id-1", "POST", PATH_WEBHOOK, body) == expected
    assert parse_signature_header(header) == (1000, [expected])

    def verify(**kw: Any) -> int | None:
        args = {
            "key": "k",
            "header": header,
            "message_id": "id-1",
            "method": "POST",
            "path": PATH_WEBHOOK,
            "body": body,
            "now": 1000.0,
        }
        args.update(kw)
        return verify_signature(**args)

    assert verify() == 1000
    assert verify(now=1300) == 1000
    assert verify(now=700) == 1000
    assert verify(now=1301) is None
    assert verify(now=699) is None
    assert verify(key="x") is None
    assert verify(body=body + b" ") is None
    assert verify(message_id="id-2") is None
    assert verify(message_id="") is None
    assert verify(path="/api/webhook/x") is None
    assert verify(header=None) is None
    assert verify(header="garbage") is None
    assert verify(header="t=1000") is None
    assert verify(header=f"t=1000,v1=00,v1={expected}") == 1000
    assert verify(key="") is None


def test_replay_guard() -> None:
    """An id is refused while its signature could still be valid."""
    guard = ReplayGuard(300, max_entries=3)
    assert guard.check_and_remember("a", 1000, 1000)
    assert not guard.check_and_remember("a", 1000, 1200)
    # After t + tolerance the signature itself has expired; the id is forgotten.
    assert guard.check_and_remember("a", 1400, 1401)
    for i in range(10):
        assert guard.check_and_remember(f"x{i}", 2000, 2000 + i)
    assert len(guard) <= 4


def test_merge_mapping() -> None:
    """Options first (with labels), then the server's bindings; no sensors."""
    chosen = {TV101: {"light.a": "Lamp"}, "gone": {"switch.z": ""}}
    tvs = {
        TV101: {"ha_entities": ["switch.b", "light.a", "sensor.t", 5]},
        TV102: {"ha_entities": ["climate.c"]},
        "other": {"ha_entities": None},
        "empty": {"ha_entities": ["weather.home"]},
    }
    assert merge_mapping(chosen, tvs) == {
        TV101: {"light.a": "Lamp", "switch.b": ""},
        "gone": {"switch.z": ""},
        TV102: {"climate.c": ""},
    }


# -- the webhook ----------------------------------------------------------------


@pytest.fixture
def states(hass: HomeAssistant) -> None:
    """Entities of the hotel, some readable, some not."""
    hass.states.async_set(
        "light.r101",
        "on",
        {"friendly_name": "Ceiling 101", "supported_color_modes": ["onoff"]},
    )
    hass.states.async_set("switch.r102", "off", {"friendly_name": "Fan 102"})
    hass.states.async_set("switch.bound", "on", {"friendly_name": "Bound switch"})
    hass.states.async_set("sensor.t101", "21.5", {"friendly_name": "Temp 101"})
    hass.states.async_set("person.guest", "home", {"friendly_name": "Guest"})


@pytest.fixture
async def post(hass: HomeAssistant, hass_client_no_auth: Any) -> Callable[..., Any]:
    """POST a signed message to the webhook: (status, json).

    The client is made on first use: the entry sets up `http` (via `webhook`).
    """
    clients: list[Any] = []

    async def _post(
        message: dict[str, Any] | bytes,
        *,
        key: str = SECRET,
        device: str | None = None,
        **kw: Any,
    ) -> tuple[int, Any]:
        if not clients:
            clients.append(await hass_client_no_auth())
        body, headers = signed(key, message, device=device, **kw)
        resp = await clients[0].post(URL, data=body, headers=headers)
        return resp.status, await resp.json()

    return _post


async def test_ping_and_signature(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any
) -> None:
    """Only messages signed with the secret, in time and once, are answered."""
    await setup_entry()
    assert await post({"type": "ping", "nonce": "abc"}) == (
        200,
        {"ok": True, "nonce": "abc"},
    )
    assert await post({"type": "ping", "nonce": "x"}, key="wrong") == (
        401,
        {"ok": False, "error": "unauthorized"},
    )
    old = int(time.time()) - 301
    assert (await post({"type": "ping"}, t=old))[0] == 401
    assert await post({"type": "ping", "nonce": "1"}, message_id="same") == (
        200,
        {"ok": True, "nonce": "1"},
    )
    assert await post({"type": "ping", "nonce": "1"}, message_id="same") == (
        200,
        {"ok": False, "error": "replay"},
    )
    assert await post(b"[1, 2]") == (400, {"ok": False, "error": "bad_request"})
    assert await post({"type": "nope"}) == (
        200,
        {"ok": False, "error": "unknown_type"},
    )


async def test_info(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """Version, location and the number of readable entities."""
    hass.config.location_name = "Hotel Demo HA"
    await setup_entry()
    status, answer = await post({"type": "info"})
    assert status == 200
    readable = [
        s.entity_id
        for s in hass.states.async_all()
        if s.domain
        in ("light", "switch", "sensor", "binary_sensor", "button", "media_player")
    ]
    assert answer["ok"] is True
    assert answer["version"]
    assert answer["location_name"] == "Hotel Demo HA"
    # The integration's own TVs (media_player, sensor, binary_sensor, button)
    # count too; person.guest does not.
    assert answer["entities"] == len(readable)


async def test_entities_and_areas(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """Entities of the readable domains, with friendly name and area."""
    await setup_entry()
    areas = ar.async_get(hass)
    room = areas.async_create("Room 101")
    lobby = areas.async_create("Lobby")
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "dev1")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=lobby.id)
    ents = er.async_get(hass)
    lamp = ents.async_get_or_create(
        "light",
        "test",
        "l1",
        suggested_object_id="area_lamp",
        device_id=device.id,
        original_name="Lamp",
    )
    ents.async_update_entity(lamp.entity_id, area_id=room.id)
    plug = ents.async_get_or_create(
        "switch", "test", "s1", suggested_object_id="area_plug", device_id=device.id
    )
    hass.states.async_set(lamp.entity_id, "on", {"friendly_name": "Room lamp"})
    hass.states.async_set(plug.entity_id, "off", {"friendly_name": "Lobby plug"})

    status, answer = await post({"type": "entities", "domains": ["light", "switch"]})
    assert status == 200
    by_id = {e["entity_id"]: e for e in answer["entities"]}
    assert set(by_id) == {
        "light.r101",
        "switch.r102",
        "switch.bound",
        lamp.entity_id,
        plug.entity_id,
    }
    assert by_id["light.r101"] == {
        "entity_id": "light.r101",
        "state": "on",
        "attributes": {
            "friendly_name": "Ceiling 101",
            "supported_color_modes": ["onoff"],
        },
        "name": "Ceiling 101",
        "area": None,  # not in the registry
    }
    assert by_id[lamp.entity_id]["area"] == "Room 101"  # its own area wins
    assert by_id[lamp.entity_id]["name"] == "Room lamp"
    assert by_id[plug.entity_id]["area"] == "Lobby"  # from its device

    # Default domains: no person; not readable domains asked for are ignored.
    _, answer = await post({"type": "entities"})
    ids = {e["entity_id"] for e in answer["entities"]}
    assert "sensor.t101" in ids
    assert "person.guest" not in ids
    _, answer = await post({"type": "entities", "domains": ["person"]})
    assert answer == {"ok": True, "entities": []}
    _, answer = await post({"type": "entities", "domains": "light"})
    assert answer == {"ok": False, "error": "bad_request"}


async def test_states(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """States by id; unknown and unreadable ids are null; at most 200."""
    await setup_entry()
    _, answer = await post(
        {
            "type": "states",
            "entity_ids": ["sensor.t101", "light.missing", "person.guest"],
        }
    )
    assert answer == {
        "ok": True,
        "states": {
            "sensor.t101": {
                "state": "21.5",
                "attributes": {"friendly_name": "Temp 101"},
            },
            "light.missing": None,
            "person.guest": None,
        },
    }
    _, answer = await post(
        {"type": "states", "entity_ids": [f"light.x{i}" for i in range(201)]}
    )
    assert answer == {"ok": False, "error": "too_many"}
    _, answer = await post({"type": "states"})
    assert answer == {"ok": False, "error": "bad_request"}


async def test_call(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """Service calls on entities of the call's domain, allowed domains only."""
    await setup_entry()
    turn_on = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "homeassistant", "restart")
    async_mock_service(hass, "switch", "turn_on")

    _, answer = await post(
        {
            "type": "call",
            "domain": "light",
            "service": "turn_on",
            "data": {"entity_id": "light.r101", "brightness": 128},
        }
    )
    assert answer == {"ok": True}
    assert turn_on[-1].data == {"entity_id": ["light.r101"], "brightness": 128}

    async def call(domain: str, service: str, data: Any) -> Any:
        _, answer = await post(
            {"type": "call", "domain": domain, "service": service, "data": data}
        )
        return answer

    forbidden = {"ok": False, "error": "forbidden"}
    bad = {"ok": False, "error": "bad_request"}
    assert await call("homeassistant", "restart", {"entity_id": "light.r101"}) == (
        forbidden
    )
    assert await call(
        "light", "turn_on", {"entity_id": ["light.r101", "switch.r102"]}
    ) == (forbidden)
    assert await call("light", "turn_on", {}) == bad
    assert await call("light", "turn_on", {"entity_id": []}) == bad
    assert await call("light", "turn_on", {"entity_id": "all"}) == bad
    assert (
        await call("light", "turn_on", {"entity_id": "light.r101", "area_id": "lobby"})
        == bad
    )
    assert await call("light", "turn_on", "light.r101") == bad
    assert await call("light", "explode", {"entity_id": "light.r101"}) == {
        "ok": False,
        "error": "unknown_service",
    }
    assert len(turn_on) == 1


async def test_call_failure(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """A failing service answers `failed`."""
    from homeassistant.exceptions import HomeAssistantError

    await setup_entry()

    async def _boom(call: Any) -> None:
        raise HomeAssistantError("device offline")

    hass.services.async_register("switch", "turn_off", _boom)
    _, answer = await post(
        {
            "type": "call",
            "domain": "switch",
            "service": "turn_off",
            "data": {"entity_id": "switch.r102"},
        }
    )
    assert answer == {"ok": False, "error": "failed"}


# -- TV → HA ----------------------------------------------------------------------


async def test_tv_controls_and_control(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """A TV, signed with its own key, reads and controls its own entities only."""
    await setup_entry(options=OPTIONS)
    key101 = tv_key(SECRET, TV101)
    status, answer = await post({"type": "tv.controls"}, key=key101, device=TV101)
    assert status == 200
    assert answer == {
        "ok": True,
        "controls": [
            {
                "key": "light.r101",
                "kind": "light",
                "label": "Лампа",
                "state": {"on": True, "brightness": 100},
                "capabilities": {"dimmable": False},
                "available": True,
            }
        ],
    }

    turn_off = async_mock_service(hass, "light", "turn_off")

    async def _off(call: Any) -> None:
        hass.states.async_set(
            "light.r101", "off", hass.states.get("light.r101").attributes
        )

    hass.services.async_register("light", "turn_off", _off)
    _, answer = await post(
        {"type": "tv.control", "key": "light.r101", "action": "turn_off", "params": {}},
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": True, "state": {"on": False, "brightness": 0}}
    assert turn_off == []  # replaced by _off

    # Another TV's entity, an invalid action, a bad body.
    _, answer = await post(
        {"type": "tv.control", "key": "switch.r102", "action": "turn_on"},
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": False, "error": "forbidden"}
    _, answer = await post(
        {
            "type": "tv.control",
            "key": "light.r101",
            "action": "set_brightness",
            "params": {"brightness": 50},
        },
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": False, "error": "invalid_request"}
    _, answer = await post({"type": "tv.control"}, key=key101, device=TV101)
    assert answer == {"ok": False, "error": "bad_request"}

    # A TV cannot send server messages.
    _, answer = await post(
        {
            "type": "call",
            "domain": "light",
            "service": "turn_on",
            "data": {"entity_id": "light.r101"},
        },
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": False, "error": "unknown_type"}


async def test_tv_authentication(
    hass: HomeAssistant, setup_entry: Callable[..., Any], post: Any, states: None
) -> None:
    """Only the TV's derived key, for a paired TV the server lists."""
    unpaired = tv(TV102)
    unpaired["paired"] = False
    await setup_entry(options=OPTIONS, tvs=[tv(TV101), unpaired])
    message = {"type": "tv.controls"}
    # The secret itself, or another TV's key, is not this TV's key.
    assert (await post(message, key=SECRET, device=TV101))[0] == 401
    assert (await post(message, key=tv_key(SECRET, TV102), device=TV101))[0] == 401
    # A TV the server does not know, or has not paired.
    stranger = "22222222-2222-4222-8222-222222222999"
    assert await post(message, key=tv_key(SECRET, stranger), device=stranger) == (
        401,
        {"ok": False, "error": "unknown_device"},
    )
    assert await post(message, key=tv_key(SECRET, TV102), device=TV102) == (
        401,
        {"ok": False, "error": "unknown_device"},
    )
    # A TV key without the device header is not the secret.
    assert (await post({"type": "ping"}, key=tv_key(SECRET, TV101)))[0] == 401


# -- synced entities: options ∪ server bindings ------------------------------------


async def test_server_bound_entities_are_synced(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    post: Any,
    states: None,
) -> None:
    """`ha_entities` of a TV join its controls (named by HA) and its TV access."""
    bound = tv(TV101)
    bound["ha_entities"] = ["switch.bound", "light.r101", "sensor.t101"]
    entry = await setup_entry(options=OPTIONS, tvs=[bound, tv(TV102)])
    await hass.async_block_till_done()
    (put,) = calls(aioclient_mock, "put", f"{API}/devices/{TV101}/controls")
    assert [(c["key"], c["label"]) for c in put["controls"]] == [
        ("light.r101", "Лампа"),
        ("switch.bound", "Bound switch"),
    ]

    key101 = tv_key(SECRET, TV101)
    async_mock_service(hass, "switch", "turn_off")
    _, answer = await post(
        {"type": "tv.control", "key": "switch.bound", "action": "turn_off"},
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": True, "state": {"on": True}}
    # A bound sensor is not a control.
    _, answer = await post(
        {"type": "tv.control", "key": "sensor.t101", "action": "turn_off"},
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": False, "error": "forbidden"}

    # The admin unbinds it: device.updated → the TV is re-read and re-sent.
    aioclient_mock.clear_requests()
    fresh = tv(TV101)
    fresh["ha_entities"] = []
    aioclient_mock.get(f"{API}/devices/{TV101}", json=fresh)
    aioclient_mock.put(f"{API}/devices/{TV101}/controls", json={"controls": []})
    await entry.runtime_data.async_handle_event(
        {"type": "device.updated", "device_id": TV101, "payload": {}}
    )
    await hass.async_block_till_done()
    (put,) = calls(aioclient_mock, "put", f"{API}/devices/{TV101}/controls")
    assert [c["key"] for c in put["controls"]] == ["light.r101"]
    _, answer = await post(
        {"type": "tv.control", "key": "switch.bound", "action": "turn_off"},
        key=key101,
        device=TV101,
    )
    assert answer == {"ok": False, "error": "forbidden"}


async def test_bound_entities_serve_control_requests(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    states: None,
) -> None:
    """A `control.requested` for a server-bound entity is carried out."""
    bound = tv(TV102)
    bound["ha_entities"] = ["switch.bound"]
    entry = await setup_entry(tvs=[tv(TV101), bound])
    aioclient_mock.post(f"{API}/control-requests/7/ack", json={"ok": True})
    turn_off = async_mock_service(hass, "switch", "turn_off")
    await entry.runtime_data.bridge.async_handle_request(
        {
            "type": "control.requested",
            "device_id": TV102,
            "payload": {"request_id": 7, "key": "switch.bound", "action": "turn_off"},
        }
    )
    await hass.async_block_till_done()
    assert len(turn_off) == 1
    assert calls(aioclient_mock, "post", f"{API}/control-requests/7/ack") == [
        {"status": "done"}
    ]


async def test_webhook_unregistered_on_unload(
    hass: HomeAssistant, setup_entry: Callable[..., Any], hass_client_no_auth: Any
) -> None:
    """After unload the webhook answers nothing."""
    entry = await setup_entry()
    assert await hass.config_entries.async_unload(entry.entry_id)
    client = await hass_client_no_auth()
    body, headers = signed(SECRET, {"type": "ping", "nonce": "n"})
    resp = await client.post(URL, data=body, headers=headers)
    assert (await resp.text()) == ""
