"""Regression coverage for tracker polling and Home Assistant state writes."""

from asyncio import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import homeassistant
except ModuleNotFoundError:
    raise unittest.SkipTest("Home Assistant is required for these entity tests") from None

from custom_components.iptime_manager.api import IPTimeAPI
from custom_components.iptime_manager.coordinator import IPTimeDataUpdateCoordinator
from custom_components.iptime_manager.device_tracker import IPTimeDeviceEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed


class TrackerUpdatesTest(unittest.TestCase):
    def test_only_writes_changes_and_expires_presence_after_successful_poll(self):
        mac = "aabbccddeeff"
        coordinator = SimpleNamespace(
            last_update_success=True,
            data={mac: {"state": "home", "connected_to_mac": "one", "rssi": -50}},
        )
        entry = SimpleNamespace(entry_id="router", options={"consider_home": 10}, data={})

        with patch("custom_components.iptime_manager.device_tracker.monotonic") as clock:
            clock.return_value = 0
            tracker = IPTimeDeviceEntity(coordinator, entry, mac, "Phone")
            tracker._last_state = tracker._state_snapshot()
            with patch.object(CoordinatorEntity, "_handle_coordinator_update") as write:
                clock.return_value = 5
                coordinator.data[mac]["rssi"] = -55
                tracker._handle_coordinator_update()
                write.assert_not_called()
                self.assertNotIn("rssi", tracker.extra_state_attributes)

                coordinator.data[mac]["connected_to_mac"] = "two"
                tracker._handle_coordinator_update()
                self.assertEqual(write.call_count, 1)

                coordinator.last_update_success = False
                clock.return_value = 11
                tracker._handle_coordinator_update()
                self.assertTrue(tracker.is_connected)
                self.assertEqual(write.call_count, 2)

                coordinator.last_update_success = True
                coordinator.data = {}
                tracker._handle_coordinator_update()
                self.assertTrue(tracker.is_connected)
                self.assertEqual(write.call_count, 3)

                clock.return_value = 15
                tracker._handle_coordinator_update()
                tracker._handle_coordinator_update()
                self.assertFalse(tracker.is_connected)
                self.assertEqual(write.call_count, 4)


class MeshStationResponseTest(unittest.IsolatedAsyncioTestCase):
    async def test_beta_session_update_runs_at_most_once_per_30_seconds(self):
        api = IPTimeAPI(None, "http://router", "user", "password")
        api._beta_ui = True
        api.efm_session_id = "session"
        clock = SimpleNamespace(monotonic=Mock(side_effect=[100, 103, 130]))
        with (
            patch.object(api, "beta_ui_wlan_check", return_value={"session": True}),
            patch.object(api, "session_update_beta_ui") as keepalive,
            patch("custom_components.iptime_manager.api.time", clock),
        ):
            for _ in range(3):
                self.assertTrue(await api.async_update())
        self.assertEqual(keepalive.await_count, 2)

    async def test_status_failure_and_same_data_recovery(self):
        data = {"web": {"ports": []}}
        api = SimpleNamespace(
            async_get_web_data=AsyncMock(side_effect=[False, True]),
            web_result=data["web"],
        )
        coordinator = SimpleNamespace(
            _poll_lock=Lock(), api=api, data=data, _firmware_update_notified=False,
        )
        with self.assertRaises(UpdateFailed):
            await IPTimeDataUpdateCoordinator._async_update_data(coordinator)
        self.assertIs(await IPTimeDataUpdateCoordinator._async_update_data(coordinator), data)

    async def test_empty_station_list_is_distinct_from_invalid_response(self):
        api = IPTimeAPI(None, "http://router", "user", "password")
        with patch.object(api, "_async_request", new_callable=AsyncMock) as request:
            request.return_value = '{"station":[]}'
            self.assertEqual(await api.get_mesh_station(), {})

            request.return_value = '{"error":"not a topology response"}'
            self.assertIsNone(await api.get_mesh_station())

    async def test_wireless_band_info_is_only_used_as_fallback(self):
        band = {"band": "2g", "channel": 1}
        for show_result in ([band], [], {"unexpected": True}):
            with self.subTest(show_result=show_result):
                api = IPTimeAPI(None, "http://router", "user", "password")
                api._beta_ui = True
                api._cached_model = "T5008SE"

                async def service(method):
                    return {"result": {
                        "port/link/status": [],
                        "firmware/info": {"version": "1.2.0"},
                        "firmware/version/latest": {"version": "1.2.3"},
                        "wireless/band/show": show_result,
                        "wireless/band/info": [band],
                    }.get(method)}

                with patch.object(api, "_async_service_json", side_effect=service) as request:
                    self.assertTrue(await api.async_get_web_data())
                    self.assertTrue(await api.async_get_web_data())
                methods = [call.args[0] for call in request.await_args_list]
                self.assertNotIn("wireless/info", methods)
                self.assertEqual("wireless/band/info" in methods, show_result != [band])
                self.assertEqual(api.web_result["wireless"]["band"], [band])
                self.assertEqual(methods.count("firmware/info"), 1)
                self.assertEqual(methods.count("firmware/version/latest"), 1)
                self.assertEqual(api.web_result["firmware"], {"version": "1.2.0", "latest_version": "1.2.3"})
