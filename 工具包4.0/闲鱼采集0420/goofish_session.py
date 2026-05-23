"""
Cookie / 会话管理
从 cookies.json (Playwright格式) 加载cookie，构建 curl_cffi Session
支持纯HTTP方式刷新 _m_h5_tk token
"""
import json
import re
import time
from pathlib import Path
from curl_cffi import requests as curl_requests

from goofish_sign import extract_token, sign_request, get_timestamp_ms

COOKIE_FILE = Path(__file__).parent / "cookies.json"
APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
IMPERSONATE = "chrome136"

# 刷新token用的牺牲接口
TOKEN_REFRESH_API = "mtop.taobao.idle.item.web.recommend.list"
TOKEN_REFRESH_URL = f"https://h5api.m.goofish.com/h5/{TOKEN_REFRESH_API}/1.0/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

HEADERS_BASE = {
    "User-Agent": UA,
    "Referer": "https://www.goofish.com/",
    "Origin": "https://www.goofish.com",
}


class GoofishSession:
    def __init__(self, cookie_file=None):
        self.cookie_file = Path(cookie_file) if cookie_file else COOKIE_FILE
        self._session = None
        self._token = ""          # 签名用的 hex token
        self._m_h5_tk = ""        # 完整 _m_h5_tk 值
        self._cookies_raw = []    # Playwright格式的cookie列表

    def load(self) -> bool:
        """从 cookies.json 加载cookie"""
        if not self.cookie_file.exists():
            print(f"[session] cookie文件不存在: {self.cookie_file}")
            return False
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                self._cookies_raw = json.load(f)
        except Exception as e:
            print(f"[session] 读取cookie失败: {e}")
            return False

        # 构建 curl_cffi session
        self._session = curl_requests.Session(impersonate=IMPERSONATE)

        # 从 Playwright 格式转换为 requests 格式
        for c in self._cookies_raw:
            name = c.get("name", "")
            value = c.get("value", "")
            domain = c.get("domain", "")
            path = c.get("path", "/")

            # 提取 _m_h5_tk
            if name == "_m_h5_tk" and ".goofish.com" in domain:
                self._m_h5_tk = value
                self._token = extract_token(value)

            # 设置cookie到session
            self._session.cookies.set(name, value, domain=domain, path=path)

        if not self._token:
            print("[session] 未找到 _m_h5_tk, 将尝试刷新")
            return self.refresh_token()

        print(f"[session] 加载成功, token={self._token[:12]}..., cookies={len(self._cookies_raw)}")
        return True

    def get_token(self) -> str:
        return self._token

    def get_session(self) -> curl_requests.Session:
        return self._session

    def is_valid(self) -> bool:
        """检查token是否过期"""
        if not self._m_h5_tk or not self._token:
            return False
        try:
            parts = self._m_h5_tk.split("_")
            if len(parts) >= 2:
                tk_time = int(parts[1])
                now_ms = int(time.time() * 1000)
                # token有效期一般是几小时, 超过2小时认为需要刷新
                return (now_ms - tk_time) < 7200_000
        except (ValueError, IndexError):
            pass
        return bool(self._token)

    def refresh_token(self) -> bool:
        """通过HTTP POST到牺牲接口刷新token (不需要浏览器)"""
        if not self._session:
            self._session = curl_requests.Session(impersonate=IMPERSONATE)

        payload = json.dumps({"itemId": "0", "pageSize": 1, "pageNum": 1},
                             separators=(",", ":"), ensure_ascii=False)
        t = get_timestamp_ms()
        # 空token签名 (首次获取)
        sign = sign_request("", t, APP_KEY, payload)

        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "AntiCreep": "true",
            "AntiFlool": "true",
            "api": TOKEN_REFRESH_API,
        }

        headers = {
            **HEADERS_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            resp = self._session.post(
                TOKEN_REFRESH_URL,
                params=params,
                data={"data": payload},
                headers=headers,
                timeout=15,
            )

            # 从响应的 Set-Cookie 头提取新 _m_h5_tk 和 _m_h5_tk_enc
            set_cookie = resp.headers.get("set-cookie", "")
            m = re.search(r'_m_h5_tk=([^;]+)', set_cookie)
            new_tk = m.group(1) if m else None
            m_enc = re.search(r'_m_h5_tk_enc=([^;]+)', set_cookie)
            new_enc = m_enc.group(1) if m_enc else None

            # 备用: 从session cookie jar读取
            if not new_tk:
                new_tk = self._session.cookies.get("_m_h5_tk")
            if not new_enc:
                new_enc = self._session.cookies.get("_m_h5_tk_enc")

            if new_tk and "_" in new_tk:
                self._m_h5_tk = new_tk
                self._token = extract_token(new_tk)
                # 同步更新session cookie jar (必须同时更新 _m_h5_tk 和 _m_h5_tk_enc)
                self._session.cookies.set("_m_h5_tk", new_tk, domain=".goofish.com")
                if new_enc:
                    self._session.cookies.set("_m_h5_tk_enc", new_enc, domain=".goofish.com")
                print(f"[session] token刷新成功: {self._token[:12]}...")
                self._update_cookies_raw(new_enc)
                return True

            print(f"[session] 刷新未获得新token, status={resp.status_code}")
            return False
        except Exception as e:
            print(f"[session] 刷新失败: {e}")
            return False

    def _update_cookies_raw(self, new_enc=None):
        """将session中的cookie更新回raw列表"""
        for c in self._cookies_raw:
            if c.get("name") == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
                c["value"] = self._m_h5_tk
            if new_enc and c.get("name") == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
                c["value"] = new_enc

    def save_cookies(self):
        """保存cookie回JSON文件"""
        try:
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(self._cookies_raw, f, ensure_ascii=False, indent=2)
            print(f"[session] cookie已保存")
        except Exception as e:
            print(f"[session] 保存失败: {e}")
