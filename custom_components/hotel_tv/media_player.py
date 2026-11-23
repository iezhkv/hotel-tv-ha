"""Each TV as a media player."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityDescription,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .const import DOMAIN
from .entity import HotelTvDeviceEntity, setup_tv_platform

PARALLEL_UPDATES = 0

TV = MediaPlayerEntityDescription(
    key="tv", translation_key="tv", device_class=MediaPlayerDeviceClass.TV
)


def source_name(channel: dict[str, Any]) -> str:
    """How a channel is listed as a source: "12 · BNT 1"."""
    number = channel.get("number")
    name = str(channel.get("name") or channel.get("id"))
    return f"{number} · {name}" if number is not None else name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a media player for every TV."""
    hub = entry.runtime_data
    setup_tv_platform(
        hass,
        hub,
        async_add_entities,
        lambda tv_id: [HotelTvMediaPlayer(hub, tv_id, TV)],
    )


class HotelTvMediaPlayer(HotelTvDeviceEntity, MediaPlayerEntity):
    """Power, channel and volume of the TV (the main entity of its device)."""

    _attr_name = None
    _attr_media_content_type = MediaType.CHANNEL
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
    )

    @property
    def available(self) -> bool:
        """The TV is online."""
        return super().available and bool(self.tv.get("online"))

    def _channels(self) -> list[dict[str, Any]]:
        return self.hub.tv_channels(self.tv_id)

    @property
    def state(self) -> MediaPlayerState | None:
        """On / off."""
        if not self.tv:
            return None
        return MediaPlayerState.ON if self.tv.get("power") else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        """0..1."""
        volume = self.tv.get("volume")
        if volume is None:
            return None
        return max(0.0, min(1.0, float(volume) / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        """Muted."""
        return bool(self.tv.get("muted")) if self.tv else None

    @property
    def source_list(self) -> list[str]:
        """The enabled channels of the TV's package."""
        return [source_name(c) for c in self._channels()]

    def _current_channel(self) -> dict[str, Any] | None:
        tv = self.tv
        if not tv.get("channel_id"):
            return None
        return self.hub.channel_by_id(tv["channel_id"]) or {
            "id": tv["channel_id"],
            "number": tv.get("channel_number"),
            "name": tv.get("channel_name"),
        }

    @property
    def source(self) -> str | None:
        """The current channel."""
        channel = self._current_channel()
        return source_name(channel) if channel else None

    @property
    def media_channel(self) -> str | None:
        """The current channel's name."""
        channel = self._current_channel()
        return str(channel.get("name")) if channel and channel.get("name") else None

    @property
    def media_title(self) -> str | None:
        """The current channel's name."""
        return self.media_channel

    @property
    def media_content_id(self) -> str | None:
        """The current channel id."""
        channel = self._current_channel()
        return channel.get("id") if channel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Channel number and TV details."""
        tv = self.tv
        return {
            "channel_number": tv.get("channel_number"),
            "channel_name": tv.get("channel_name"),
            "tv_id": self.tv_id,
            "kind": tv.get("kind"),
            "paired": tv.get("paired"),
            "last_seen_at": tv.get("last_seen_at"),
        }

    async def async_turn_on(self) -> None:
        """Power on."""
        await self._async_command("power_on")

    async def async_turn_off(self) -> None:
        """Power off."""
        await self._async_command("power_off")

    async def async_select_source(self, source: str) -> None:
        """Switch channel."""
        for channel in self._channels():
            if source in (
                source_name(channel),
                channel.get("name"),
                str(channel.get("number")),
            ):
                await self._async_command("set_channel", {"channel_id": channel["id"]})
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_source",
            translation_placeholders={"source": source},
        )

    async def async_set_volume_level(self, volume: float) -> None:
        """0..1 → 0..100."""
        await self._async_command("set_volume", {"volume": round(volume * 100)})

    async def async_volume_up(self) -> None:
        """One step up."""
        await self._async_command("volume_up")

    async def async_volume_down(self) -> None:
        """One step down."""
        await self._async_command("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute / unmute."""
        await self._async_command("mute" if mute else "unmute")

    async def async_media_next_track(self) -> None:
        """Channel up."""
        await self._async_command("channel_up")

    async def async_media_previous_track(self) -> None:
        """Channel down."""
        await self._async_command("channel_down")

    async def async_media_play(self) -> None:
        """Play."""
        await self._async_command("play")

    async def async_media_pause(self) -> None:
        """Pause."""
        await self._async_command("pause")
