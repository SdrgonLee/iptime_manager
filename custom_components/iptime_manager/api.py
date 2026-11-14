from __future__ import annotations

import logging
import time
import re
import html
import aiohttp
from typing import Any, Dict, List, Optional, Final
from json import loads
from .const import *
from .easymesh_wifi import build_easymesh_band_enable_payload

# 요약: ipTIME 공유기와의 통신(Web CGI) 담당 API 클래스
# 연결될 파일: const.py, coordinator.py

_LOGGER = logging.getLogger(__name__)

_EASYMESH_SECRET_KEYS = frozenset({"password", "passwd", "passphrase", "key", "psk"})


def _sanitize_easymesh_config(value: Any) -> Any:
    """Return EasyMesh data without Wi-Fi credentials."""
    if isinstance(value, dict):
        return {
            key: _sanitize_easymesh_config(child)
            for key, child in value.items()
            if str(key).lower() not in _EASYMESH_SECRET_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_easymesh_config(child) for child in value]
    return value


def _normalize_model_name(raw_model: Any) -> str:
    text = str(raw_model or "").strip()
    if not text:
        return "ipTIME Router"

    text = re.sub(r"\s+", " ", text)
    if text.lower().startswith("iptime"):
        remainder = text[6:].strip().upper()
        return f"ipTIME {remainder}".strip() if remainder else "ipTIME Router"
    return f"ipTIME {text.upper()}"


def _extract_table_rows(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, flags=re.IGNORECASE | re.DOTALL)


def _extract_table_cells(row_html: str) -> List[str]:
    if not row_html:
        return []
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
    return [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells]

def format_channel_string(channel_str: Any) -> str:
    if channel_str in (None, "", "0", "0.0", "auto", "Auto", "AUTO"):
        return "auto"
    s = str(channel_str).lower().strip()
    if "." not in s:
        return s
    parts = s.split(".")
    if len(parts) != 2:
        return parts[0]
    try:
        x = int(parts[0])
        y = int(parts[1])
    except ValueError:
        return parts[0]
    diff = abs(x - y)
    if 1 <= x <= 14:
        return f"{x} (40MHz)" if diff == 4 else str(x)
    if x >= 36:
        if diff == 4: return f"{x} (40MHz)"
        if y in [42, 58, 106, 122, 138, 155, 171]: return f"{x} (80MHz)"
        if y in [50, 114, 163, 15, 47, 79, 111, 143, 175, 207]: return f"{x} (160MHz)"
        return f"{x} (Bonded)"
    return str(x)

class IPTimeAPI:
    """ipTIME 공유기 API (누락 함수 복구 버전)"""
    
    def __init__(self, hass, url: str, user_id: str, user_pw: str) -> None:
        self._hass = hass
        self._user_id = user_id
        self._user_pw = user_pw
        self._ismobile = False
        self._ismesh = False
        self._beta_ui = False
        self.result: Dict[str, Any] = {}
        self.web_result: Dict[str, Any] = {}
        self._latest_firmware_version: Any = None
        self._latest_firmware_checked_at = 0.0
        self._next_firmware_poll = 0.0
        self._next_session_update = 0.0
        self._last_caching_time = 0.0
        self._cached_model = None

        self._url = url if "http" in url else "http://" + url
        if self._url.endswith("/"):
            self._url = self._url[:-1]
            
        self._host = self._url.split("//")[-1].split(":")[0]
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"{self._url}/",
            "Origin": self._url,
            "Connection": "keep-alive"
        }
        self.efm_session_id: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _async_get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # HA 공용 세션 대신 전용 독립 세션 생성 (세션 기반 공유기 대응)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIME_OUT),
                headers=self.headers
            )
        return self._session

    async def _async_request(self, method: str, url: str, **kwargs) -> str:
        try:
            session = await self._async_get_session()
            headers = kwargs.get("headers", self.headers.copy())
            if self.efm_session_id:
                headers["Cookie"] = f"efm_session_id={self.efm_session_id}"
            
            kwargs["headers"] = headers
            async with session.request(method, url, **kwargs) as response:
                return await response.text()
        except Exception as err:
            _LOGGER.debug(f"요청 실패 ({url}): {err}")
            return ""

    async def _async_service_json(self, method: str, params: Any = None) -> Dict[str, Any]:
        """beta UI JSON-RPC 호출 (자동 재로그인 복구 탑재)."""
        url = f"{self._url}{BETA_SERVICE_URN}"
        payload: Dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params

        async def _execute_call():
            headers = self.headers.copy()
            if self.efm_session_id:
                headers["Cookie"] = f"efm_session_id={self.efm_session_id}"
            
            session = await self._async_get_session()
            async with session.post(url, json=payload, headers=headers) as response:
                try:
                    return await response.json()
                except Exception:
                    text = await response.text()
                    try:
                        return loads(text)
                    except Exception:
                        return {}

        try:
            res = await _execute_call()
            # 만약 세션 만료 에러가 감지되거나 빈 응답인 경우 즉시 재로그인 시도
            is_unauthorized = False
            if res.get("error") is not None:
                err_code = res.get("error", {}).get("code")
                err_msg = str(res.get("error", {}).get("message", "")).lower()
                if err_code in [-32000, -32602] or "session" in err_msg or "login" in err_msg or "auth" in err_msg:
                    is_unauthorized = True
            
            if is_unauthorized or not res:
                _LOGGER.debug(f"세션 만료 감지 또는 빈 응답으로 재로그인 실행: {method}")
                self.efm_session_id = None
                if await self.login_beta_ui():
                    # 새 세션으로 즉시 1회 재시도!
                    res = await _execute_call()
            return res
        except Exception as err:
            _LOGGER.debug(f"서비스 호출 실패 ({method}): {err}")
            return {}

    async def async_update(self, rssi_limit: int = DEFAULT_RSSI_LIMIT) -> bool:
        """데이터 업데이트 메인 로직 (안정화)."""
        # 1. 세션이 없으면 로그인 시도
        if not self.efm_session_id:
            if self._beta_ui or await self.verify_beta_ui():
                self._beta_ui = True
                if not await self.login_beta_ui(): 
                    _LOGGER.debug("베타 UI 로그인 실패")
                    return False
                await self.beta_ui_check_mesh()
            else:
                await self.verify_mobile()
                if self._ismobile:
                    if not await self.m_login(): return False
                    await self.m_check_mesh()
                else:
                    if not await self.login(): return False
                    await self.check_mesh()

        # 2. 데이터 수집
        if self._beta_ui:
            result = await self.beta_ui_wlan_check()
        elif self._ismobile:
            result = await self.m_wlan_check()
        else:
            result = await self.wlan_check()

        if result.get("session") is False:
            _LOGGER.debug("세션 만료 감지, 다음 주기에서 재로그인 시도")
            self.efm_session_id = None
            return False

        if self._beta_ui:
            now = time.monotonic()
            if now >= self._next_session_update:
                # Avoid sending a keepalive for every client scan.
                self._next_session_update = now + 30
                await self.session_update_beta_ui()

        if self._ismesh:
            mesh_stations = await self.get_mesh_station(rssi_limit=rssi_limit)
            if mesh_stations is None:
                return False
            result.update(mesh_stations)

        self.result = result
        return True

    async def async_get_web_data(self) -> bool:
        """웹 UI에서 노출되는 시스템/포트 정보를 수집한다. (연결될 파일: coordinator.py)"""
        # 요약: 스마트 캐싱 엔진을 적용하여 실시간 데이터는 5초마다 수집하고, 정적 설정 데이터는 5분 캐시 처리한다.
        # 연결 파일: coordinator.py
        if self.web_result is None:
            self.web_result = {}

        if not self._beta_ui:
            return bool(self.efm_session_id)

        try:
            ports = await self._async_service_json("port/link/status")
            ports_data = ports.get("result")
            if isinstance(ports_data, list):
                self.web_result["ports"] = ports_data
            elif "ports" in self.web_result:
                # A previously supported endpoint can detect a failed refresh.
                return False

            now = time.time()
            # 5분(300초) 단위 캐싱 조건 성립 여부 판단
            use_cache = (now - self._last_caching_time) < 300.0

            poll_time = time.monotonic()
            if poll_time >= self._next_firmware_poll:
                self._next_firmware_poll = poll_time + 300
                await self._async_refresh_firmware(poll_time)

            # 2. 공유기 모델명 수집 (평생 캐시 처리: CGI 파싱 부하 최소화)
            if self._cached_model is not None:
                self.web_result["model"] = self._cached_model
            else:
                product_html = await self._async_request("GET", f"{self._url}/ui/port_setup")
                product_match = re.search(r'PRODUCT:\s*"([^"]+)"', product_html)
                model_name = _normalize_model_name(product_match.group(1) if product_match else None)
                self._cached_model = model_name
                self.web_result["model"] = model_name

            # 3. 자주 변경되지 않는 설정값 캐싱 처리
            if not use_cache or "lan" not in self.web_result or "wan" not in self.web_result:
                _LOGGER.debug("정적 설정 데이터 캐시 만료 또는 최초 수집: 공유기 쿼리 수행 시작")
                
                lan = await self._async_service_json("network/interface/lan/info")
                if lan.get("result"):
                    self.web_result["lan"] = lan["result"]

                wan = await self._async_service_json("network/interface/wan1/info")
                if wan.get("result"):
                    self.web_result["wan"] = wan["result"]

                geoip = await self._async_service_json("geoip/get")
                geoip_data = geoip.get("result")
                if isinstance(geoip_data, dict) and geoip_data:
                    self.web_result["geoip"] = {"enable": geoip_data.get("enable", False)}

                accesslist = await self._async_service_json("acl/config")
                if accesslist.get("result"):
                    self.web_result["acl"] = accesslist["result"]

                dos_config = await self._async_service_json("dos/config")
                if dos_config.get("result"):
                    self.web_result["security_dos"] = dos_config["result"]

                reboot_timer = await self._async_service_json("reboot/timer")
                if reboot_timer.get("result") is not None:
                    self.web_result["reboot_timer"] = reboot_timer["result"]

                self._last_caching_time = now

            # 4. 실시간 수집 데이터 (유선 포트, 무선, EasyMesh 상태)
            wireless_band_show = await self._async_service_json("wireless/band/show")
            band_data = wireless_band_show.get("result")
            if not isinstance(band_data, list) or not band_data:
                wireless_band_info = await self._async_service_json("wireless/band/info")
                band_data = wireless_band_info.get("result")
            if not isinstance(band_data, list):
                band_data = []
            wireless_bss = await self._async_service_json("wireless/bss/show")

            easymesh_info = await self._async_service_json("easymesh/info")
            easymesh_config = await self._async_service_json("easymesh/config")
            easymesh_agents = await self._async_service_json("easymesh/show/agent")
            if (
                easymesh_info.get("result") is not None
                or easymesh_config.get("result") is not None
                or easymesh_agents.get("result") is not None
            ):
                self.web_result["easymesh"] = {
                    "info": easymesh_info.get("result", {}),
                    # Coordinator/entity state must never retain Wi-Fi credentials.
                    "config": _sanitize_easymesh_config(easymesh_config.get("result", {})),
                    "agents": easymesh_agents.get("result", {}),
                }
            
            if band_data or wireless_bss.get("result") is not None:
                self.web_result["wireless"] = {
                    "band": band_data,
                    "bss": wireless_bss.get("result", []),
                }

            uptime = None
            for candidate in (self.web_result.get("wan", {}), self.web_result.get("lan", {})):
                if isinstance(candidate, dict) and candidate.get("connected_period") is not None:
                    uptime = candidate.get("connected_period")
                    break
            self.web_result["uptime"] = uptime
            return (
                isinstance(ports_data, list)
                or bool(band_data)
                or isinstance(wireless_bss.get("result"), list)
                or any(
                    isinstance(response.get("result"), (dict, list))
                    for response in (easymesh_info, easymesh_config, easymesh_agents)
                )
            )
        except Exception as err:
            _LOGGER.debug(f"웹 데이터 수집 실패: {err}")
            return False

    async def _async_refresh_firmware(self, now: float) -> None:
        firmware = (await self._async_service_json("firmware/info")).get("result")
        firmware_block = self.web_result.setdefault("firmware", {})
        current = firmware.get("version") if isinstance(firmware, dict) else None
        if isinstance(current, str) and current:
            firmware_block["version"] = current

        if self._latest_firmware_version is not None and now - self._latest_firmware_checked_at < 86400:
            return

        latest = (await self._async_service_json("firmware/version/latest")).get("result")
        if isinstance(latest, dict):
            latest = next(
                (latest[key] for key in ("version", "latest", "value", "result", "ok")
                 if isinstance(latest.get(key), str) and latest[key]),
                None,
            )
        if isinstance(latest, str) and latest:
            self._latest_firmware_version = latest
            self._latest_firmware_checked_at = now
            firmware_block["latest_version"] = latest

    async def async_set_web_wireless_bss_enable(self, bss: str, enable: bool) -> bool:
        """Toggle a wireless BSS using the beta UI."""
        if not self._beta_ui:
            return False

        response = await self._async_service_json(
            "wireless/bss/set",
            {"bss": bss, "enable": bool(enable), "commit": True},
        )
        if response.get("error"):
            _LOGGER.warning(f"무선 BSS 제어 실패: {response.get('error')}")
            return False
        self._last_caching_time = 0.0 # 캐시 강제 만료
        return bool(response)

    async def async_set_web_easymesh_wifi_enable(self, network_type: str, enable: bool) -> bool:
        """Toggle one Controller-managed EasyMesh SSID.

        ipTIME's Flutter UI reads the complete EasyMesh configuration and sends
        the full ``wifi_all`` array back when one SSID changes. Fetching it here
        keeps credentials transient: passwords are forwarded to the router but
        are never stored in coordinator/entity state or written to logs.
        """
        if not self._beta_ui:
            return False

        current_response = await self._async_service_json("easymesh/config")
        current = current_response.get("result")
        if not isinstance(current, dict):
            _LOGGER.warning("EasyMesh SSID 제어 실패: 현재 설정을 불러올 수 없습니다")
            return False

        raw_wifi = current.get("wifi_all")
        if not isinstance(raw_wifi, list):
            _LOGGER.warning("EasyMesh SSID 제어 실패: wifi_all 목록이 없습니다")
            return False

        wifi_payload: List[Dict[str, Any]] = []
        target_found = False
        supported_fields = ("type", "enable", "hide", "access", "authenc", "ssid", "password")

        for raw_item in raw_wifi:
            if not isinstance(raw_item, dict):
                continue
            item = {field: raw_item.get(field) for field in supported_fields if field in raw_item}
            if str(raw_item.get("type")) == str(network_type):
                item["enable"] = bool(enable)
                target_found = True
            wifi_payload.append(item)

        if not target_found:
            _LOGGER.warning("EasyMesh SSID 제어 실패: 네트워크 유형 %s을 찾을 수 없습니다", network_type)
            return False

        global_config = current.get("global")
        global_enable = (
            bool(global_config.get("enable"))
            if isinstance(global_config, dict) and global_config.get("enable") is not None
            else True
        )
        params = {
            "global": {"enable": global_enable},
            "wifi_all": wifi_payload,
        }
        response = await self._async_service_json("easymesh/config", params)
        if response.get("error"):
            error = _sanitize_easymesh_config(response.get("error"))
            _LOGGER.warning("EasyMesh SSID 제어 실패: %s", error)
            return False

        self._last_caching_time = 0.0
        return bool(response)

    async def async_set_web_easymesh_band_wifi_enable(
        self,
        band: str,
        network_type: str,
        enable: bool,
    ) -> bool:
        """Toggle one individually configured EasyMesh band SSID."""
        if not self._beta_ui:
            return False

        current_response = await self._async_service_json("easymesh/config")
        current = current_response.get("result")
        if not isinstance(current, dict):
            _LOGGER.warning("EasyMesh band SSID control failed: current config is unavailable")
            return False

        params = build_easymesh_band_enable_payload(
            current,
            band,
            network_type,
            enable,
        )
        if params is None:
            _LOGGER.warning(
                "EasyMesh band SSID control failed: network %s:%s was not found",
                band,
                network_type,
            )
            return False

        response = await self._async_service_json("easymesh/config", params)
        if response.get("error"):
            error = _sanitize_easymesh_config(response.get("error"))
            _LOGGER.warning("EasyMesh band SSID control failed: %s", error)
            return False

        self._last_caching_time = 0.0
        return bool(response)

    async def async_set_web_wireless_band_enable(self, band: str, enable: bool, separated: Any = None, bss: Any = None) -> bool:
        """Toggle a wireless band using the beta UI (최적화 버전)."""
        if not self._beta_ui:
            return False

        # 단순 On/Off 시에는 separated와 bss를 보내지 않는 것이 안전함 (전체 재시작 방지)
        params: Dict[str, Any] = {"band": band, "enable": bool(enable), "commit": True}
        if separated is not None and str(separated).lower() != "none":
            params["separated"] = separated
        if bss is not None and str(bss).lower() != "none":
            params["bss"] = bss

        _LOGGER.debug(f"무선 band 제어 요청: {params}")
        response = await self._async_service_json("wireless/band/set", params)
        if response.get("error"):
            _LOGGER.warning(f"무선 band 제어 실패: {response.get('error')}")
            return False
        self._last_caching_time = 0.0 # 캐시 강제 만료
        return bool(response)

    async def async_set_web_reboot_timer(self, run: bool, hour: int = 4, min_time: int = 0, days: List[str] = ["Fri"]) -> bool:
        # 자동 재시작 설정 (연결될 파일: switch.py)
        if not self._beta_ui:
            return False
        params = {
            "run": bool(run),
            "hour": int(hour),
            "min": int(min_time),
            "days": days
        }
        response = await self._async_service_json("reboot/timer", params)
        if not response.get("error"):
            self._last_caching_time = 0.0 # 캐시 강제 만료
            return True
        return False

    async def login(self) -> bool:
        """로그인 시도 (구형 및 신형 주소 모두 대응)"""
        for urn in [LOGIN_NEW_URN, LOGIN_URN]:
            url = f"{self._url}{urn}"
            data = {"username": self._user_id, "passwd": self._user_pw}
            if urn == LOGIN_NEW_URN:
                data = {"tmenu": "main", "smenu": "main", "username": self._user_id, "passwd": self._user_pw}
                
            try:
                session = await self._async_get_session()
                async with session.post(url, data=data, allow_redirects=True) as response:
                    text = await response.text()
                    # 세션 ID(16자리 문자열) 추출
                    ids = re.findall(re.compile(r"\w{16}"), text)
                    if ids:
                        self.efm_session_id = ids[0]
                        _LOGGER.debug("공유기 로그인 성공")
                        return True
            except Exception as err:
                _LOGGER.debug("공유기 로그인 요청 실패 (%s)", type(err).__name__)
        return False

    async def m_login(self) -> bool:
        url = f"{self._url}{M_LOGIN_URN}"
        data = {"username": self._user_id, "passwd": self._user_pw}
        try:
            session = await self._async_get_session()
            async with session.post(url, data=data) as response:
                text = await response.text()
                ids = re.findall(re.compile(r"\w{16}"), text)
                if ids:
                    self.efm_session_id = ids[0]
                    return True
        except Exception: pass
        return False

    async def session_update_beta_ui(self) -> None:
        """베타 UI 세션 업데이트 (연장)"""
        url = f"{self._url}{BETA_SERVICE_URN}"
        data = {"method":"session/update"}
        headers = self.headers.copy()
        if self.efm_session_id:
            headers["Cookie"] = f"efm_session_id={self.efm_session_id}"
            
        try:
            session = await self._async_get_session()
            async with session.post(url, json=data, headers=headers) as response:
                await response.text()
        except Exception: pass

    async def login_beta_ui(self) -> bool:
        """베타 UI 로그인 및 세션 쿠키 추출 (최종 강화 버전)."""
        url = f"{self._url}{BETA_SERVICE_URN}"
        data = {"method":"session/login", "params":{"id": self._user_id, "pw": self._user_pw}}
        
        # 세션 초기화 (깨끗한 상태에서 로그인 시도)
        if self._session:
            await self._session.close()
            self._session = None
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": self._url,
            "Referer": f"{self._url}/",
            "Connection": "keep-alive"
        }
        
        try:
            session = await self._async_get_session()
            async with session.post(url, json=data, headers=headers) as response:
                res_json = await response.json()
                
                if res_json and res_json.get('result') == "done":
                    # 쿠키 추출 수동화 및 자동화 병합 최종 강화 (연결될 파일: select.py, switch.py)
                    session_id = None
                    set_cookie_headers = response.headers.getall("Set-Cookie", [])
                    for cookie_str in set_cookie_headers:
                        if "efm_session_id=" in cookie_str:
                            parts = cookie_str.split(";")
                            for part in parts:
                                if "efm_session_id=" in part:
                                    session_id = part.split("=")[-1].strip()
                                    break
                    
                    if not session_id:
                        cookie_obj = response.cookies.get('efm_session_id')
                        if cookie_obj:
                            session_id = cookie_obj.value
                            
                    if session_id:
                        self.efm_session_id = session_id
                        self._next_session_update = 0.0
                        _LOGGER.debug("베타 UI 로그인 성공")
                    else:
                        _LOGGER.debug("쿠키가 없으나 결과가 done이므로 계속 진행")
                        # 쿠키가 없어도 일단 진행 (이후 요청에서 401 뜨면 그때 세션 파기)
                    
                    return True
                else:
                    _LOGGER.warning("베타 UI 로그인 거부됨 (HTTP %s)", response.status)
        except Exception as err:
            _LOGGER.warning("베타 UI 로그인 요청 실패 (%s)", type(err).__name__)
        return False

    async def verify_beta_ui(self) -> bool:
        """베타 UI 여부 판별 (파일 존재 여부 기반)."""
        urn = BETA_UI_URN if BETA_UI_URN.startswith("/") else f"/{BETA_UI_URN}"
        url = f"{self._url}{urn}flutter_bootstrap.js"
        try:
            session = await self._async_get_session()
            async with session.get(url, timeout=3) as response:
                if response.status == 200:
                    _LOGGER.debug("베타 UI(Flutter) 기기 확인됨")
                    return True
        except Exception: pass
        return False

    async def verify_mobile(self) -> bool:
        text = await self._async_request("GET", f"{self._url}{HOSTINFO_URN}")
        self._ismobile = "iux" in text if text else False
        return True

    async def check_mesh(self) -> bool:
        text = await self._async_request("GET", f"{self._url}{MESH_URN}")
        mode_match = re.search(r'<input\b[^>]*id=["\']mode_none["\'][^>]*>', text or "", flags=re.IGNORECASE | re.DOTALL)
        self._ismesh = bool(mode_match and "checked" not in mode_match.group(0).lower())
        return self._ismesh

    async def m_check_mesh(self) -> bool:
        text = await self._async_request("GET", f"{self._url}{M_MESH_URN}")
        try:
            res_json = loads(text)
            self._ismesh = "easymesh" in res_json
            return self._ismesh
        except Exception: return False

    async def beta_ui_check_mesh(self) -> bool:
        url = f"{self._url}{BETA_SERVICE_URN}"
        data = {"method":"easymesh/info"}
        headers = self.headers.copy()
        if self.efm_session_id:
            headers["Cookie"] = f"efm_session_id={self.efm_session_id}"
            
        try:
            session = await self._async_get_session()
            async with session.post(url, json=data, headers=headers) as response:
                res_json = await response.json()
                if res_json and res_json.get('result'):
                    self._ismesh = res_json['result'].get('active', False)
        except Exception: pass
        return self._ismesh

    async def wlan_check(self) -> Dict:
        result_dict = {"session": True}
        for band, urn in [("2.4GHz", WLAN_2G_URN), ("5GHz", WLAN_5G_URN)]:
            text = await self._async_request("GET", self._url + urn)
            if not text:
                return {"session": False}
            result_dict.update(self.device_parsing(_extract_table_rows(text), band))
        return result_dict

    async def m_wlan_check(self) -> Dict:
        result_dict = {"session": True}
        valid_band = False
        for band, urn in [("2.4GHz", M_WLAN_2G_URN), ("5GHz", M_WLAN_5G_URN)]:
            text = await self._async_request("GET", self._url + urn)
            try:
                data = loads(text)
                if isinstance(data, dict) and isinstance(data.get("stalist"), list):
                    result_dict.update(self.json_parsing(data, band))
                    valid_band = True
            except (ValueError, TypeError, AttributeError):
                continue
        return result_dict if valid_band else {"session": False}

    async def beta_ui_wlan_check(self) -> Dict:
        """베타 UI 기기 목록 조회 (수동 쿠키 주입)."""
        url = f"{self._url}{BETA_SERVICE_URN}"
        data = {"method":"network/interface/lan/stations"}
        headers = self.headers.copy()
        if self.efm_session_id:
            headers["Cookie"] = f"efm_session_id={self.efm_session_id}"
            
        try:
            session = await self._async_get_session()
            async with session.post(url, json=data, headers=headers) as response:
                res_json = await response.json()
                if response.ok and isinstance(res_json, dict) and isinstance(res_json.get("result"), list):
                    res = self.beta_ui_device_parsing(res_json['result'])
                    res["session"] = True
                    return res
        except Exception as err:
            _LOGGER.debug(f"베타 UI 기기 조회 실패: {err}")
        return {"session": False}

    def beta_ui_device_parsing(self, response_json: List) -> Dict:
        """베타 UI(Flutter 기반) 전용 기기 목록 파싱 (계층 구조 대응)."""
        result_dict = {}
        for device in response_json:
            mac = device.get("mac", "").strip().replace(":", "").replace("-", "").lower()
            if not mac: continue
            
            info = device.get("info", {})
            conn = device.get("connection", {})
            wireless = conn.get("wireless", {})
            
            # 정보 추출 (계층형 구조 대응)
            ip = info.get("ip", "N/A")
            name = info.get("name", "N/A")
            band = wireless.get("bss", conn.get("type", "Unknown"))
            
            result_dict[mac] = {
                "ip": ip,
                "name": name,
                "band": band,
                "state": "home"
            }
        return result_dict

    def device_parsing(self, response_list: List, band: str) -> Dict:
        result_dict = {}
        for device in response_list:
            if isinstance(device, str):
                tds = _extract_table_cells(device)
            else:
                tds = []
                try:
                    for td in device.find_all("td"):
                        tds.append(html.unescape(td.get_text(" ", strip=True)))
                except Exception:
                    tds = []
            if len(tds) == 4:
                mac = tds[0].strip().replace(":", "").replace("-", "").lower()
                ip_match = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", tds[3])
                result_dict[mac] = {"ip": ip_match.group() if ip_match else "N/A", "band": band, "state": "home"}
        return result_dict

    def json_parsing(self, response_json: Dict, band: str) -> Dict:
        result_dict = {}
        for device in response_json.get("stalist", []):
            if "mac" in device:
                mac = device["mac"].strip().replace(":", "").replace("-", "").lower()
                result_dict[mac] = {"ip": device.get("ipaddr", "N/A"), "band": band,
                    "state": "home"}
        return result_dict

    async def get_mesh_station(self, rssi_limit: int = DEFAULT_RSSI_LIMIT) -> Dict | None:
        """EasyMesh 기기 목록 조회 및 파싱 (예외 격리 및 RSSI 임계값 유기적 조절)."""
        text = await self._async_request("GET", f"{self._url}{MESH_STATION_URN}")
        res = {}
        
        try:
            data = loads(text)
            stations = data.get("station") if isinstance(data, dict) else None
        except (ValueError, TypeError) as err:
            _LOGGER.debug(f"Failed to parse EasyMesh topology JSON: {err}")
            return None
        if not isinstance(stations, list):
            return None

        # The topology response links each station to a Controller/Agent via
        # ``agent_mac``. Build a small, non-sensitive lookup for entity attrs.
        mesh_nodes: Dict[str, Dict[str, Any]] = {}

        def add_mesh_nodes(raw_nodes: Any, role: str) -> None:
            if isinstance(raw_nodes, dict):
                raw_nodes = [raw_nodes]
            if not isinstance(raw_nodes, list):
                return
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                raw_mac = str(node.get("mac") or "").strip()
                normalized_mac = raw_mac.replace(":", "").replace("-", "").lower()
                if not normalized_mac:
                    continue
                product_name = node.get("product_name") or node.get("model")
                mesh_nodes[normalized_mac] = {
                    "name": node.get("nickname") or product_name or raw_mac,
                    "mac": raw_mac,
                    "model": product_name,
                    "role": role,
                    "ip": node.get("ip"),
                }

        add_mesh_nodes(data.get("controller", []), "controller")
        add_mesh_nodes(data.get("agent", []), "agent")

        for s in stations:
            try:
                if not isinstance(s, dict):
                    continue
                mac = s.get("mac")
                if not mac:
                    continue
                mac = mac.strip().replace(":", "").replace("-", "").lower()
                
                # 'Unknown' 혹은 'WIRED' 연결 타입은 필터링 (무선 기기만 추적 대상)
                connection = str(s.get("connection") or "").strip()
                if connection in ["Unknown", "WIRED"]:
                    continue

                # RSSI 안전 파싱
                rssi_val = s.get("rssi")
                rssi_int = 0
                if rssi_val is not None:
                    try:
                        rssi_int = int(rssi_val)
                    except (ValueError, TypeError):
                        rssi_int = 0

                # RSSI 한계값(rssi_limit)과 대조하여 재실 여부 판정
                # 만약 사용자가 RSSI 차단을 끄고 싶다면 설정에서 -100 등의 값을 입력하면 됩니다.
                state = "home" if rssi_val is None or rssi_int >= rssi_limit else "not_home"

                raw_agent_mac = str(s.get("agent_mac") or "").strip()
                agent_key = raw_agent_mac.replace(":", "").replace("-", "").lower()
                mesh_node = mesh_nodes.get(agent_key, {})
                
                res[mac] = {
                    "ip": s.get("ip", "N/A"),
                    "band": s.get("mode", "Unknown"),
                    "state": state,
                    "connected_to": mesh_node.get("name") or raw_agent_mac or None,
                    "connected_to_mac": mesh_node.get("mac") or raw_agent_mac or None,
                    "connected_to_model": mesh_node.get("model"),
                    "connected_to_role": mesh_node.get("role"),
                    "connected_to_ip": mesh_node.get("ip"),
                    "mesh_connection": connection or None,
                    "mesh_channel": s.get("channel"),
                    "mesh_interface": s.get("if_type"),
                }
            except Exception as err:
                _LOGGER.debug(f"Error parsing EasyMesh station {s.get('mac', 'unknown')}: {err}")
                
        return res

    async def async_close(self) -> None:
        if self._session:
            await self._session.close()

    async def async_reboot(self) -> bool:
        """공유기 재부팅 (Web CGI 및 JSON-RPC)."""
        if self._beta_ui:
            try:
                response = await self._async_service_json("reboot/now")
                if not response.get("error"):
                    _LOGGER.info("공유기 재부팅 명령 전송 (JSON-RPC)")
                    return True
            except Exception as err:
                _LOGGER.debug(f"JSON-RPC 재부팅 시도 실패: {err}")

        url = f"{self._url}/sess-bin/timepro.cgi?tmenu=sysconf&smenu=reboot&act=reboot"
        try:
            await self._async_request("GET", url)
            _LOGGER.info("공유기 재부팅 명령 전송 (Web CGI)")
            return True
        except Exception as err: 
            _LOGGER.debug(f"Web CGI 재부팅 시도 실패: {err}")
            return False



    async def async_get_wireless_channel_list(self, band: str) -> List[str]:
        """특정 무선 대역(band)에서 지원하는 채널 목록을 조회한다. (연결될 파일: select.py)"""
        if not self._beta_ui:
            return ["auto"]
        
        # 1차 시도: 단일 문자열 "2g", "5g" 등으로 전달
        response = await self._async_service_json("wireless/channel/list", band)
        
        # 2차 시도: 만약 에러가 나거나 리스트가 안 오면 딕셔너리로 {"band": band} 전달
        if response.get("error") or not isinstance(response.get("result"), list):
            response = await self._async_service_json("wireless/channel/list", {"band": band})
            
        result = response.get("result")
        if isinstance(result, list):
            channels = []
            for ch in result:
                if isinstance(ch, dict):
                    ch_val = ch.get("channel") or ch.get("value")
                    if ch_val is not None:
                        channels.append(str(ch_val))
                else:
                    channels.append(str(ch))
            
            # "auto" 혹은 "0" 정규화 및 수동 주입
            clean_channels = []
            for c in channels:
                if c in ("0", "0.0", "auto", "Auto", "AUTO"):
                    if "auto" not in clean_channels:
                        clean_channels.append("auto")
                else:
                    # 채널 본딩 정보 매핑 (예: 149.155 -> 149 (80MHz))
                    norm_c = format_channel_string(c)
                    if norm_c not in clean_channels:
                        clean_channels.append(norm_c)
            
            if "auto" not in clean_channels:
                clean_channels.insert(0, "auto")
            return clean_channels
        return ["auto"]

    async def async_set_wireless_channel(self, band: str, channel: str) -> bool:
        """특정 무선 대역의 채널을 변경한다. (연결될 파일: select.py)"""
        if not self._beta_ui:
            return False
            
        ch_val = "auto" if str(channel).lower() == "auto" else channel
        
        # 기존 무선 대역 정보가 있으면 상태 필드를 최대한 보존해서 호출
        enable = True
        if "wireless" in self.web_result and "band" in self.web_result["wireless"]:
            for b in self.web_result["wireless"]["band"]:
                if str(b.get("band")).lower() == str(band).lower():
                    enable = bool(b.get("enable", True))
                    break

        params = {
            "band": band,
            "channel": ch_val,
            "enable": enable,
            "commit": True
        }
        
        _LOGGER.info(f"Wi-Fi 채널 변경 요청: band={band}, channel={ch_val}")
        response = await self._async_service_json("wireless/band/set", params)
        if not response.get("error"):
            self._last_caching_time = 0.0 # 캐시 강제 만료
            return True
        else:
            _LOGGER.warning(f"Wi-Fi 채널 변경 중 API 오류: {response.get('error')}")
            
        return False






