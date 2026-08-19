"""Tests for EasyMesh per-band Wi-Fi parsing and write payloads."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "iptime_manager"
    / "easymesh_wifi.py"
)
SPEC = importlib.util.spec_from_file_location("iptime_manager_easymesh_wifi", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
EASYMESH_WIFI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EASYMESH_WIFI)


class EasyMeshWifiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "wifi_all": [
                {"type": "main", "enable": True, "ssid": "BBOBBI_WIFI6"}
            ],
            "wifi_band": [
                {
                    "band": "2g",
                    "enable": True,
                    "separated": True,
                    "bss": [
                        {
                            "type": "main",
                            "enable": True,
                            "hide": False,
                            "access": "all",
                            "authenc": "wpa2psk_aes",
                            "ssid": "BBOBBI",
                            "password": "main-secret",
                        },
                        {
                            "type": "guest",
                            "enable": True,
                            "hide": False,
                            "access": "wan",
                            "authenc": "wpa3sae_wpa2psk_aes",
                            "ssid": "BBOBBI_GUEST",
                            "password": "guest-secret",
                        },
                    ],
                },
                {
                    "band": "5g",
                    "enable": True,
                    "separated": False,
                    "bss": [
                        {
                            "type": "main",
                            "enable": True,
                            "ssid": "BBOBBI_WIFI6",
                        }
                    ],
                },
            ],
        }

    def test_lists_only_individually_configured_band_networks(self) -> None:
        web_data = {"easymesh": {"config": self.config}}

        entries = EASYMESH_WIFI.easymesh_band_wifi_list(web_data)

        self.assertEqual(
            [(entry["band"], entry["type"], entry["ssid"]) for entry in entries],
            [
                ("2g", "main", "BBOBBI"),
                ("2g", "guest", "BBOBBI_GUEST"),
            ],
        )

    def test_builds_router_ui_compatible_guest_toggle_payload(self) -> None:
        payload = EASYMESH_WIFI.build_easymesh_band_enable_payload(
            self.config,
            "2g",
            "guest",
            False,
        )

        self.assertIsNotNone(payload)
        band_payload = payload["wifi_band"][0]
        self.assertEqual(band_payload["band"], "2g")
        self.assertTrue(band_payload["enable"])
        self.assertTrue(band_payload["separated"])
        self.assertTrue(band_payload["bss"][0]["enable"])
        self.assertFalse(band_payload["bss"][1]["enable"])
        self.assertEqual(band_payload["bss"][0]["password"], "main-secret")
        self.assertEqual(band_payload["bss"][1]["password"], "guest-secret")

    def test_returns_none_for_an_unknown_band_network(self) -> None:
        self.assertIsNone(
            EASYMESH_WIFI.build_easymesh_band_enable_payload(
                self.config,
                "2g",
                "missing",
                False,
            )
        )

    def test_lists_separated_5g_and_6g_networks(self) -> None:
        config = {
            "wifi_band": [
                {
                    "band": band,
                    "enable": True,
                    "separated": True,
                    "bss": [
                        {
                            "type": "main",
                            "enable": True,
                            "ssid": f"BBOBBI_{band.upper()}",
                        },
                        {
                            "type": "guest",
                            "enable": True,
                            "ssid": f"BBOBBI_{band.upper()}_GUEST",
                        },
                    ],
                }
                for band in ("5g", "5g2", "6g", "6g2")
            ]
        }

        entries = EASYMESH_WIFI.easymesh_band_wifi_list(
            {"easymesh": {"config": config}}
        )

        self.assertEqual(len(entries), 8)
        self.assertEqual(
            {(entry["band"], entry["type"]) for entry in entries},
            {
                (band, network_type)
                for band in ("5g", "5g2", "6g", "6g2")
                for network_type in ("main", "guest")
            },
        )


if __name__ == "__main__":
    unittest.main()
