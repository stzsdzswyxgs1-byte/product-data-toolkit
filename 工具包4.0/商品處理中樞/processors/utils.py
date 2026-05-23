# -*- coding: utf-8 -*-
"""
處理器共用工具函數
"""
import pandas as pd


# ─── 4.0.45: Shared HTTP Session (TLS handshake reuse) ───
# 高 RTT (300-400ms) 下 TLS handshake 一次 ~3 RTT = ~1s. 每個 request 重 handshake 浪費.
# Module-level Session: 8-16 個並發 thread 共用 connection pool, handshake 只第一次跑.
# 對 71-85 件 batch (image_opt) 預估省 ~15% wall time (省 60-100s).
# requests.Session 本身 thread-safe (內部用 urllib3 連線池, 各 thread 自己拿 connection).
import requests as _requests

_SHARED_SESSION = None


def get_shared_session():
    """全 process 共用 requests.Session (TLS keep-alive).

    第一次呼叫 lazy 建立, 之後重用.
    Adapter 設大一點 pool_maxsize=64 cover 並發 cap (image_opt max=48, K0/Stage E 也 ~48).
    """
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        s = _requests.Session()
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=64, pool_block=False)
            s.mount('https://', adapter)
            s.mount('http://', adapter)
        except Exception:
            pass  # adapter 失敗也 OK, session 仍能用
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 toolkit-hub/4.0',
        })
        _SHARED_SESSION = s
    return _SHARED_SESSION


def append_reason(current, reason: str) -> str:
    """追加過濾原因"""
    if pd.isna(current) or not str(current).strip():
        return reason
    return f"{current}|{reason}"


def is_network_blip(exc, elapsed: float = 0, err_code=None, status_code=None) -> bool:
    """區分純網路層中斷 (VPN 抖 / CF Tunnel / urllib3 連不上) vs 真實 API 失敗.

    純網路層 → return True → caller 不該通報 APIMonitor (避免 VPN 抖 8 次誤觸發 PauseException).

    ★ 4.0.30: 從 image_optimizer 抽到共用 utils, seo_v64 / image_dewatermark 也能用.
       原本 4.0.24 在 checkpoint_manager.classify_response 加的 fix, 因為 checkpoint_manager.py
       不在 product_hub whitelist 永遠到不了客戶端 → 4.0.27 把 fix 下放到 image_optimizer caller-side
       但只 cover image_opt, 沒 cover seo_v64 → 4.0.30 統一抽到 utils 三邊都 import.
    ★ 4.0.73: 加 status_code 參數 — CF 524 / 502 / 504 是 CF/Gateway 層切連線, 中介可能仍在跑,
       不該算真 API 失敗. 5-11 用戶撞「8 次失敗暫停」就是 524 被算 8 次真失敗 (上游帳號掉雙重作用).

    參數:
        exc: 引發失敗的 exception object
        elapsed: 從發 request 到失敗的秒數 (給 ReadTimeout 判斷用)
        err_code: 上游 error code (有 code 表示一定到中介 → 絕不是純網路層)
        status_code: HTTP status (524/502/504 = CF/Gateway 層, 算 blip)
    """
    # ★ 4.0.73: CF Tunnel / Gateway 層 status → blip (中介那邊可能還跑著)
    # 524 = CF Tunnel timeout (origin 沒在 100s 內送 byte)
    # 502 = Bad Gateway (CF 連不到 origin)
    # 504 = Gateway Timeout (CF 等 origin 超時)
    # 522 = Connection Timed Out (CF 連 origin timeout)
    # 523 = Origin is Unreachable
    if status_code in (502, 504, 522, 523, 524):
        return True
    # 雙重保險: 有 err_code 表示請求一定到了中介 (中介自己回的) → 絕不是純網路層
    # (例: upstream_error / middleware_timeout / content_policy_violation / hub_aborted)
    if err_code:
        return False
    if exc is None:
        return False
    # 用 type isinstance 避開 import 循環 (請求側 caller 用 requests 但這裡不依賴)
    exc_type_name = type(exc).__name__
    if exc_type_name in ('ChunkedEncodingError', 'ConnectionError',
                         'ConnectionResetError', 'ConnectionRefusedError',
                         'ConnectionAbortedError', 'NewConnectionError',
                         'RemoteDisconnected', 'IncompleteRead',
                         'TimeoutError', 'BadStatusLine'):
        return True
    if exc_type_name == 'ReadTimeout':
        # 中介 90s/130s 硬牆早觸發了, elapsed < 100s 表示 CF 押住 chunked, 中介應已寫完
        return elapsed < 100
    # 兜底: 訊息辨識
    msg = str(exc)
    network_blip_patterns = (
        'IncompleteRead', 'Response ended prematurely',
        'Max retries exceeded',           # urllib3 retry 用完
        'HTTPSConnectionPool', 'HTTPConnectionPool',
        'NewConnectionError',
        'ConnectionResetError', 'Connection reset',
        'ConnectionRefusedError', 'Connection refused',
        'Connection aborted',
        'RemoteDisconnected',
        'getaddrinfo failed', 'Name or service not known',
        'Temporary failure in name resolution',
        'SSL: ', 'TLSV1_ALERT',
        'BadStatusLine',
        'All API routes failed',           # ← seo_v64 的 chat fallback chain 全失敗訊息
    )
    for pat in network_blip_patterns:
        if pat in msg:
            return True
    return False


# ─── 4.0.32: RTT-aware 並發 cap ───
# 高 RTT (218ms+) 下高並發 (76/48/38) → TLS 握手堆積 → urllib3 Max retries timeout
# Hub 端啟動時測 mw_ping_ms, 自己 cap 並發 (中介 worker suggestion 沒考慮 hub→中介 RTT)
_HUB_RTT_MS = None      # 啟動時測到的 RTT
_HUB_CAP_PCT = None     # cap 比例 (0-1, None = 不 cap). 4.0.34 改 % based 平滑降


def set_hub_rtt(rtt_ms):
    """pipeline 啟動時呼叫 — 測完 mw_ping_ms 設 hub 端並發上限.

    ★ 4.0.34: 從 stepped (4 grade) 改 % based 平滑降. 之前 289ms 直接 cap 15
       (76→15 砍 5x), 用戶反饋「降一半都慢, 你直接 cap 5 倍太多」.

    新邏輯 (% of middleware suggested):
        RTT < 100ms  → 不 cap (信中介)
        RTT 100-150  → 85% (76 → 64)
        RTT 150-200  → 65% (76 → 49)
        RTT 200-250  → 50% (76 → 38, 218ms 同事用這條, 比之前 30 寬)
        RTT 250-300  → 40% (76 → 30, 289ms 用戶用這條, 比之前 15 寬 2x)
        RTT 300-400  → 30% (76 → 22)
        RTT > 400    → 20% (76 → 15, 極端 case)

    最低 cap 8 (避免 % 算下來太小).
    """
    global _HUB_RTT_MS, _HUB_CAP_PCT
    _HUB_RTT_MS = rtt_ms
    # ★ 4.0.81: RTT cap 解除 (信中介 suggestion)
    # 原 50% cap 是為了防「503 暴增 → urllib3 retry → TLS pool 爆」的鏈條,
    # 但中介 4 層防護 (cli-proxy v7.1.17 + disable-cooling + Watchdog + CF Storm remap)
    # 部署後該觸發源已大幅消除. AdaptiveCap 仍有 ramp-up + fail-rate auto-shrink 雙保險.
    # 若實戰出現 urllib3 Max retries timeout, 把這行改回原本的階梯式 % 表即可.
    _HUB_CAP_PCT = None
    try:
        print(f'[utils 4.0.81] RTT cap 已解除 (RTT={rtt_ms}ms, 信中介 suggestion)')
    except Exception:
        pass


def apply_hub_cap(suggested_workers: int, retry_round: int = 0) -> int:
    """各 stage 拿到 middleware suggested workers 後過這層 cap.

    retry_round: 第 N 輪 retry (0 = 主跑). 每多一輪砍半, 因為 retry 階段
                 通常網路層仍有問題, 大並發會一直撞牆.

    ★ 4.0.34: % based — apply_hub_cap(76) 在 289ms 回 30 (40%), 不是 15.
    """
    if _HUB_CAP_PCT is None:
        # 沒 RTT 限制 — retry 仍砍半 (避免 retry 階段又撞同款 timeout)
        if retry_round > 0:
            return max(8, suggested_workers // (2 ** retry_round))
        return suggested_workers
    cap = max(8, int(suggested_workers * _HUB_CAP_PCT))
    if retry_round > 0:
        cap = max(4, cap // (2 ** retry_round))
    return min(suggested_workers, cap)


def get_hub_cap_info() -> str:
    """給 ADMIN_FINGERPRINT / log 用 — 描述當前 cap 狀態"""
    if _HUB_CAP_PCT is None:
        return 'unlimited (RTT 健康或未測)'
    return f'pct={int(_HUB_CAP_PCT*100)}% (RTT={_HUB_RTT_MS}ms)'


# ─── 4.0.35: AdaptiveCapController ───
# 動態並發 cap — 跑批中根據 success rate 自動 ratchet up/down.
# 4.0.34 RTT cap 是 initial 起點; adaptive 在 [min, middleware_suggested] 範圍內動態調.
# 多用戶不同網路條件下, 自動找各自 sweet spot (快的拉滿, 慢的降低).
import threading as _threading
from collections import deque as _deque

_AC_CONTROLLERS = {}  # global per-stage registry, batch start reset


def reset_adaptive_caps():
    """每 batch 開始呼叫 — clear registry, 下次跑批重新 probe."""
    _AC_CONTROLLERS.clear()


class AdaptiveCapController:
    """跑批中動態 cap. 用 DynamicSemaphore (在 checkpoint_manager) 配 滑動視窗統計.

    用法:
        ac = get_or_create_adaptive_cap('image_opt', initial=38, max_cap=76)
        ac.set_log_fn(log_fn)
        def _worker(task):
            if not ac.acquire(): return None  # shutdown
            try:
                r = do_task(task)
                ac.report_success()
                return r
            except Exception as e:
                ac.report_fail(exception=e)  # 內部過濾 network_blip 不算
                raise
            finally:
                ac.release()
    """
    def __init__(self, name: str, initial_cap: int, max_cap: int = None,
                 min_cap: int = 8, window: int = 30, cooldown: int = 5):
        # ★ 4.0.42: cooldown 從 20 → 10 (砍半). 71 件 image_opt batch 跑完 cap 還在 14
        # (從 8 慢慢爬, cooldown=20 + 10% 太保守). 改 10 task 就能 ratchet, 加上「第一次大 jump」
        # 邏輯, 71 件能爬到 max ~26 而不是 14, 預期 image_opt wall time -33%.
        # ★ 4.0.83: cooldown 10 → 5. 中介 audit 顯示 hub 只用 my_rpm_share 45% (50/112 RPM), 沒撞天花板.
        # 50 件 batch cooldown=10 需 40 task 爬到 max (80% 時間沒用滿). cooldown=5 需 20 task
        # 爬完 (40% 時間沒用滿). 帳號池 8% 利用率 + cascade_pause 過濾 (4.0.81) 安全網夠.
        # 預期 wall-time +20-30% 對所有 stage (stage_c / stage_e / k0 / image_opt). 配 4.0.81 RTT cap 解除.
        self.name = name
        self.max_cap = max_cap or max(initial_cap, 8)
        self.min_cap = max(1, min_cap)
        self.cap = max(self.min_cap, min(initial_cap, self.max_cap))
        self.window_size = window
        self.cooldown = cooldown
        self.results = _deque(maxlen=window)
        self.tasks_since_last_adjust = 0
        self.adjust_count = 0  # ★ 4.0.42: 記錄 ratchet 次數, 第一次 0% fail 大 jump
        self._lock = _threading.Lock()
        self._log_fn = None
        # DynamicSemaphore 從 checkpoint_manager 借 (客戶端有 4.0.10 版本就含)
        try:
            from checkpoint_manager import DynamicSemaphore
            self._sem = DynamicSemaphore(self.cap)
        except ImportError:
            self._sem = None

    def set_log_fn(self, log_fn):
        self._log_fn = log_fn

    def acquire(self) -> bool:
        if self._sem is None:
            return True
        return self._sem.acquire()

    def release(self):
        if self._sem:
            self._sem.release()

    def shutdown(self):
        if self._sem:
            self._sem.shutdown()

    def get_cap(self) -> int:
        with self._lock:
            return self.cap

    def get_stats(self) -> dict:
        with self._lock:
            n = len(self.results)
            n_fail = sum(1 for r in self.results if not r)
            return {
                'cap': self.cap,
                'window_size': n,
                'fail_rate': n_fail / n if n else 0.0,
                'tasks_since_adjust': self.tasks_since_last_adjust,
            }

    def report_success(self):
        with self._lock:
            self.results.append(True)
            self.tasks_since_last_adjust += 1
            self._maybe_adjust()

    def report_fail(self, exception=None, error_code=None):
        """report 失敗. 內部過濾 network_blip — 純連線層不算進 fail rate
        (那類失敗降 cap 沒用, 因為 cap 再低 TLS 還是會撐爆 — 這時應該 escalate
        而不是 cap 降. 對網路抖動的處理由 record_fail_safe 上一層處理).

        ★ 4.0.81: 同時過濾 cascade_pause — 中介 CF Storm 防護期間會把上游 truncate/403
        改寫成 cascade_pause, 這是中介保險閥觸發, 不是 worker 撞牆, 不該縮 cap.
        """
        # 純連線層 blip 不算
        try:
            if is_network_blip(exception, err_code=error_code):
                return
        except Exception:
            pass
        # ★ 4.0.81: CF Storm 觸發的 cascade_pause 不算
        if error_code == 'cascade_pause':
            return
        with self._lock:
            self.results.append(False)
            self.tasks_since_last_adjust += 1
            self._maybe_adjust()

    def _maybe_adjust(self):
        # cooldown 期間不調 (避免震盪)
        if self.tasks_since_last_adjust < self.cooldown:
            return
        # 視窗未填一半也不調 (sample 太少不可信)
        # ★ 4.0.52: 小 batch (累計 < 30 task) 用較鬆條件 (5 sample 就調), 否則永遠卡起點
        #   image_opt 6-12 件 batch 之前累計 6 sample 達不到 window/2=15, ratchet 永遠不觸發.
        #   小 batch 觀察期短可接受 (反正 batch 小, 即使誤判降也只影響少數件).
        min_sample = 5 if self.tasks_since_last_adjust < 30 else self.window_size // 2
        if len(self.results) < min_sample:
            return
        n = len(self.results)
        n_fail = sum(1 for r in self.results if not r)
        rate = n_fail / n if n else 0.0
        old_cap = self.cap
        # ★ 4.0.42: 0% fail 用「先大跳, 再小步」策略, 對小 batch (<200 件) 也能爬到 max
        #   - 第一次 ratchet: cap 直接跳到 (cap+max)/2 中點 (大 probe)
        #   - 之後 ratchet: +25% (穩定爬)
        #   - fail rate >5% 觸發降速時重置 adjust_count, 之後又從大跳開始 (重新探測)
        if rate < 0.05:
            if self.adjust_count == 0:
                # 第一次穩定 → 大跳到 cap 跟 max 的中點
                target = (self.cap + self.max_cap) // 2
                new_cap = min(self.max_cap, max(self.cap + 4, target))
            else:
                # 之後 +25% (至少 +3)
                new_cap = min(self.max_cap, max(self.cap + 3, int(self.cap * 1.25)))
        elif rate < 0.15:
            return  # 健康範圍, 不動
        elif rate < 0.30:
            new_cap = max(self.min_cap, int(self.cap * 0.80))
            self.adjust_count = -1  # ★ 4.0.42: 觸發降速 → 之後 +1 = 0, 重置大跳計數
        else:
            # 大爛 → 急降一半
            new_cap = max(self.min_cap, int(self.cap * 0.50))
            self.adjust_count = -1
        if new_cap == old_cap:
            return
        self.cap = new_cap
        self.adjust_count += 1
        if self._sem:
            self._sem.set_target(new_cap)
        self.tasks_since_last_adjust = 0
        if self._log_fn:
            try:
                trend = '↑' if new_cap > old_cap else '↓'
                self._log_fn(f'  [AdaptiveCap:{self.name}] {trend} '
                             f'{old_cap}→{new_cap} (fail {rate:.0%} in last {n} task)')
            except Exception:
                pass


def get_or_create_adaptive_cap(name: str, initial_cap: int,
                                max_cap: int = None, **kwargs) -> AdaptiveCapController:
    """全局 registry. 同 name 重用 (例如 image_opt 跑批 + retry 用同個 controller)"""
    if name not in _AC_CONTROLLERS:
        _AC_CONTROLLERS[name] = AdaptiveCapController(
            name=name, initial_cap=initial_cap, max_cap=max_cap, **kwargs
        )
    return _AC_CONTROLLERS[name]


def make_adaptive_worker(controller: 'AdaptiveCapController', orig_fn, success_check=None):
    """4.0.36: 包裝 worker function, acquire → run → report success/fail → release.

    參數:
        controller: AdaptiveCapController
        orig_fn: 原 worker function
        success_check: 4.0.38 加 — 從 result 判斷是否成功. 不傳則只看 exception
                       (caller 內部 swallow 的 fail 看不到, 易讓 adaptive over-confident).
                       傳 callable(result) → bool, True = success.
                       Stage C/E/K0 內部 swallow exception 並 return 空 result, 必須提供.

    成功 = 沒拋 exception 且 success_check(result) == True
    失敗 = 拋 exception (network_blip 自動過濾) 或 success_check 回 False
    """
    if controller is None:
        return orig_fn

    def wrapped(*args, **kwargs):
        if not controller.acquire():
            return None  # shutdown
        try:
            result = orig_fn(*args, **kwargs)
            # 判 success
            is_success = True
            if success_check is not None:
                try:
                    is_success = bool(success_check(result))
                except Exception:
                    is_success = True  # check 自己崩 → 不懲罰 (預設 success)
            if is_success:
                controller.report_success()
            else:
                controller.report_fail()
            return result
        except Exception as e:
            controller.report_fail(exception=e)
            raise
        finally:
            controller.release()
    return wrapped


def record_fail_safe(monitor, status_code=None, error_code=None, error_type=None,
                     error_message=None, exception=None, elapsed: float = 0):
    """4.0.31: 智能 record_fail — 純連線層失敗 (VPN 抖 / TLS timeout) 不算 monitor.

    取代 callers 直接呼叫 monitor.record_fail(...). 5 個 caller 統一走這條:
      - seo_v64._reject_scan_one   (Stage E single)
      - seo_v64._reject_scan_batch (Stage E batch)
      - seo_v64._seo_visual_multimodal (Stage C) ← 4.0.30 漏這條, Stage C cascade
      - seo_v64._patched_chat     (Stage A unified) ← 4.0.30 已修, 仍可改用此 helper
      - image_dewatermark._ai_call (K0 locate/verify)

    image_optimizer 已有自己的 _is_network_blip 檢查, 不動.

    回傳: True 表示真通報了, False 表示判 blip 沒通報.
    """
    if not monitor:
        return False
    # ★ 4.0.73: 加 status_code 給 is_network_blip 識別 CF 524/502/504 等 gateway blip
    if is_network_blip(exception, elapsed=elapsed, err_code=error_code, status_code=status_code):
        return False  # 純連線層, 不算數
    try:
        monitor.record_fail(status_code=status_code, error_code=error_code,
                            error_type=error_type, error_message=error_message,
                            exception=exception)
        return True
    except Exception:
        # PauseException 之類仍會 raise (record_fail 內部觸發), caller 該重新 raise
        raise
