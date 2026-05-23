"""
Checkpoint + APIMonitor

- CheckpointManager: 寫 _checkpoint.json (state) + _checkpoint.xlsx (df), atomic
- APIMonitor: 連續 N 次失敗 → raise PauseException
- PauseException: 拋出代表 API 掛或用戶手動暫停, pipeline 抓住 → 寫 ckpt → return 'paused'
"""
import os, json, time, threading
from datetime import datetime

class PauseException(Exception):
    """API 失效或用戶暫停, 觸發 checkpoint 寫盤 + 流程中止"""
    pass


class DynamicSemaphore:
    """
    執行緒安全的動態 semaphore — 可以 mid-flight 調整 target 不卡死。

    用法:
      sem = DynamicSemaphore(initial=51)
      def _worker(task):
          if not sem.acquire():  # False = 收到 shutdown
              return None
          try:
              return _do_work(task)
          finally:
              sem.release()

      # mid-flight 調整 (worker 還在跑時)
      sem.set_target(38)  # 降到 38, in-flight 不影響, 之後新 acquire 等到 active < 38

      # user_stop / 結束:
      sem.shutdown()  # 喚醒所有 acquire 等待者, 他們 return False

    保證:
    - 多 thread 同時 acquire/release 不會 race (Condition lock)
    - set_target 不會卡死 (升高 notify_all, 降低 in-flight 自然消化)
    - shutdown 不會 deadlock (notify_all)
    - target 永遠 >= 1 (避免 acquire 永遠 block)
    """
    def __init__(self, initial: int):
        import threading as _th
        self._target = max(1, int(initial))
        self._active = 0
        self._cond = _th.Condition()
        self._shutdown = False

    @property
    def target(self):
        with self._cond:
            return self._target

    @property
    def active(self):
        with self._cond:
            return self._active

    def acquire(self) -> bool:
        """阻塞直到能取得 1 個 slot. shutdown 時 return False."""
        with self._cond:
            while self._active >= self._target and not self._shutdown:
                self._cond.wait()
            if self._shutdown:
                return False
            self._active += 1
            return True

    def release(self):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()

    def set_target(self, new: int) -> tuple:
        """調整 target. 升高會喚醒等待者, 降低靠 in-flight 自然消化"""
        new = max(1, int(new))
        with self._cond:
            old = self._target
            self._target = new
            if new > old:
                self._cond.notify_all()  # 升高 → 多釋放 (new-old) 個 slot
        return old, new

    def shutdown(self):
        """中止 — 喚醒所有等待的 acquire, 讓它們 return False"""
        with self._cond:
            self._shutdown = True
            self._cond.notify_all()


class APIMonitor:
    """全域 API 失敗計數器, 連續 N 次失敗 → raise PauseException"""

    def __init__(self, fail_threshold: int = 8, success_reset: int = 5, event_log_fn=None):
        self.fail_threshold = fail_threshold
        self.success_reset = success_reset
        self.consecutive_fails = 0
        self.consecutive_success = 0
        self._lock = threading.Lock()
        self._paused = False  # 一旦標記就不再呼叫 (避免 pause 中被多 thread 干擾)
        self.user_stop = False  # ★ 用戶按停止 → 各個 batch loop 檢查這個 flag → 提早結束
        # ★ 4.0.24 admin 統計 — 給 admin Claude 詳細日誌結尾 dump 看「VPN 抖了幾次」
        self.total_network_blips = 0
        self.total_real_failures = 0
        self.total_pause_codes = 0
        self.total_skip_codes = 0
        self.total_successes = 0
        # ★ 4.0.26 admin 事件流 — 每個 record_fail 寫 1 行進詳細日誌, admin 看 timeline
        self._event_log_fn = event_log_fn  # callable(line: str) → 寫進 detail log

    def record_success(self):
        with self._lock:
            self.consecutive_success += 1
            self.total_successes += 1
            if self.consecutive_success >= self.success_reset:
                self.consecutive_fails = 0  # 連續成功 N 次 → 重置失敗計數

    def record_action(self, action: str):
        """4.0.24: 給 admin 統計用 — 累積各 action 出現次數 (不影響 pause 邏輯)"""
        with self._lock:
            if action == 'network_blip':
                self.total_network_blips += 1
            elif action == 'retry':
                self.total_real_failures += 1
            elif action == 'pause':
                self.total_pause_codes += 1
            elif action == 'skip':
                self.total_skip_codes += 1

    # ★ 對齊中介 v3.5 完整分類表 (4 大 action: success / skip / retry / pause)
    # SKIP = 個別圖永久問題 (mark_done, 不重試, 不算 monitor)
    SKIP_CODES = {
        'content_policy_violation', # Disney/版權 (400)
        'stream_disconnected',      # OpenAI 個別圖主動斷流 (502)
        'middleware_timeout',       # 中介 150s hard cap (504)
    }
    # PAUSE = 中介保險閥觸發 (mark_failed 等狀況恢復, 不算 monitor 避免死循環)
    PAUSE_CODES = {
        'cascade_pause',            # 中介偵測 cooldown 全停 60s (503)
        'hub_aborted',              # hub 自己 call stop (503)
        'no_codex_account',         # 0 active 帳號 (503)
        'all_credentials_cooling',  # image 端點全 cooling (503)
        'usage_limit_reached',      # 上游帳號配額爆 (503)
    }
    # RETRY = 上游 transient 錯誤 (mark_failed, 算 monitor)
    RETRY_CODES = {
        'upstream_timeout',         # 上游處理太久 (504)
        'upstream_error',           # 上游服務錯誤 (5xx)
        'unknown_error',            # 未分類錯誤 (500)
        'stream_error',             # upstream stream 錯 (502)
        'upstream_connection_error',# 中介連不上 cli-proxy (502)
        'stream_truncated',         # chat SSE 截斷 (502)
        'aggregate_error',          # chat 聚合錯誤 (502)
        'upstream_truncated',       # chat 流被截 (502)
        'proxy_error',              # proxy generic (502)
    }
    # 限流 (retry, 等)
    RATE_LIMIT_CODES = {
        'rate_limit_exceeded',      # 429 通用
        'local_rpm_cap',            # Gemini RPM
        'hourly_limit',             # 中介 40000/hr
        'queue_full',               # 中介 queue 滿
        'ip_queue_full',            # per-IP queue 滿
    }
    # 中介統一標記
    MIDDLEWARE_CLASSIFIED_TYPE = 'middleware_classified'

    # 舊 API 兼容: 不算 monitor 失敗的集合
    MIDDLEWARE_NON_FAIL_CODES = SKIP_CODES | PAUSE_CODES
    MIDDLEWARE_NON_FAIL_TYPES = {'middleware_classified', 'hourly_limit', 'rate_limit_exceeded'}
    # 舊 API 兼容
    INDIVIDUAL_PERMANENT_CODES = SKIP_CODES
    MIDDLEWARE_PROTECTIVE_CODES = PAUSE_CODES | {
        'upstream_truncated', 'stream_truncated', 'aggregate_error', 'local_rpm_cap',
    }
    MIDDLEWARE_PROTECTIVE_TYPES = {'middleware_classified', 'hourly_limit', 'rate_limit_exceeded'}

    @classmethod
    def classify_response(cls, status_code=None, body=None, exception=None) -> tuple:
        """★★★ 統一分類接口 (對齊中介 v3.5)
        回 (action, reason)
        action: 'success' | 'skip' | 'retry' | 'pause' | 'network_blip'
          - success     : 200 + 正常 data
          - skip        : 個別圖永久問題 (mark_done, 不重試, 不計 monitor)
          - retry       : 上游 transient API failure (mark_failed 重試, 計 monitor → 累積 pause)
          - pause       : 中介保險閥 (mark_failed 重試, 不計 monitor 避免死循環)
          - network_blip: ★ 4.0.24 hub 端純連線層抖動 (VPN / CF Tunnel / urllib3 連不上)
                          不是 API 失敗, 不該計 monitor (避免 VPN 抖動 8 次就誤觸發 PauseException
                          全 batch 中斷). 仍 mark_failed → image_opt 內建 retry 機制會自己處理.
        """
        # 1. exception (連線層) — VPN 抖動 / CF Tunnel / urllib3 連線失敗
        if exception:
            msg = str(exception).lower()
            # 純網路層失敗: urllib3 / requests 連線都連不上中介, 不算 API 失敗
            network_blip_patterns = (
                'response ended prematurely',
                'max retries exceeded',         # urllib3 retry 用完 (HTTPSConnectionPool)
                'newconnectionerror',           # 連都連不上
                'connectionreseterror',         # 連線被 reset
                'connection refused',
                'connection reset',
                'connection aborted',
                'remotedisconnected',           # 上游關連線
                'chunkedencodingerror',         # IncompleteRead
                'incompleteread',
                'name or service not known',    # DNS 失敗
                'nodename nor servname',
                'getaddrinfo failed',
                'temporary failure in name resolution',
                'ssl: ',                        # SSL 握手失敗
                'tlsv1_alert',
                'badstatusline',                # HTTP 響應行壞
            )
            if any(p in msg for p in network_blip_patterns):
                return ('network_blip', 'network_layer_blip')
            # 廣泛 connection / timeout (落單字, 含 read timeout) 也算 blip
            if 'connection' in msg or 'timeout' in msg:
                return ('network_blip', 'network_generic')
            return ('retry', f'exception: {msg[:60]}')

        # 2. 沒 body / non-JSON (可能 CF HTML 錯誤頁)
        if body is None:
            if status_code is not None and 520 <= status_code < 530:
                return ('retry', f'cloudflare_{status_code}')
            if status_code is not None and 500 <= status_code < 600:
                return ('retry', f'http_{status_code}_no_body')
            return ('retry', f'non_json_{status_code or "?"}')

        err = body.get('error') or {}
        code = (err.get('code') or '').strip().lower()
        err_type = (err.get('type') or '').strip().lower()

        # 3. 200 沒 error → 成功
        if status_code == 200 and not err:
            return ('success', None)

        # 4. 看 code (不論 type 是否 middleware_classified, 只要 code 在已知集合就分類)
        # 中介通常會帶 type='middleware_classified', 但有時也可能直接給 code
        if code in cls.SKIP_CODES:
            return ('skip', code)
        if code in cls.PAUSE_CODES:
            return ('pause', code)
        if code in cls.RETRY_CODES:
            return ('retry', code)

        # 5. 限流 429 / hourly_limit / rate_limit_exceeded → pause (不算 monitor, retry)
        # 中介保險閥, hub 應等視窗 reset, 不該因此 self-pause 死循環
        if status_code == 429 or err_type in ('hourly_limit', 'rate_limit_exceeded') or code in cls.RATE_LIMIT_CODES:
            return ('pause', f'rate_limit_{code or err_type or "?"}')

        # 6. 401 → token 失效
        if status_code == 401:
            return ('skip', 'token_invalidated')

        # 7. 5xx → retry
        if status_code is not None and 500 <= status_code < 600:
            return ('retry', f'http_{status_code}')

        # 8. hub fallback chain 用錯 model
        em = (err.get('message') or '').lower()
        if 'unknown provider' in em:
            return ('skip', 'unknown_provider')

        # 9. 200 + 未知 error
        if status_code == 200:
            return ('retry', f'200_unexpected_{code or err_type or "?"}')

        return ('retry', 'unknown')

    @classmethod
    def is_real_api_failure(cls, action: str) -> bool:
        """給 monitor 計數: 只有 retry 算 (累積→PauseException).
        pause / skip / success / network_blip 不算 — network_blip 是 hub 端 VPN/網路抖動,
        不該因此誤觸發 PauseException 中斷整 batch (4.0.24 修).
        """
        return action == 'retry'

    @classmethod
    def should_retry_action(cls, action: str) -> bool:
        """給 ckpt mark: retry/pause/network_blip → mark_failed (重試);
        skip/success → mark_done (不重試)"""
        return action in ('retry', 'pause', 'network_blip')

    @classmethod
    def should_retry(cls, status_code=None, error_code=None, error_type=None, error_message=None, exception=None) -> bool:
        """舊 API 兼容: 內部走 classify_response → should_retry_action"""
        body = None
        if error_code or error_type or error_message:
            body = {'error': {'code': error_code, 'type': error_type, 'message': error_message}}
        action, _ = cls.classify_response(status_code=status_code, body=body, exception=exception)
        return cls.should_retry_action(action)

    @classmethod
    def classify_failure(cls, status_code=None, error_code=None, error_type=None, error_message=None, exception=None):
        """舊 API 兼容: 內部走 classify_response → is_real_api_failure"""
        body = None
        if error_code or error_type or error_message:
            body = {'error': {'code': error_code, 'type': error_type, 'message': error_message}}
        action, _ = cls.classify_response(status_code=status_code, body=body, exception=exception)
        return cls.is_real_api_failure(action)

    def record_fail(self, status_code=None, error_code=None, error_type=None, error_message=None, exception=None, stage=''):
        """記錄失敗. 自動用 classify_failure 判斷是否真 API 掛.

        舊 API 兼容: 如果只傳 is_real_api_failure=True/False 也支援 (deprecated)
        ★ 4.0.26: 加 stage kwarg (例 'image_opt', 'k0_locate'), 用於 admin event 事件流
        """
        # 兼容舊呼叫: record_fail(is_real_api_failure=True/False)
        if status_code is True or status_code is False:
            is_real = bool(status_code)
            action = 'retry' if is_real else 'success'  # 兼容路徑沒 action 概念
            reason = '(legacy_api)'
        else:
            # ★ 4.0.24: 同步累積各 action 統計給 admin 看 (network_blip 等)
            body = None
            if error_code or error_type or error_message:
                body = {'error': {'code': error_code, 'type': error_type, 'message': error_message}}
            action, reason = self.classify_response(status_code=status_code, body=body, exception=exception)
            self.record_action(action)
            is_real = self.is_real_api_failure(action)
            # ★ 4.0.26: 寫 admin 事件流 (每個失敗 1 行)
            if self._event_log_fn:
                try:
                    import datetime as _dt
                    ts = _dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]
                    ec = error_code or ''
                    et = error_type or ''
                    code_str = ec or et or '-'
                    exc_name = type(exception).__name__ if exception else '-'
                    exc_short = (str(exception)[:80].replace('\n', ' ') if exception
                                 else (error_message or '')[:80].replace('\n', ' ') if error_message else '-')
                    # 一行緊湊格式, 容易 grep:
                    # [ADMIN_EVENT] HH:MM:SS.mmm stage=X action=Y reason=Z status=N code=K exc=ExcName fails=F blips=B
                    line = (f'[ADMIN_EVENT] {ts} stage={stage or "?":12s} '
                            f'action={action:13s} reason={reason or "-"} '
                            f'status={status_code or "-"} code={code_str} '
                            f'exc={exc_name} '
                            f'fails={self.consecutive_fails+(1 if is_real else 0)} '
                            f'blips={self.total_network_blips}')
                    if exc_short and exc_short != '-':
                        line += f' | {exc_short}'
                    self._event_log_fn(line)
                except Exception:
                    pass
        if not is_real:
            return
        with self._lock:
            self.consecutive_fails += 1
            self.consecutive_success = 0
            if self.consecutive_fails >= self.fail_threshold and not self._paused:
                self._paused = True
                # ★ 通知中介 stop: 立刻丟掉本 IP 在 queue 裡的請求 + 60s 拒絕新請求 (不浪費 OpenAI 配額)
                self._notify_middleware_stop(reason=f'hub 連續 {self.consecutive_fails} 次失敗')
                raise PauseException(
                    f'API 連續 {self.consecutive_fails} 次失敗 (>= {self.fail_threshold}), 暫停'
                )

    def _notify_middleware_stop(self, reason: str = '', duration_s: int = 60,
                                 stop_url: str = 'https://api.example.com/v1/admin/stop',
                                 key: str = '<TEST_API_KEY>'):
        """通知中介 hub 暫停 — 中介立刻丟棄 hub IP 的 queue + 60s 拒絕新請求"""
        import requests
        try:
            r = requests.post(stop_url,
                              headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                              json={'duration_s': duration_s, 'reason': reason}, timeout=5)
            if r.status_code == 200:
                try:
                    j = r.json()
                    self._last_stop_response = j
                except Exception:
                    pass
        except Exception:
            pass  # 通知失敗不影響本地 paused 狀態, 中介自己有 cascade 偵測兜底

    def notify_middleware_resume(self,
                                  resume_url: str = 'https://api.example.com/v1/admin/resume',
                                  key: str = '<TEST_API_KEY>'):
        """用戶按繼續時通知中介解除封鎖"""
        import requests
        try:
            r = requests.post(resume_url,
                              headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                              json={}, timeout=5)
            if r.status_code == 200:
                try: return True, r.json()
                except: return True, {}
        except Exception as e:
            return False, {'error': str(e)[:100]}
        return False, {'error': f'HTTP {r.status_code}'}

    def is_paused(self):
        with self._lock:
            return self._paused

    def reset(self):
        with self._lock:
            self.consecutive_fails = 0
            self.consecutive_success = 0
            self._paused = False

    def ping(self, api_url: str = '', key: str = '', model: str = 'gpt-5.5',
             health_url: str = 'https://api.example.com/v1/health'):
        """輕量 ping API. 用中介專用 /v1/health (不耗 token, 10s cache).

        回應規格 (中介 v3):
        - HTTP 200 + status='ok'        → 全鏈路通 (reset, 可繼續)
        - HTTP 200 + status='degraded'  → 中介 cooldown / hourly 滿, 等 1-2 分
        - HTTP 503                      → 上游 cli-proxy-api 掛, API 真掛
        """
        import requests
        try:
            r = requests.get(health_url, timeout=5)
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    return False, f'/v1/health 200 但 JSON 解析失敗: {r.text[:80]}'
                # ★ 中介 cascade pause (5 次 30s 內 cooling 觸發 → 全停 60s)
                if j.get('cascade_paused'):
                    rem = j.get('cascade_remaining_s', 0)
                    return False, f'中介 cascade 全停 (上游帳號全 cooldown), 剩 {rem}s'
                # ★ hub 自己之前 call /v1/admin/stop 還沒解
                if j.get('hub_stop_active'):
                    return False, f'中介還記得 hub 之前 stop, 請先 resume'
                status = (j.get('status') or '').lower()
                if status == 'ok':
                    self.reset()
                    latency = j.get('upstream_latency_ms', '?')
                    return True, f'API 通了 (上游 {latency}ms)'
                elif status == 'degraded':
                    cd = j.get('cooldown_remaining_s', 0)
                    hf = j.get('hourly_full', False)
                    why = '中介 cooldown' if cd else ('hourly 配額滿' if hf else '中介 degraded')
                    return False, f'{why} (剩 {cd}s)'
                else:
                    return False, f'/v1/health status={status}: {j}'
            elif r.status_code == 503:
                try:
                    err = r.json().get('upstream_error', 'cli-proxy 掛')
                except Exception:
                    err = 'cli-proxy 掛 (503)'
                return False, f'上游連不上: {err}'
            else:
                return False, f'/v1/health HTTP {r.status_code}: {r.text[:80]}'
        except Exception as e:
            return False, f'CF/中介都連不上: {str(e)[:80]}'


# 全域 monitor (lazy init, 由 pipeline 開跑時 attach)
_global_monitor: 'APIMonitor | None' = None

def set_monitor(m: APIMonitor):
    global _global_monitor
    _global_monitor = m

def get_monitor() -> APIMonitor:
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = APIMonitor()
    return _global_monitor


def warmup_and_ping(api_url: str = 'https://api.example.com/v1/chat/completions',
                    health_url: str = 'https://api.example.com/v1/health',
                    key: str = '<TEST_API_KEY>', model: str = 'gpt-5.5') -> dict:
    """跑前 warmup: 發 1 個短 chat 讓中介認到 hub IP, 等 2s 再 ping 看真實 dynamic_cap"""
    import requests, base64, time as _t
    from io import BytesIO
    from PIL import Image
    img = Image.new('RGB', (100, 100), 'white')
    buf = BytesIO(); img.save(buf, 'JPEG', quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode()
    body = {'model': model, 'temperature': 0,
            'messages':[{'role':'user','content':[
                {'type':'text','text':'回 ok'},
                {'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{b64}'}}]}]}
    try:
        requests.post(api_url, headers={'Authorization': f'Bearer {key}','Content-Type':'application/json'},
                      json=body, timeout=15)
    except Exception:
        pass
    _t.sleep(2)  # 等中介更新 dynamic_cap
    try:
        r = requests.get(health_url, timeout=5)
        if r.status_code == 200: return r.json()
    except Exception: pass
    return {}


def get_optimal_workers(task_type: str = 'chat_short',
                        avg_response_sec: float = 5.0,
                        fallback: int = 24,
                        max_cap: int = 96,
                        min_floor: int = 4,
                        safety_margin: float = 0.85,
                        warmup: bool = False,
                        health_url: str = 'https://api.example.com/v1/health') -> tuple:
    """
    動態取最佳並發. 回 (workers, info_dict).

    ★★★ 中介為指揮中心 (v3.3 後新增):
    /v1/health 直接回 suggested_workers 含:
      - chat_short  (avg 5s,  Stage C/E, K0)
      - chat_medium (avg 10s, Stage A/B/D 用 batch=10 件)
      - chat_long   (avg 25s, 重 reasoning)
      - image_edit  (avg 9s,  image_opt)
      - image_long  (avg 30s, 複雜圖)

    中介已自動處理:
    - tier 區分 (Pro=100, ProLite=40)
    - 多 hub 共享 (my_rpm_share = total_cap / active_hubs)
    - cascade recovery 期間 ×0.5
    - 帳號 cooldown 期間自動降

    Hub 直接抓 suggested_workers[task_type], 不需自己算!

    fallback: 抓不到中介建議時用 avg_response_sec 自算 (保守)
    """
    import requests
    info = {}
    try:
        if warmup:
            j = warmup_and_ping()
            if j: info.update(j)
            else:
                r = requests.get(health_url, timeout=5)
                if r.status_code == 200: info.update(r.json())
        else:
            r = requests.get(health_url, timeout=5)
            if r.status_code != 200:
                info['error'] = f'HTTP {r.status_code}'
                return fallback, info
            info.update(r.json())
        j = info

        # ★ 優先用中介 suggested_workers (中介為真相)
        suggested = j.get('suggested_workers', {})
        if suggested and task_type in suggested:
            w = int(suggested[task_type])
            workers = max(min_floor, min(max_cap, w))
            info['source'] = 'middleware_suggested'
            info['workers'] = workers
            return workers, info

        # ★ Fallback: 中介沒回 suggested_workers (舊版本), 自己算
        rpm_cap = j.get('codex_dynamic_rpm_cap', 0)
        my_share = j.get('my_rpm_share', rpm_cap)  # 多 hub 共享時用 my_share
        cap = my_share if my_share else rpm_cap
        if cap <= 0:
            # 中介 idle (active=0 沒人在跑), 樂觀假設 4 帳號 × 80 = 320
            cap = 320
            info['rpm_cap_assumed'] = True
        info['rpm_cap_used'] = cap
        workers = int(cap * avg_response_sec / 60 * safety_margin)
        workers = max(min_floor, min(max_cap, workers))
        info['source'] = 'hub_fallback_compute'
        info['workers'] = workers
        return workers, info
    except Exception as e:
        info['error'] = str(e)[:80]
        return fallback, info


# ──────────────────────────────────────────────────────────────────────
class CheckpointManager:
    """
    Checkpoint = JSON state + XLSX df snapshot, atomic write (.tmp + os.replace)

    state schema:
    {
      'run_id': 'run_20260507_120000',
      'output_dir': 'C:/...',
      'input_path': 'C:/.../input.xlsx',
      'created_at': iso,
      'last_save': iso,
      'completed_stages': ['stage_a', 'stage_b', ...],
      'current_stage': 'stage_e',
      'stage_progress': {
         'stage_e': {'12345/2.jpg': 'completed', '12345/3.jpg': 'failed', ...}
      },
      'paused': false,
      'paused_at': null,
      'paused_reason': null
    }
    """

    ALL_STAGES = ['stage_a', 'stage_b', 'stage_c', 'stage_d', 'stage_e',
                  'k0_dewatermark', 'image_opt', 'final_export']

    def __init__(self, output_dir: str, input_path: str = '', run_id: str = ''):
        self.output_dir = output_dir
        self.json_path = os.path.join(output_dir, '_checkpoint.json')
        self.xlsx_path = os.path.join(output_dir, '_checkpoint.xlsx')
        self._lock = threading.Lock()
        if not os.path.exists(self.json_path):
            self.state = {
                'run_id': run_id or f'run_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
                'output_dir': output_dir,
                'input_path': input_path,
                'created_at': datetime.now().isoformat(),
                'last_save': datetime.now().isoformat(),
                'completed_stages': [],
                'current_stage': None,
                'stage_progress': {},
                'paused': False,
                'paused_at': None,
                'paused_reason': None,
            }
        else:
            self.state = self._load_state()

    @classmethod
    def has_checkpoint(cls, output_dir: str) -> bool:
        return os.path.exists(os.path.join(output_dir, '_checkpoint.json'))

    @classmethod
    def load_existing(cls, output_dir: str) -> 'CheckpointManager':
        ckpt = cls.__new__(cls)
        ckpt.output_dir = output_dir
        ckpt.json_path = os.path.join(output_dir, '_checkpoint.json')
        ckpt.xlsx_path = os.path.join(output_dir, '_checkpoint.xlsx')
        ckpt._lock = threading.Lock()
        ckpt.state = ckpt._load_state()
        return ckpt

    def _load_state(self):
        with open(self.json_path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        # ★ 防呆: 補齊缺失欄位 (損壞 json / 舊版本相容)
        defaults = {
            'run_id': '', 'output_dir': self.output_dir, 'input_path': '',
            'created_at': '', 'last_save': '',
            'completed_stages': [], 'current_stage': None,
            'stage_progress': {}, 'paused': False,
            'paused_at': None, 'paused_reason': None,
        }
        for k, v in defaults.items():
            if k not in loaded:
                loaded[k] = v
        return loaded

    def _atomic_write_json(self):
        tmp = self.json_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.json_path)

    def _atomic_write_xlsx(self, df):
        # ★ atomic: 寫到 BytesIO 再 binary write 到 .tmp, replace
        # (pandas/openpyxl 都從副檔名檢查, 不能用 .tmp 副檔名直接寫)
        from io import BytesIO
        buf = BytesIO()
        df.to_excel(buf, index=False, engine='openpyxl')
        tmp = self.xlsx_path + '.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(buf.getvalue())
            os.replace(tmp, self.xlsx_path)
        except Exception:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass
            raise

    def save(self, df=None):
        """寫 state (json), 可選同時寫 df (xlsx)"""
        with self._lock:
            self.state['last_save'] = datetime.now().isoformat()
            self._atomic_write_json()
            if df is not None:
                self._atomic_write_xlsx(df)

    def mark_done(self, stage: str, item_key, save_immediately: bool = False, df=None):
        """標記某個 item 在某 stage 已完成"""
        with self._lock:
            self.state['stage_progress'].setdefault(stage, {})[str(item_key)] = 'completed'
        if save_immediately:
            self.save(df=df)

    def mark_failed(self, stage: str, item_key):
        """標記失敗 (resume 時會重跑)"""
        with self._lock:
            self.state['stage_progress'].setdefault(stage, {})[str(item_key)] = 'failed'

    def is_done(self, stage: str, item_key) -> bool:
        with self._lock:
            return self.state['stage_progress'].get(stage, {}).get(str(item_key)) == 'completed'

    def get_done_count(self, stage: str) -> int:
        with self._lock:
            return sum(1 for v in self.state['stage_progress'].get(stage, {}).values() if v == 'completed')

    def filter_pending(self, stage: str, items, key_fn=lambda x: x):
        """過濾掉已 completed 的 items, 回 [(原 idx, item)] 給 pending 列表"""
        with self._lock:
            done = self.state['stage_progress'].get(stage, {})
        return [(i, item) for i, item in enumerate(items)
                if done.get(str(key_fn(item))) != 'completed']

    def start_stage(self, stage: str, df=None):
        with self._lock:
            self.state['current_stage'] = stage
        self.save(df=df)

    def complete_stage(self, stage: str, df=None):
        with self._lock:
            if stage not in self.state['completed_stages']:
                self.state['completed_stages'].append(stage)
            self.state['current_stage'] = None
            # 不刪 stage_progress, 全部跑完才刪 (用戶要求保留)
        self.save(df=df)

    def is_stage_done(self, stage: str) -> bool:
        with self._lock:
            return stage in self.state['completed_stages']

    def mark_paused(self, reason: str, df=None):
        with self._lock:
            self.state['paused'] = True
            self.state['paused_at'] = datetime.now().isoformat()
            self.state['paused_reason'] = reason
        self.save(df=df)

    def clear_paused(self):
        with self._lock:
            self.state['paused'] = False
            self.state['paused_at'] = None
            self.state['paused_reason'] = None
        self.save()

    def cleanup(self):
        """全部完成後刪 _checkpoint.* (用戶要求只在全部跑完才刪)"""
        for p in [self.json_path, self.xlsx_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    # 給 GUI 看的摘要
    def summary(self) -> dict:
        with self._lock:
            cur = self.state['current_stage']
            done_in_cur = sum(1 for v in self.state['stage_progress'].get(cur, {}).values()
                              if v == 'completed') if cur else 0
            return {
                'run_id': self.state['run_id'],
                'completed_stages': list(self.state['completed_stages']),
                'current_stage': cur,
                'done_in_current': done_in_cur,
                'paused': self.state.get('paused', False),
                'paused_at': self.state.get('paused_at'),
                'paused_reason': self.state.get('paused_reason'),
                'last_save': self.state.get('last_save'),
            }
