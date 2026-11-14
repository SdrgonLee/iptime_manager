from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME, CONF_TARGET
from .coordinator import IPTimeChangedEntity

# 요약: ipTIME 공유기 연결 기기 목록을 바탕으로 재실 상태를 관리하는 플랫폼
# 연결될 파일: const.py, coordinator.py, __init__.py

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """기기 추적기 설정. (연결될 파일: const.py, coordinator.py)"""
    from homeassistant.helpers import entity_registry as er

    coordinator = hass.data[DOMAIN][entry.entry_id].tracker
    device_map = entry.data.get("devices", {})
    
    # 옵션에 추적할 기기 리스트(targets)가 있으면 거기서만 필터링하고, 없으면 전체 기기 로드
    tracked_macs = entry.options.get(CONF_TARGET)
    if tracked_macs is None:
        tracked_macs = entry.data.get(CONF_TARGET, list(device_map.keys()))

    # 옵션 변경 등으로 제외된 엔티티가 있으면 엔티티 레지스트리에서 완전히 제거
    tracked_macs_clean = {m.replace(":", "").replace("-", "").lower() for m in tracked_macs}
    entity_registry = er.async_get(hass)
    registered_entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    
    prefix = f"{entry.entry_id}_"
    for entity in registered_entities:
        if entity.domain == "device_tracker" and entity.unique_id.startswith(prefix):
            mac = entity.unique_id[len(prefix):].replace(":", "").replace("-", "").lower()
            if mac not in tracked_macs_clean:
                _LOGGER.info("Removing untracked ipTIME presence sensor entity: %s (MAC: %s)", entity.entity_id, mac)
                entity_registry.async_remove(entity.entity_id)

    _LOGGER.debug("Starting ipTIME Device Tracker: tracking %s of %s devices", len(tracked_macs), len(device_map))
    
    entities = [
        IPTimeDeviceEntity(coordinator, entry, mac, device_map[mac])
        for mac in tracked_macs
        if mac in device_map
    ]
    async_add_entities(entities)

class IPTimeDeviceEntity(IPTimeChangedEntity, ScannerEntity):
    """ipTIME 연결 기기 엔티티."""

    def __init__(self, coordinator, entry, mac: str, name: str) -> None:
        super().__init__(coordinator)
        # 매칭용 MAC: 모든 기호 제거 후 소문자
        self._mac = mac.replace(":", "").replace("-", "").lower()
        self._attr_name = name
        # 고유 ID는 기존 호환성을 위해 원본 MAC 사용
        self._attr_unique_id = f"{entry.entry_id}_{mac}"
        self._entry = entry
        self._last_seen: float | None = None
        self._consider_home = entry.options.get(
            CONF_CONSIDER_HOME, entry.data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME)
        )
        self._connected = False
        self._update_presence()
        _LOGGER.debug("[%s] Entity initialization completed (matching MAC: %s)", name, self._mac)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _update_presence(self) -> None:
        if not self.coordinator.last_update_success:
            return
        now = monotonic()
        device = (self.coordinator.data or {}).get(self._mac, {})
        if device.get("state") == "home":
            self._last_seen = now
            self._connected = True
            return
        self._connected = (
            self._last_seen is not None and now - self._last_seen < self._consider_home
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_presence()
        super()._handle_coordinator_update()

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """추가 속성 제공."""
        device = (self.coordinator.data or {}).get(self._mac, {})
        return {
            "ip": device.get("ip"),
            "band": device.get("band"),
            "connected_to": device.get("connected_to"),
            "connected_to_mac": device.get("connected_to_mac"),
            "connected_to_model": device.get("connected_to_model"),
            "connected_to_role": device.get("connected_to_role"),
            "connected_to_ip": device.get("connected_to_ip"),
            "mesh_connection": device.get("mesh_connection"),
            "mesh_channel": device.get("mesh_channel"),
            "mesh_interface": device.get("mesh_interface"),
        }

    @property
    def icon(self) -> str:
        """연결 상태에 따른 아이콘 리턴 (연결: cellphone-nfc / 해제: cellphone-remove)"""
        return "mdi:cellphone-nfc" if self.is_connected else "mdi:cellphone-remove"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }
