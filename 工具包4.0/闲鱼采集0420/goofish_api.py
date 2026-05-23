"""
mtop API 客户端
封装HTTP调用、签名、错误处理、token自动刷新
"""
import json
import time

from goofish_sign import sign_request, get_timestamp_ms
from goofish_session import GoofishSession, APP_KEY, HEADERS_BASE

BASE_URL = "https://h5api.m.goofish.com/h5"


class MtopClient:
    def __init__(self, session: GoofishSession):
        self.gs = session
        self._last_call_time = 0

    def call(self, api: str, version: str, data: dict, referer: str = None,
             session=None) -> dict:
        """调用 mtop API, 返回 data 部分
        失败抛出 MtopError
        session: 可选, 传入独立的curl_cffi Session (多线程场景)
        """
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        token = self.gs.get_token()
        t = get_timestamp_ms()
        sign = sign_request(token, t, APP_KEY, data_str)

        url = f"{BASE_URL}/{api}/{version}/"

        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": sign,
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "AntiCreep": "true",
            "AntiFlool": "true",
            "api": api,
            "sessionOption": "AutoLoginOnly",
        }

        headers = {
            **HEADERS_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if referer:
            headers["Referer"] = referer

        s = session or self.gs.get_session()

        try:
            resp = s.post(url, params=params, data={"data": data_str},
                          headers=headers, timeout=20)
        except Exception as e:
            raise MtopError("network", str(e))

        try:
            result = resp.json()
        except Exception:
            raise MtopError("parse", f"非JSON响应, status={resp.status_code}")

        # 检查返回状态
        ret = result.get("ret", [])
        ret_str = str(ret[0]) if ret else ""

        if "SUCCESS" in ret_str:
            return result.get("data", {})

        if "FAIL_SYS_TOKEN_EXOIRED" in ret_str or "TOKEN_EXOIRED" in ret_str:
            # token过期, 刷新后重试一次
            print(f"  [mtop] token过期, 刷新中...")
            if self.gs.refresh_token():
                return self._retry_call(api, version, data, referer, session)
            raise MtopError("token_expired", "token刷新失败, 请重新登录")

        if "RGV587" in ret_str:
            raise MtopError("rate_limit", f"触发频率限制: {ret_str}")

        if "SESSION_EXPIRED" in ret_str:
            raise MtopError("session_expired", "会话过期, 请重新登录")

        if "FAIL_SYS_ILLEGAL_ACCESS" in ret_str:
            raise MtopError("illegal_access", "非法访问")

        # 其他情况尝试返回data (有些API虽然ret不含SUCCESS但有数据)
        data_part = result.get("data", {})
        if data_part:
            return data_part

        raise MtopError("unknown", f"未知错误: {ret_str[:100]}")

    def _retry_call(self, api, version, data, referer, session=None):
        """token刷新后重试"""
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        token = self.gs.get_token()
        t = get_timestamp_ms()
        sign = sign_request(token, t, APP_KEY, data_str)

        url = f"{BASE_URL}/{api}/{version}/"

        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": sign,
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "AntiCreep": "true",
            "AntiFlool": "true",
            "api": api,
            "sessionOption": "AutoLoginOnly",
        }

        headers = {
            **HEADERS_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if referer:
            headers["Referer"] = referer

        s = session or self.gs.get_session()
        resp = s.post(url, params=params, data={"data": data_str},
                      headers=headers, timeout=20)

        result = resp.json()
        ret = result.get("ret", [])
        ret_str = str(ret[0]) if ret else ""

        if "SUCCESS" in ret_str:
            return result.get("data", {})

        data_part = result.get("data", {})
        if data_part:
            return data_part

        raise MtopError("retry_failed", f"重试后仍失败: {ret_str[:100]}")

    def test_connection(self) -> dict:
        """测试API连通性, 返回结果信息"""
        info = {"token_valid": False, "api_ok": False, "detail": ""}
        try:
            token = self.gs.get_token()
            if not token:
                info["detail"] = "无token"
                return info
            info["token_valid"] = True

            # 尝试调用首页推荐API (无需登录也能用)
            data = self.call(
                "mtop.taobao.idlehome.home.webpc.feed", "1.0",
                {"pageNumber": "1", "pageSize": "1"}
            )
            info["api_ok"] = True
            info["detail"] = f"连接成功, 返回数据keys={list(data.keys())[:5]}"
        except MtopError as e:
            info["detail"] = f"{e.code}: {e.message}"
        except Exception as e:
            info["detail"] = str(e)
        return info


class MtopError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")
