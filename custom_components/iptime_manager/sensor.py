from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from .coordinator import IPTimeChangedEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_URL

# 요약: Web 데이터를 통합하여 시스템 정보 및 네트워크 통계를 제공하는 센서 플랫폼
# 연결될 파일: coordinator.py, const.py, api.py

_LOGGER = logging.getLogger(__name__)

SENSOR_TYPES: Dict[str, SensorEntityDescription] = {
    "uptime": SensorEntityDescription(
        key="uptime",
        name="Uptime",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "version": SensorEntityDescription(
        key="version",
        name="Firmware Version",
        icon="mdi:information-outline",
    ),
    "latest_version": SensorEntityDescription(
        key="latest_version",
        name="Latest Firmware Version",
        icon="mdi:update",
    ),
    "wan_ip": SensorEntityDescription(
        key="wan_ip",
        name="WAN IP Address",
        icon="mdi:ip-network",
    ),
    "easymesh_agent_count": SensorEntityDescription(
        key="easymesh_agent_count",
        name="EasyMesh Agent Count",
        icon="mdi:access-point-network",
    ),
}


def _web_sensor_value(web_data: Dict[str, Any], key: str) -> Any:
    firmware = web_data.get("firmware", {}) if isinstance(web_data, dict) else {}
    wan = web_data.get("wan", {}) if isinstance(web_data, dict) else {}

    if key == "uptime":
        return web_data.get("uptime") if isinstance(web_data, dict) else None
    if key == "version":
        return firmware.get("version")
    if key == "latest_version":
        return firmware.get("latest_version")
    if key == "wan_ip":
        return wan.get("ip")
    if key == "easymesh_agent_count":
        mesh = web_data.get("easymesh", {}) if isinstance(web_data, dict) else {}
        agents = mesh.get("agents", {}) if isinstance(mesh, dict) else {}
        if isinstance(agents, dict):
            agents = agents.get("agent", [])
        return len(agents) if isinstance(agents, list) else 0
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """센서 설정."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for key in SENSOR_TYPES:
        entities.append(IPTimeSystemSensor(coordinator, entry, SENSOR_TYPES[key]))

    async_add_entities(entities)


class IPTimeSystemSensor(IPTimeChangedEntity, SensorEntity):
    """공유기 시스템 정보 센서 (Uptime, Model, Version)."""

    def __init__(self, coordinator, entry, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_name = f"{description.name} ({entry.data.get(CONF_URL)})"
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        # Summary: Place router identity and connection metadata in the device information section.
        # Related files: coordinator.py, api.py, select.py.
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
    @property
    def native_value(self) -> Any:
        """통합 데이터에서 시스템 정보 추출 (Web 전용)."""
        if not self.coordinator.data:
            return None

        web_data = self.coordinator.data.get("web", {})
        return _web_sensor_value(web_data, self.entity_description.key)

    @property
    def device_info(self) -> dict[str, Any]:
        web_data = self.coordinator.data.get("web", {}) if self.coordinator.data else {}
        model = web_data.get("model", "ipTIME Router")
        version = web_data.get("firmware", {}).get("version", "Unknown")

        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": model,
            "manufacturer": "EFM Networks",
            "model": model,
            "sw_version": version,
        }
