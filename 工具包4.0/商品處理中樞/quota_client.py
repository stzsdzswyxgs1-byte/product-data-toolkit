# -*- coding: utf-8 -*-
"""
配額管理 client — 跟 Cloudflare Worker (quota-worker) 通訊

Worker URL: https://quota-worker.example.workers.dev
路由: /quota/{tg_id}, /quota/{tg_id}/consume, /quota/{tg_id}/refund

配額規則:
  - 每天 02:00 UTC (= 10:00 Asia/Taipei) 自動重置
  - 每個 tg_id 獨立額度 (預設 2000/日, 管理員可調)
  - 1 件商品 = 1 額度
  - 只在跑「貴功能」前扣 (Stage E / 去水印 / 首圖優化)
"""
import json
import urllib.request
import urllib.error
from typing import Optional


class QuotaError(Exception):
    """配額相關錯誤基底"""


class QuotaExceeded(QuotaError):
    """配額不足. 帶 used/limit/remaining/can_consume 資訊"""
    def __init__(self, message, info: dict):
        super().__init__(message)
        self.info = info


class QuotaConfigError(QuotaError):
    """配置錯誤 (沒填 tg_id, 沒填 endpoint, 等等)"""


class QuotaNetworkError(QuotaError):
    """網路錯誤 (Worker 連不上, 超時, 等等)"""


class QuotaClient:
    """配額 client. 從 config 讀 endpoint + secret, 發 HTTP 給 Worker."""

    EXPENSIVE_STEPS = ('v65_stage_e', 'image_dewatermark', 'image_opt')

    def __init__(self, config: dict):
        """
        config 預期格式:
            {
              "tg_id": "YOUR_TG_ID",
              "quota": {
                "mode": "middleware",  # off | middleware
                "endpoint": "https://quota-worker.example.workers.dev",
                "client_secret": "...",
                "default_daily_limit": 2000
              }
            }
        """
        self.config = config
        qc = config.get('quota') or {}
        self.mode = qc.get('mode', 'middleware')
        self.endpoint = (qc.get('endpoint') or '').rstrip('/')
        self.secret = qc.get('client_secret', '')
        self.tg_id = str(config.get('tg_id', '')).strip()
        self.timeout = qc.get('timeout', 8)

    # ────────────────────────────────────────────────────────────
    # 公用方法 (給 pipeline / app 用)
    # ────────────────────────────────────────────────────────────
    @classmethod
    def has_expensive_step(cls, steps_config: dict) -> bool:
        """檢查 config['steps'] 裡有沒有勾任一貴功能"""
        for key in cls.EXPENSIVE_STEPS:
            if (steps_config.get(key) or {}).get('enabled'):
                return True
        return False

    def is_active(self) -> bool:
        """配額是否啟用 (mode != off)"""
        return self.mode != 'off'

    def is_configured(self) -> bool:
        """配置完整 (有 endpoint + secret + tg_id)"""
        return bool(self.endpoint and self.secret and self.tg_id)

    def precheck(self, expected_count: int, has_expensive: bool):
        """
        開跑前檢查. 返回 dict {used, limit, remaining, ...}.

        若 mode=off → 永遠 pass (返回 None)
        若 has_expensive=False → 不扣額, 但仍會回查當前狀態 (None 表示不需檢查)
        若 mode=middleware 但 tg_id 空 → 拋 QuotaConfigError (鎖死)
        若 expected_count > remaining → 不拋錯, 由 caller 決定 (拆檔 or 中止)
        """
        if not self.is_active():
            return None
        if not has_expensive:
            return None  # 沒勾貴功能, 不需 check
        if not self.tg_id:
            raise QuotaConfigError(
                "未填 TG ID — 跑貴功能 (Stage E / 去水印 / 首圖優化) 必須在設定填 TG ID. "
                "請到「⚙ 設定」→「TG ID」填你的 Telegram chat_id."
            )
        if not self.endpoint or not self.secret:
            raise QuotaConfigError(
                "quota 配置不完整 (endpoint / client_secret 空). 聯絡管理員."
            )
        return self._http('GET', f'/quota/{self.tg_id}')

    def consume(self, count: int) -> dict:
        """原子扣 count 額度. 不夠時拋 QuotaExceeded."""
        if not self.is_active() or not self.tg_id:
            return {}
        if count <= 0:
            return {}
        try:
            return self._http('POST', f'/quota/{self.tg_id}/consume', {'count': count})
        except QuotaError:
            raise

    def refund(self, count: int) -> Optional[dict]:
        """退回 count 額度 (abort/中止時用). 失敗只記 log, 不拋."""
        if not self.is_active() or not self.tg_id or count <= 0:
            return None
        try:
            return self._http('POST', f'/quota/{self.tg_id}/refund', {'count': count})
        except Exception:
            return None

    def check(self) -> Optional[dict]:
        """純查詢 (不扣). 沒配置時返回 None."""
        if not self.is_active() or not self.is_configured():
            return None
        try:
            return self._http('GET', f'/quota/{self.tg_id}')
        except Exception:
            return None

    # ────────────────────────────────────────────────────────────
    # 底層 HTTP
    # ────────────────────────────────────────────────────────────
    def _http(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = self.endpoint + path
        data = None
        # ★ User-Agent 必須像瀏覽器 — Cloudflare 預設擋 Python-urllib
        headers = {
            'Authorization': f'Bearer {self.secret}',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) quota-client/1.0',
        }
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
                return payload
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode('utf-8'))
            except Exception:
                err_body = {'error': f'http_{e.code}', 'message': str(e)}

            if e.code == 400 and err_body.get('error') == 'quota_exceeded':
                raise QuotaExceeded(
                    f"配額不足: 今日已用 {err_body.get('used')}/{err_body.get('limit')}, "
                    f"剩 {err_body.get('remaining')}, 想扣 {err_body.get('requested')}",
                    err_body
                )
            if e.code == 401:
                raise QuotaConfigError(f"認證失敗 (client_secret 不正確): {err_body}")
            raise QuotaNetworkError(f"HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise QuotaNetworkError(f"連線失敗 ({self.endpoint}): {e.reason}")
        except Exception as e:
            raise QuotaNetworkError(f"未知錯誤: {e}")
