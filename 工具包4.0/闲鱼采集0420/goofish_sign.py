"""
mtop API 签名引擎
sign = MD5(token & t & appKey & data)
"""
import hashlib
import time


def sign_request(token: str, t: str, app_key: str, data: str) -> str:
    s = f"{token}&{t}&{app_key}&{data}"
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def extract_token(m_h5_tk_value: str) -> str:
    """从 _m_h5_tk cookie 值中提取 token 部分
    '1780e6974400394db42e6698fc0f3698_1773051288539' -> '1780e6974400394db42e6698fc0f3698'
    """
    if not m_h5_tk_value:
        return ""
    return m_h5_tk_value.split("_")[0]


def get_timestamp_ms() -> str:
    return str(int(time.time() * 1000))
