from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from .coordinator import IPTimeChangedEntity

from .const import DOMAIN, CONF_URL
from .api import format_channel_string

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    web_data = (coordinator.data or {}).get("web", {})
    entities = []
    
    if coordinator.api._beta_ui:
        # 자동 재부팅 설정이 지원되는 모델일 경우에만 생성
        if web_data.get("reboot_timer") is not None:
            entities.append(IPTimeRebootDaySelect(coordinator, entry))
            
        # 무선 채널 제어 셀렉터 동적 생성
        wireless = web_data.get("wireless", {})
        bands = wireless.get("band", [])
        if isinstance(bands, list):
            for b in bands:
                band_id = b.get("band")
                if band_id:
                    entities.append(IPTimeWifiChannelSelect(coordinator, entry, band_id))

    async_add_entities(entities)


# 요약: 자동 재시작 요일을 셀렉트로 제어한다. (연결 파일: api.py)
class IPTimeRebootDaySelect(IPTimeChangedEntity, SelectEntity):
    """Auto Reboot Day selector."""

    _attr_options = ["Everyday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reboot_day"
        self._attr_name = f"Auto Reboot Day ({entry.data.get(CONF_URL)})"
        self._attr_icon = "mdi:calendar-week"
        self._attr_entity_category = EntityCategory.CONFIG
        self._last_valid_hour = None
        self._last_valid_min = None
        self._last_valid_days = None

    @property
    def current_option(self) -> str | None:
        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        timer = web_data.get("reboot_timer", {})
        
        d = timer.get("days")
        if d is not None:
            self._last_valid_days = d
        days = self._last_valid_days if self._last_valid_days is not None else ["Fri"]

        if not isinstance(days, list) or not days:
            return "Friday"

        if len(days) >= 7:
            return "Everyday"

        day_map = {
            "mon": "Monday",
            "tue": "Tuesday",
            "wed": "Wednesday",
            "thu": "Thursday",
            "fri": "Friday",
            "sat": "Saturday",
            "sun": "Sunday"
        }

        first_day = str(days[0]).lower()
        return day_map.get(first_day, "Friday")

    async def async_select_option(self, option: str) -> None:
        day_map = {
            "Everyday": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "Monday": ["Mon"],
            "Tuesday": ["Tue"],
            "Wednesday": ["Wed"],
            "Thursday": ["Thu"],
            "Friday": ["Fri"],
            "Saturday": ["Sat"],
            "Sunday": ["Sun"]
        }
        target_days = day_map.get(option, ["Fri"])
        self._last_valid_days = target_days

        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        timer = web_data.get("reboot_timer", {})
        run = timer.get("run", False)
        
        h = timer.get("hour")
        if h is not None:
            self._last_valid_hour = h
        hour = self._last_valid_hour if self._last_valid_hour is not None else 4
        
        m = timer.get("min")
        if m is not None:
            self._last_valid_min = m
        min_time = self._last_valid_min if self._last_valid_min is not None else 0

        await self.coordinator.api.async_set_web_reboot_timer(run, hour, min_time, target_days)
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> dict[str, Any]:
        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        model = web_data.get("model", "ipTIME Router")
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": model,
            "manufacturer": "EFM Networks",
            "model": model,
        }


# 요약: 무선 채널 제어 셀렉터 (연결될 파일: api.py)
class IPTimeWifiChannelSelect(IPTimeChangedEntity, SelectEntity):
    """Wi-Fi Channel selector."""

    def __init__(self, coordinator, entry, band: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._band = band
        self._attr_unique_id = f"{entry.entry_id}_wifi_channel_{band}"
        
        # 대역 친화적 명칭 정규화
        band_label = {
            "2g": "2.4GHz",
            "5g": "5GHz",
            "5g-2": "5GHz-2",
            "6g": "6GHz",
            "6g-2": "6GHz-2",
            "mlo": "MLO",
        }.get(str(band).lower(), str(band).upper())

        self._attr_name = f"Wi-Fi Channel {band_label} ({entry.data.get(CONF_URL)})"
        self._attr_icon = "mdi:wifi-cog"
        self._attr_entity_category = EntityCategory.CONFIG
        self._cached_options = ["auto"]
        self._next_options_update = 0.0

    @property
    def current_option(self) -> str | None:
        """현재 설정된 채널 반환."""
        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        wireless = web_data.get("wireless", {})
        bands = wireless.get("band", [])
        if isinstance(bands, list):
            for b in bands:
                if str(b.get("band")).lower() == str(self._band).lower():
                    ch = b.get("channel")
                    if ch in (None, "", "0", "0.0"):
                        return "auto"
                    return format_channel_string(ch)
        return "auto"

    @property
    def options(self) -> list[str]:
        """사용 가능한 채널 목록 반환 (API 부하 최소화를 위한 1시간 캐싱)."""
        now = monotonic()
        if now >= self._next_options_update:
            self._next_options_update = now + 60
            self.hass.async_create_task(self._async_update_options())
            
        return self._cached_options

    async def _async_update_options(self) -> None:
        """비동기식으로 채널 목록을 수집하여 캐시 갱신."""
        try:
            channels = await self.coordinator.api.async_get_wireless_channel_list(self._band)
            if len(channels) > 1:
                self._next_options_update = monotonic() + 3600
            if channels and channels != self._cached_options:
                self._cached_options = channels
                self._handle_coordinator_update()
        except Exception as err:
            _LOGGER.debug(f"Failed to update channel list for band {self._band}: {err}")

    async def async_select_option(self, option: str) -> None:
        """사용자가 채널 선택 시 공유기 채널 변경 실행."""
        channel_to_set = option.split(" ")[0] if option != "auto" else "auto"
        success = await self.coordinator.api.async_set_wireless_channel(self._band, channel_to_set)
        if success:
            await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> dict[str, Any]:
        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        model = web_data.get("model", "ipTIME Router")
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": model,
            "manufacturer": "EFM Networks",
            "model": model,
        }
