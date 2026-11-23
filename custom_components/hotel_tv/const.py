"""Constants for the Hotel TV integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hotel_tv"
MANUFACTURER: Final = "Hotel TV"

# Config entry data.
CONF_PROPERTY_ID: Final = "property_id"
CONF_PROPERTY_NAME: Final = "property_name"
# From pairing (docs/HA-PAIRING.md §1): the shared secret of the signed wire, the
# server's connection id and the webhook id the server posts to. The `htv_` API
# token (CONF_TOKEN) and the server address (CONF_URL) are stored as well.
CONF_SECRET: Final = "secret"
CONF_CONNECTION_ID: Final = "connection_id"
CONF_WEBHOOK_ID: Final = "webhook_id"
# Config flow field: the pairing code from Настройки → Home Assistant.
CONF_CODE: Final = "code"

# The signed wire (docs/HA-PAIRING.md §2–§4).
HEADER_SIGNATURE: Final = "X-HotelTV-Signature"
HEADER_ID: Final = "X-HotelTV-Id"
HEADER_DEVICE: Final = "X-HotelTV-Device"
SIGNATURE_TOLERANCE: Final = 300
MAX_WEBHOOK_BODY: Final = 256 * 1024
# `states`: at most this many entity ids per message.
MAX_STATES: Final = 200
# Domains the server may read (`info`, `entities`, `states`).
READ_DOMAINS: Final = (
    "climate",
    "light",
    "switch",
    "cover",
    "scene",
    "script",
    "fan",
    "lock",
    "media_player",
    "sensor",
    "binary_sensor",
    "weather",
    "input_boolean",
    "button",
)
# Domains the server may call services of (`call`).
CALL_DOMAINS: Final = (
    "climate",
    "light",
    "switch",
    "cover",
    "scene",
    "script",
    "fan",
    "lock",
    "media_player",
    "input_boolean",
    "button",
)

# Options: {tv_id: {entity_id: label override or ""}} (dict order = position).
# `tv_id` is the Hotel TV device id (the TV the entities are shown on).
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
    "fan": "fan",
    "lock": "lock",
    "media_player": "media",
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
SERVICE_SEND_COMMAND: Final = "send_command"
SERVICE_REFRESH_CHANNELS: Final = "refresh_channels"
