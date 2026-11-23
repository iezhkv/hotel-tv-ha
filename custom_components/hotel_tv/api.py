"""Client for the Hotel TV Integration API (`/api/v1/*`, docs/CONTRACTS.md).

Kept free of Home Assistant imports (it takes an aiohttp session) so it can move to
a PyPI library later.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from .sse import SseEvent, SseParser

REQUEST_TIMEOUT = 20
CONNECT_TIMEOUT = 15
# A synchronous POST /channels/refresh probes every source: 10–90 s.
REFRESH_TIMEOUT = 150
# POST /ha/pair probes every webhook URL (8 s each) before it answers.
PAIR_TIMEOUT = 60
PAIR_PATH = "/api/v1/ha/pair"


class HotelTvError(Exception):
    """Base error; `code` is the server's error code when it sent one."""

    def __init__(self, code: str = "unknown", message: str | None = None) -> None:
        """Keep the server's error code."""
        super().__init__(message or code)
        self.code = code


class HotelTvConnectionError(HotelTvError):
    """The server could not be reached, timed out or answered garbage."""


class HotelTvAuthError(HotelTvError):
    """The token was refused (401)."""


class HotelTvNotFoundError(HotelTvError):
    """404: the TV / message / request does not exist."""


class HotelTvRequestError(HotelTvError):
    """400 / 422: the server refused the request as invalid."""


def normalize_url(url: str) -> str:
    """The server base URL without a trailing slash (and without /api/v1)."""
    url = url.strip().rstrip("/")
    url = url.removesuffix("/api/v1").rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(url)
    if parts.query or parts.fragment:
        raise ValueError(url)
    return url


def normalize_code(code: str) -> str:
    """Upper-case the pairing code and restore the ABCD-EFGH dash."""
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:]}"
    return code.strip().upper()


async def async_pair(
    session: aiohttp.ClientSession, url: str, code: str, ha: dict[str, Any]
) -> dict[str, Any]:
    """POST /api/v1/ha/pair (unsigned; the code is the credential).

    Returns `{ok, connection_id, secret, token, property: {id, name}}`. Raises
    HotelTvConnectionError when the server cannot be reached, and HotelTvError
    with the server's slug (`invalid_code`, `expired_code`, `rate_limited`,
    `unreachable`, …) when it refuses.
    """
    try:
        async with asyncio.timeout(PAIR_TIMEOUT):
            async with session.post(
                normalize_url(url) + PAIR_PATH,
                data=json.dumps({"code": code, "ha": ha}),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HotelTvConnectionError("cannot_connect", str(err)) from err
    if not isinstance(data, dict):
        if status == 404:
            # No pairing endpoint: not a Hotel TV server (or a too old one).
            raise HotelTvError("not_hotel_tv")
        raise HotelTvConnectionError("bad_response")
    if data.get("ok") is not True:
        slug = data.get("error")
        if isinstance(slug, dict):
            slug = slug.get("code")
        slug = str(slug or f"http_{status}")
        if status >= 500 and slug != "unreachable":
            raise HotelTvConnectionError(slug)
        raise HotelTvError(slug)
    prop = data.get("property")
    if (
        not isinstance(prop, dict)
        or not prop.get("id")
        or not data.get("secret")
        or not data.get("token")
        or not data.get("connection_id")
    ):
        raise HotelTvConnectionError("bad_response")
    return data


def _error_from(status: int, data: Any) -> HotelTvError:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "") or None
    elif isinstance(error, str):  # tolerate a bare string
        code, message = error, None
    else:
        code, message = "", None
    if status == 401:
        return HotelTvAuthError(code or "unauthorized", message)
    if status == 404:
        return HotelTvNotFoundError(code or "not_found", message)
    if status in (400, 409, 413, 422):
        return HotelTvRequestError(code or "bad_request", message)
    if status == 403:
        return HotelTvAuthError(code or "forbidden", message)
    if status >= 500:
        return HotelTvConnectionError(code or "server_error", message)
    return HotelTvError(code or f"http_{status}", message)


class HotelTvClient:
    """Bearer-token client of one Hotel TV property."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        """Initialize."""
        self._session = session
        self.url = normalize_url(url)
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        headers = self._headers
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(request_timeout):
                async with self._session.request(
                    method,
                    f"{self.url}/api/v1{path}",
                    data=None if payload is None else json.dumps(payload),
                    headers=headers,
                ) as resp:
                    status = resp.status
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        data = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HotelTvConnectionError("cannot_connect", str(err)) from err
        if status >= 400:
            raise _error_from(status, data)
        if not isinstance(data, dict):
            raise HotelTvConnectionError("bad_response")
        return data

    # -- reads ------------------------------------------------------------

    async def async_get_info(self) -> dict[str, Any]:
        """GET /info → {property: {id, name}, version, capabilities}."""
        data = await self._request("GET", "/info")
        prop = data.get("property")
        if not isinstance(prop, dict) or not prop.get("id"):
            raise HotelTvError("not_hotel_tv")
        return data

    async def async_get_tvs(self) -> list[dict[str, Any]]:
        """GET /devices → every TV of the property."""
        return _list(await self._request("GET", "/devices"), "devices")

    async def async_get_tv(self, tv_id: str) -> dict[str, Any]:
        """GET /devices/{id}."""
        return await self._request("GET", f"/devices/{_seg(tv_id)}")

    async def async_get_channels(self) -> list[dict[str, Any]]:
        """GET /channels."""
        return _list(await self._request("GET", "/channels"), "channels")

    async def async_get_packages(self) -> list[dict[str, Any]]:
        """GET /packages."""
        return _list(await self._request("GET", "/packages"), "packages")

    # -- TVs ----------------------------------------------------------------

    async def async_update_tv(
        self, tv_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /devices/{id} ({name?, package_id?}) → Device."""
        return await self._request("PATCH", f"/devices/{_seg(tv_id)}", changes)

    async def async_command(
        self, tv_id: str, command: str, payload: dict[str, Any] | None = None
    ) -> None:
        """POST /devices/{id}/command."""
        body: dict[str, Any] = {"type": command}
        if payload:
            body["payload"] = payload
        await self._request("POST", f"/devices/{_seg(tv_id)}/command", body)

    # -- channels & messages -----------------------------------------------

    async def async_refresh_channels(self, *, wait: bool = False) -> dict[str, Any]:
        """POST /channels/refresh.

        By default fire-and-forget (`?wait=false`, 202): completion arrives as a
        `channels.updated` event. With `wait` the call blocks until the probe is done.
        """
        if wait:
            return await self._request(
                "POST", "/channels/refresh", {}, request_timeout=REFRESH_TIMEOUT
            )
        return await self._request("POST", "/channels/refresh?wait=false", {})

    async def async_send_message(self, payload: dict[str, Any]) -> str:
        """POST /messages → message id."""
        data = await self._request("POST", "/messages", payload)
        return str(data.get("id") or "")

    async def async_delete_message(self, message_id: str) -> None:
        """DELETE /messages/{id}."""
        await self._request("DELETE", f"/messages/{_seg(message_id)}")

    # -- TV controls --------------------------------------------------------

    async def async_put_controls(
        self, tv_id: str, controls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """PUT /devices/{id}/controls (full replace)."""
        data = await self._request(
            "PUT", f"/devices/{_seg(tv_id)}/controls", {"controls": controls}
        )
        return _list(data, "controls")

    async def async_patch_control(
        self, tv_id: str, key: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /devices/{id}/controls/{key}."""
        return await self._request(
            "PATCH", f"/devices/{_seg(tv_id)}/controls/{_seg(key)}", changes
        )

    async def async_get_pending_requests(self) -> list[dict[str, Any]]:
        """GET /control-requests?status=pending (oldest first).

        A server without this endpoint (404) yields [] (older API version).
        """
        try:
            data = await self._request("GET", "/control-requests?status=pending")
        except HotelTvNotFoundError:
            return []
        return _list(data, "requests")

    async def async_ack(self, request_id: str | int, status: str) -> None:
        """POST /control-requests/{id}/ack {status: done|failed}."""
        await self._request(
            "POST", f"/control-requests/{_seg(str(request_id))}/ack", {"status": status}
        )

    # -- events ---------------------------------------------------------------

    async def async_events(
        self,
        last_event_id: str | None = None,
        read_timeout: float = 60,
        on_open: Callable[[], None] | None = None,
    ) -> AsyncIterator[SseEvent]:
        """GET /events: yield events until the stream ends or breaks.

        Raises HotelTvAuthError on 401 and HotelTvConnectionError otherwise; a
        stream that ends normally just stops the iteration.
        """
        headers = {**self._headers, "Accept": "text/event-stream"}
        headers["Cache-Control"] = "no-cache"
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        timeout = aiohttp.ClientTimeout(
            total=None, connect=CONNECT_TIMEOUT, sock_read=read_timeout
        )
        try:
            async with self._session.get(
                f"{self.url}/api/v1/events", headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        data = None
                    raise _error_from(resp.status, data)
                if on_open is not None:
                    on_open()
                parser = SseParser()
                async for chunk in resp.content.iter_any():
                    for event in parser.feed(chunk):
                        yield event
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HotelTvConnectionError("stream_broken", str(err)) from err


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = data.get(key)
    if not isinstance(items, list):
        raise HotelTvConnectionError("bad_response")
    return [item for item in items if isinstance(item, dict)]
