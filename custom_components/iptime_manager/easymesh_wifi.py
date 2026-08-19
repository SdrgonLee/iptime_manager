"""Helpers for ipTIME EasyMesh Wi-Fi configuration payloads."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_WIFI_FIELDS = ("type", "enable", "hide", "access", "authenc", "ssid", "password")
_BAND_FIELDS = ("band", "enable", "separated")


def easymesh_config_from_web(web_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the sanitized EasyMesh configuration stored by the coordinator."""
    mesh = web_data.get("easymesh", {}) if isinstance(web_data, dict) else {}
    config = mesh.get("config", {}) if isinstance(mesh, dict) else {}
    return config if isinstance(config, dict) else {}


def easymesh_controller_wifi_list(web_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return controller-wide EasyMesh networks such as the shared main SSID."""
    raw_wifi = easymesh_config_from_web(web_data).get("wifi_all", [])
    if not isinstance(raw_wifi, list):
        return []
    return [
        item
        for item in raw_wifi
        if isinstance(item, dict) and item.get("type") is not None and item.get("ssid")
    ]


def easymesh_band_wifi_list(web_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten individually configured band SSIDs from ``wifi_band``.

    ipTIME exposes per-band main and guest SSIDs below each band's ``bss``
    array.  A band whose ``separated`` flag is false follows ``wifi_all`` and
    therefore must not create duplicate Home Assistant switches.
    """
    raw_bands = easymesh_config_from_web(web_data).get("wifi_band", [])
    if not isinstance(raw_bands, list):
        return []

    result: List[Dict[str, Any]] = []
    for band_info in raw_bands:
        if not isinstance(band_info, dict) or not band_info.get("band"):
            continue
        if band_info.get("separated") is False:
            continue
        raw_bss = band_info.get("bss", [])
        if not isinstance(raw_bss, list):
            continue
        for wifi_info in raw_bss:
            if not isinstance(wifi_info, dict):
                continue
            if wifi_info.get("type") is None or not wifi_info.get("ssid"):
                continue
            item = dict(wifi_info)
            item["band"] = str(band_info.get("band"))
            item["band_enable"] = band_info.get("enable")
            item["separated"] = band_info.get("separated")
            result.append(item)
    return result


def easymesh_band_wifi_key(band: Any, network_type: Any) -> str:
    """Return the stable identity for one per-band EasyMesh SSID."""
    return f"{str(band)}:{str(network_type)}"


def build_easymesh_band_enable_payload(
    config: Dict[str, Any],
    band: str,
    network_type: str,
    enable: bool,
) -> Optional[Dict[str, Any]]:
    """Build the same partial ``wifi_band`` payload used by ipTIME's UI.

    Passwords remain transient in this function: the caller fetches the raw
    configuration, forwards the complete selected band back to the router,
    and never stores or logs the returned payload.
    """
    raw_bands = config.get("wifi_band") if isinstance(config, dict) else None
    if not isinstance(raw_bands, list):
        return None

    for raw_band in raw_bands:
        if not isinstance(raw_band, dict) or str(raw_band.get("band")) != str(band):
            continue
        raw_bss = raw_band.get("bss")
        if not isinstance(raw_bss, list):
            return None

        bss_payload: List[Dict[str, Any]] = []
        target_found = False
        for raw_item in raw_bss:
            if not isinstance(raw_item, dict):
                continue
            item = {field: raw_item.get(field) for field in _WIFI_FIELDS if field in raw_item}
            if str(raw_item.get("type")) == str(network_type):
                item["enable"] = bool(enable)
                target_found = True
            bss_payload.append(item)

        if not target_found:
            return None

        band_payload = {
            field: raw_band.get(field)
            for field in _BAND_FIELDS
            if field in raw_band
        }
        band_payload.setdefault("band", band)
        band_payload["bss"] = bss_payload
        return {"wifi_band": [band_payload]}

    return None
