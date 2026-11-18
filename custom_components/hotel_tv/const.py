"""Constants for the Hotel TV integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hotel_tv"
MANUFACTURER: Final = "Hotel TV"

# Config entry data.
CONF_PROPERTY_ID: Final = "property_id"
CONF_PROPERTY_NAME: Final = "property_name"

# Options: {room_id: {entity_id: label override or ""}} (dict order = position).
OPT_CONTROLS: Final = "controls"

# HA event fired for every event of the server's feed.
EVENT_HOTEL_TV: Final = "hotel_tv_event"

# Safety-net full refresh while the event stream is up.
REFRESH_INTERVAL: Final = 300
# The server pings every 20 s; no byte for this long means the stream is dead
# (docs/API.md: "nothing, not even a ping, for ~45 s → reconnect").
SSE_READ_TIMEOUT: Final = 45
SSE_BACKOFF_MIN: Final = 1
SSE_BACKOFF_MAX: Final = 30
# Coalesce entity state changes before they are pushed to the server.
CONTROL_DEBOUNCE: Final = 0.5
# A guest request older than this (HA was down, stream replayed) is not executed.
CONTROL_REQUEST_MAX_AGE: Final = 60
SERVICE_CALL_TIMEOUT: Final = 15

# HA domains a guest may control from the TV, and the control kind each becomes.
CONTROL_DOMAINS: Final[dict[str, str]] = {
    "climate": "climate",
    "light": "light",
    "switch": "switch",
    "cover": "cover",
    "scene": "scene",
}

# Commands a TV understands (src/lib/core/commands.ts in the server).
COMMAND_TYPES: Final = (
    "power_on",
    "power_off",
    "set_channel",
    "channel_up",
    "channel_down",
    "set_volume",
    "volume_up",
    "volume_down",
    "mute",
    "unmute",
    "play",
    "pause",
    "show_message",
    "open_panel",
    "reload",
    "identify",
)

MESSAGE_KINDS: Final = ("info", "alert", "welcome", "promo")
MESSAGE_DISPLAYS: Final = ("banner", "modal", "fullscreen")

# Services.
SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_CHECK_IN: Final = "check_in"
SERVICE_CHECK_OUT: Final = "check_out"
SERVICE_SET_GUEST: Final = "set_guest"
SERVICE_SEND_COMMAND: Final = "send_command"
SERVICE_REFRESH_CHANNELS: Final = "refresh_channels"
