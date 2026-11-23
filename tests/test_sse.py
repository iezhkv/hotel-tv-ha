"""The SSE parser and the client's event stream."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.api import (
    HotelTvAuthError,
    HotelTvClient,
    HotelTvConnectionError,
)
from custom_components.hotel_tv.sse import SseEvent, SseParser

from .conftest import API, SERVER, TOKEN


def parse(*chunks: bytes | str) -> list[SseEvent]:
    parser = SseParser()
    out: list[SseEvent] = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    return out


def test_basic_event() -> None:
    """id, event and data of one event."""
    events = parse(b'id: 7\nevent: device.state\ndata: {"a":1}\n\n')
    assert events == [SseEvent(event="device.state", data='{"a":1}', id="7")]


def test_default_type_and_multiline_data() -> None:
    """No event field = message; data lines are joined with LF."""
    assert parse(b"data: one\ndata: two\n\n") == [
        SseEvent(event="message", data="one\ntwo", id="")
    ]


def test_byte_by_byte_and_line_endings() -> None:
    """CRLF and CR line breaks, the stream cut at every byte."""
    raw = b"id: 1\r\nevent: a\r\ndata: x\r\n\r\nid: 2\revent: b\rdata: y\r\r"
    events = parse(*[raw[i : i + 1] for i in range(len(raw))])
    assert [(e.id, e.event, e.data) for e in events] == [
        ("1", "a", "x"),
        ("2", "b", "y"),
    ]


def test_crlf_split_between_chunks_is_one_break() -> None:
    """A CR at the end of a chunk and the LF at the start of the next are one break."""
    events = parse(b"data: x\r", b"\n\r", b"\ndata: y\n\n")
    assert [e.data for e in events] == ["x", "y"]


def test_utf8_split_inside_a_character() -> None:
    """A multi-byte character cut between chunks."""
    raw = "data: Стая 204 — добре дошли\n\n".encode()
    cut = raw.index("т".encode()) + 1
    assert parse(raw[:cut], raw[cut:])[0].data == "Стая 204 — добре дошли"


def test_comments_ping_and_no_data() -> None:
    """Comments are ignored; an event without data is not dispatched."""
    events = parse(b": keep-alive\n\nevent: nothing\n\nevent: ping\ndata: {}\n\n")
    assert [e.event for e in events] == ["ping"]


def test_id_persists_and_null_id_is_ignored() -> None:
    """The last event id carries over; an id with NUL is ignored."""
    parser = SseParser()
    events = parser.feed(b"id: 5\ndata: a\n\ndata: b\n\nid: 6\x00\ndata: c\n\n")
    assert [e.id for e in events] == ["5", "5", "5"]
    assert parser.last_event_id == "5"


def test_field_details() -> None:
    """Only one leading space is stripped; a field without colon has an empty value."""
    events = parse(b"data:  two spaces\ndata\ndata:no space\n\n")
    assert events[0].data == " two spaces\n\nno space"


def test_retry_and_unknown_fields() -> None:
    """retry is kept when numeric; unknown fields are ignored."""
    parser = SseParser()
    events = parser.feed(b"retry: 3000\nfoo: bar\ndata: x\n\nretry: soon\ndata: y\n\n")
    assert events[0].retry == 3000
    assert events[1].retry is None
    assert parser.retry == 3000


def test_bom_and_incomplete_tail() -> None:
    """A leading BOM is dropped; an event not ended by a blank line is not dispatched."""
    events = parse("﻿data: a\n\n".encode(), b"data: unfinished\n")
    assert [e.data for e in events] == ["a"]


async def test_client_stream(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The client sends the token and Last-Event-ID and yields parsed events."""
    body = (
        "event: ping\ndata: {}\n\n"
        'id: 41\nevent: device.online\ndata: {"id": 41, "type": "device.online"}\n\n'
        'id: 42\nevent: device.updated\ndata: {"id": 42, "type": "device.updated"}\n\n'
    )
    aioclient_mock.get(f"{API}/events", text=body)
    client = HotelTvClient(async_get_clientsession(hass), SERVER, TOKEN)
    events = [e async for e in client.async_events("40")]
    assert [(e.event, e.id) for e in events] == [
        ("ping", ""),
        ("device.online", "41"),
        ("device.updated", "42"),
    ]
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Last-Event-ID"] == "40"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "text/event-stream"


async def test_client_stream_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """401 is an auth error, 5xx a connection error."""
    client = HotelTvClient(async_get_clientsession(hass), SERVER, TOKEN)
    aioclient_mock.get(
        f"{API}/events",
        status=401,
        json={"error": {"code": "unauthorized", "message": "bad token"}},
    )
    with pytest.raises(HotelTvAuthError):
        _ = [e async for e in client.async_events()]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/events", status=502, text="bad gateway")
    with pytest.raises(HotelTvConnectionError):
        _ = [e async for e in client.async_events()]
