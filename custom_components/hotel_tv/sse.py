"""Incremental Server-Sent Events parser (WHATWG HTML §9.2 "event stream interpretation").

Free of Home Assistant and aiohttp imports: feed it the raw bytes of a response as
they arrive, in chunks of any size, and it returns the events completed so far.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched event."""

    event: str
    data: str
    # The stream's last event id at dispatch time (the `id:` of this or an
    # earlier event), "" when none was ever set.
    id: str
    retry: int | None = None


class SseParser:
    """Turns a byte stream into `SseEvent`s."""

    def __init__(self) -> None:
        """Start a new stream."""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._first = True
        self._pending_cr = False
        self._data: list[str] = []
        self._event = ""
        self._retry: int | None = None
        self.last_event_id = ""
        # The last `retry:` value seen (ms), for the caller's reconnect delay.
        self.retry: int | None = None

    def feed(self, chunk: bytes | str) -> list[SseEvent]:
        """Consume a chunk; return the events it completed."""
        text = chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        if self._first and text:
            self._first = False
            text = text.removeprefix("﻿")
        if self._pending_cr:
            # A CR ended the previous chunk: a LF right after it is the same break.
            self._pending_cr = False
            text = text.removeprefix("\n")
        self._buffer += text
        events: list[SseEvent] = []
        while True:
            line, found = self._next_line()
            if not found:
                break
            event = self._process_line(line)
            if event is not None:
                events.append(event)
        return events

    def _next_line(self) -> tuple[str, bool]:
        buf = self._buffer
        cr = buf.find("\r")
        lf = buf.find("\n")
        if cr == -1 and lf == -1:
            return "", False
        if cr == -1 or (lf != -1 and lf < cr):
            self._buffer = buf[lf + 1 :]
            return buf[:lf], True
        # A CR: CRLF counts as one break; a CR at the very end may be half of one.
        if cr + 1 < len(buf):
            skip = 2 if buf[cr + 1] == "\n" else 1
            self._buffer = buf[cr + skip :]
        else:
            self._buffer = ""
            self._pending_cr = True
        return buf[:cr], True

    def _process_line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment
        field, sep, value = line.partition(":")
        if sep:
            value = value.removeprefix(" ")
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            if "\0" not in value:
                self.last_event_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            self._retry = int(value)
            self.retry = self._retry
        return None

    def _dispatch(self) -> SseEvent | None:
        data, event, retry = self._data, self._event, self._retry
        self._data, self._event, self._retry = [], "", None
        if not data:
            return None
        return SseEvent(
            event=event or "message",
            data="\n".join(data),
            id=self.last_event_id,
            retry=retry,
        )
