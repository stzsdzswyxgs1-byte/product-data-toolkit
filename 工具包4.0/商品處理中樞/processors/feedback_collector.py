# -*- coding: utf-8 -*-
"""Feedback collector — 收 LaMa 結果 + batch summary 給 admin Claude review.

設計原則:
1. 全 best-effort, 永遠不 block 主跑批流程
2. 跑批中事件累積到 self._events (in-memory list)
3. 批次結束時一次性 POST 到 /feedback/lama_summary
4. Push 失敗就 silent log warn, 用戶完全無感

Schema 跟 API server 對齊 (見 contract):
{
  "ts": ISO8601,
  "tg": str,
  "batch": str (batch_<YYYYMMDD>_<HHMMSS>),
  "bc": str,
  "idx": int,
  "stage_e_dec": "reject" / "keep",
  "lama_out": "rescued" / "rollback_residue" / "ai_locate_fail" / "no_watermark" / "not_attempted",
  "lama_ms": int,
  "ai_locate_calls": int,
  "verify_calls": int,
  "input_hash": str (16 hex),       # LaMa 看到的原始 jpg bytes hash (跨 stage 不一致)
  "output_hash": str (16 hex),      # LaMa 處理後 jpg bytes hash
  "stage_e_hash": str (16 hex),     # ★ 4.0.21: Stage E 視角 hash (resize 1024+q85), admin 用這個找 D:/images
}
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

FEEDBACK_ENDPOINT = 'https://api.example.com/feedback/lama_summary'
DETAIL_LOG_ENDPOINT = 'https://api.example.com/feedback/detail_log'  # ★ 4.0.43
KEY = '<TEST_API_KEY>'
PUSH_TIMEOUT = 15
PUSH_RETRIES = 2
DETAIL_LOG_PUSH_TIMEOUT = 30  # detail log 大些, 30s

_collector_lock = threading.Lock()
_collector_instance = None


def get_collector():
    """全局 singleton."""
    global _collector_instance
    with _collector_lock:
        if _collector_instance is None:
            _collector_instance = FeedbackCollector()
        return _collector_instance


def reset_collector():
    """測試用. 清掉 singleton."""
    global _collector_instance
    with _collector_lock:
        _collector_instance = None


class FeedbackCollector:
    """跑批時收 LaMa events. 批次結束 push 一次."""

    def __init__(self):
        self._events = []   # list of dicts
        self._lock = threading.Lock()
        self._tg_id = None
        self._batch_id = None
        self._enabled = True

    def configure(self, tg_id: str, batch_id: Optional[str] = None):
        """跑批開始時設定 context."""
        with self._lock:
            self._tg_id = str(tg_id) if tg_id else ''
            if batch_id:
                self._batch_id = batch_id
            elif not self._batch_id:
                # 自動生成 batch_<YYYYMMDD>_<HHMMSS>
                now = datetime.datetime.now()
                self._batch_id = f'batch_{now.strftime("%Y%m%d_%H%M%S")}'

    def get_batch_id(self) -> str:
        with self._lock:
            if not self._batch_id:
                self.configure(tg_id=self._tg_id or '0', batch_id=None)
            return self._batch_id

    def get_tg_id(self) -> str:
        with self._lock:
            return self._tg_id or ''

    def disable(self):
        """測試用 / config 關掉時. 之後 record_lama 不收, push 不發."""
        with self._lock:
            self._enabled = False

    def record_lama(self,
                    bc: str, idx: int,
                    stage_e_dec: str,
                    lama_out: str,
                    lama_ms: int = 0,
                    ai_locate_calls: int = 0,
                    verify_calls: int = 0,
                    input_hash: str = '',
                    output_hash: str = '',
                    stage_e_hash: str = '',
                    # ★ 4.0.22 診斷 metadata (給 admin Claude review):
                    watermarks: list = None,
                    watermark_area: float = 0.0,
                    skipped_center: int = 0,
                    verify_remaining: str = '',
                    # ★ 4.0.53 confidence 欄位 (給 admin priority_review 排序)
                    locate_confidence: float = 1.0,
                    verify_confidence: float = 1.0,
                    ):
        """跑 LaMa 時 (image_dewatermark.py 內) 每張處理過的圖呼一次.

        ★ 4.0.21: stage_e_hash 跟 API 端 D:\\images 對齊 (resize 1024+q85+sha256[:16]),
          給 admin Claude review 時直接找圖. input_hash 仍是 LaMa 視角原圖 hash.
        ★ 4.0.22: 新加 K0 診斷 metadata —
          - watermarks: AI 定位結果 [{type, bbox}, ...] 給 admin 看 AI 看到什麼
          - watermark_area: 0-1, 水印總面積比例 (大水印 LaMa 救不回的 indicator)
          - skipped_center: 商品中心保護擋掉的 bbox 數
          - verify_remaining: K0_verify AI 判髒時的殘留描述 (rollback 時這欄非空)
          - admin 一查 lama event 就直接看到 K0 的全套判定理由, 不用 cross-ref request-logs
        """
        if not self._enabled:
            return
        evt = {
            'ts': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'tg': self.get_tg_id(),
            'batch': self.get_batch_id(),
            'bc': str(bc),
            'idx': int(idx),
            'stage_e_dec': stage_e_dec,
            'lama_out': lama_out,
            'lama_ms': int(lama_ms),
            'ai_locate_calls': int(ai_locate_calls),
            'verify_calls': int(verify_calls),
            'input_hash': input_hash,
            'output_hash': output_hash,
            'stage_e_hash': stage_e_hash,
            # 4.0.22 診斷
            'watermarks': watermarks if watermarks else [],
            'watermark_area': float(watermark_area or 0.0),
            'skipped_center': int(skipped_center or 0),
            'verify_remaining': str(verify_remaining or ''),
            # ★ 4.0.53 confidence (admin priority_review 排序)
            'locate_confidence': float(locate_confidence or 0.0),
            'verify_confidence': float(verify_confidence or 0.0),
        }
        with self._lock:
            self._events.append(evt)

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def drain(self) -> list:
        """取出全部 events 清空 buffer (回傳 list of dict)."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
            return out

    def push(self, log_fn=None) -> bool:
        """批次結束時呼叫. 把累積 events POST 到 /feedback/lama_summary.
        Best-effort, 失敗只 log 不 raise.
        返回是否成功 push.
        """
        events = self.drain()
        if not events:
            return True
        if not self._enabled:
            return True
        body_lines = [json.dumps(e, ensure_ascii=False) for e in events]
        body = '\n'.join(body_lines).encode('utf-8')
        if len(body) > 9 * 1024 * 1024:
            # 9 MB safety limit (server limit 10 MB)
            if log_fn:
                log_fn(f'[feedback] body 超過 9 MB ({len(body)/1024/1024:.1f} MB), 砍半 push',
                       'warn')
            half = len(events) // 2
            self._events = events[half:]   # 暫存後半, 下次再 push (其實也 drain 了)
            events = events[:half]
            body = '\n'.join(json.dumps(e, ensure_ascii=False) for e in events).encode('utf-8')

        for attempt in range(PUSH_RETRIES):
            try:
                req = urllib.request.Request(
                    FEEDBACK_ENDPOINT,
                    data=body,
                    method='POST',
                    headers={
                        'Authorization': f'Bearer {KEY}',
                        'Content-Type': 'application/x-ndjson',
                        'User-Agent': 'Mozilla/5.0 toolkit-feedback/4.0',
                    },
                )
                with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as r:
                    if r.status == 200:
                        if log_fn:
                            log_fn(f'[feedback] push 成功: {len(events)} 個 LaMa events')
                        return True
                    else:
                        if log_fn:
                            log_fn(f'[feedback] push status={r.status}, 重試 {attempt+1}/{PUSH_RETRIES}',
                                   'warn')
            except urllib.error.HTTPError as e:
                if log_fn:
                    log_fn(f'[feedback] push HTTP {e.code}: {e.reason}, attempt {attempt+1}', 'warn')
            except urllib.error.URLError as e:
                if log_fn:
                    log_fn(f'[feedback] push 網路錯: {e.reason}, attempt {attempt+1}', 'warn')
            except Exception as e:
                if log_fn:
                    log_fn(f'[feedback] push exception {type(e).__name__}: {str(e)[:80]}', 'warn')
            if attempt < PUSH_RETRIES - 1:
                time.sleep(2)
        if log_fn:
            log_fn(f'[feedback] push 最終失敗 ({len(events)} events 丟了, 不影響跑批)', 'warn')
        return False


    def push_detail_log(self, log_path: str, log_fn=None) -> bool:
        """★ 4.0.43: batch 結束時把 詳細日誌_<ts>.txt 上傳到 admin.
        gzip 壓縮 (~70% 壓縮率) + base64 + POST /feedback/detail_log.
        Best-effort, 失敗不影響跑批.

        body: {tg, batch, ts, original_size, gz_b64}
        """
        if not log_path or not self._enabled:
            return False
        try:
            if not os.path.isfile(log_path):
                return False
            with open(log_path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            if log_fn:
                log_fn(f'[feedback-log] 讀檔失敗: {e}', 'warn')
            return False
        if not raw:
            return False
        # gzip 壓縮
        try:
            import gzip, base64
            gz = gzip.compress(raw, compresslevel=6)
            gz_b64 = base64.b64encode(gz).decode('ascii')
        except Exception as e:
            if log_fn:
                log_fn(f'[feedback-log] gzip 失敗: {e}', 'warn')
            return False
        # 太大砍 (>5MB compressed = ~25MB raw, 不正常 batch)
        if len(gz) > 5 * 1024 * 1024:
            if log_fn:
                log_fn(f'[feedback-log] 壓縮後仍 >5MB ({len(gz)/1024/1024:.1f}MB), 不上傳', 'warn')
            return False
        # ★ 4.0.44 Bug 3: filename 中文會被 server 端 regex strip 成「____」
        # client 端先轉 ASCII safe (保留 timestamp + 副檔名)
        orig_name = os.path.basename(log_path)
        try:
            orig_name.encode('ascii')
            ascii_name = orig_name
        except UnicodeEncodeError:
            # 「詳細日誌_20260510_163119.txt」→「detail_20260510_163119.txt」
            import re as _re
            m = _re.search(r'(\d{8}_\d{6})\.([a-zA-Z0-9]+)$', orig_name)
            if m:
                ascii_name = f'detail_{m.group(1)}.{m.group(2)}'
            else:
                ascii_name = 'detail_' + ''.join(c if c.isascii() and c.isalnum() or c in '._-' else '_' for c in orig_name)
        body_obj = {
            'tg': self.get_tg_id(),
            'batch': self.get_batch_id(),
            'ts': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'original_size': len(raw),
            'compressed_size': len(gz),
            'filename': ascii_name,
            'gz_b64': gz_b64,
        }
        body = json.dumps(body_obj, ensure_ascii=False).encode('utf-8')
        for attempt in range(PUSH_RETRIES):
            try:
                req = urllib.request.Request(
                    DETAIL_LOG_ENDPOINT,
                    data=body,
                    method='POST',
                    headers={
                        'Authorization': f'Bearer {KEY}',
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0 toolkit-feedback/4.0',
                    },
                )
                with urllib.request.urlopen(req, timeout=DETAIL_LOG_PUSH_TIMEOUT) as r:
                    if r.status == 200:
                        if log_fn:
                            log_fn(f'[feedback-log] push 詳細日誌成功: '
                                   f'{len(raw)/1024:.0f}KB → {len(gz)/1024:.0f}KB (gzip)')
                        return True
            except urllib.error.HTTPError as e:
                if log_fn:
                    log_fn(f'[feedback-log] HTTP {e.code}: {e.reason}, attempt {attempt+1}', 'warn')
            except urllib.error.URLError as e:
                if log_fn:
                    log_fn(f'[feedback-log] 網路錯: {e.reason}, attempt {attempt+1}', 'warn')
            except Exception as e:
                if log_fn:
                    log_fn(f'[feedback-log] exception {type(e).__name__}: {str(e)[:80]}', 'warn')
            if attempt < PUSH_RETRIES - 1:
                time.sleep(2)
        if log_fn:
            log_fn(f'[feedback-log] 詳細日誌 push 最終失敗, 不影響跑批', 'warn')
        return False


# ────────────────────────────────────────────────────────────────
# X-Trace header helpers
# ────────────────────────────────────────────────────────────────

def make_xtrace_single(stage: str, bc: str, idx: int, retry: int = 0,
                       hub_ver: Optional[str] = None) -> str:
    """單張圖請求的 X-Trace header.

    stage ∈ {stage_c, stage_k0_locate, stage_k0_verify, stage_k_edit}
    """
    coll = get_collector()
    parts = [
        'v1',
        f'tg={coll.get_tg_id()}',
        f'batch={coll.get_batch_id()}',
        f'stage={stage}',
        f'bc={bc}',
        f'idx={idx}',
        f'retry={retry}',
    ]
    if hub_ver:
        parts.append(f'hub_ver={hub_ver}')
    return '|'.join(parts)


def make_xtrace_batch(stage: str, items: list, retry: int = 0,
                      hub_ver: Optional[str] = None) -> str:
    """Stage E batch 請求的 X-Trace header.

    items: [(bc, idx), (bc, idx), ...]
        順序對應 messages[].content[].image_url 順序.
    """
    coll = get_collector()
    if not items:
        return ''
    imgs_str = ','.join(f'{bc}:{idx}' for bc, idx in items)
    parts = [
        'v1',
        f'tg={coll.get_tg_id()}',
        f'batch={coll.get_batch_id()}',
        f'stage={stage}',
        f'imgs={imgs_str}',
        f'retry={retry}',
    ]
    if hub_ver:
        parts.append(f'hub_ver={hub_ver}')
    return '|'.join(parts)


def hash_bytes(data: bytes) -> str:
    """SHA256 前 16 hex (跟 API server 對齊)."""
    return hashlib.sha256(data).hexdigest()[:16]


def hash_stage_e_view(img_path: str, max_dim: int = 1024, quality: int = 85) -> str:
    """算「Stage E 攔截端看到的 hash」— 跟 seo_v64_full._img_to_b64_compressed
    完全相同 algorithm: resize max_dim + JPEG quality + optimize, 算 sha256 前 16 hex.

    用這個 hash 可以在 D:/toolkit-feedback/images/<prefix>/<hash>.jpg 找到圖.
    跟 hub LaMa input_hash 不同 — LaMa hash 是原始 jpg bytes hash, 跨 stage 不一致.

    回傳 '' 若 file 不存在或讀取失敗 (best-effort, 永不 raise).
    """
    try:
        if not img_path or not os.path.isfile(img_path):
            return ''
        # ★ 4.0.71: 應用 EXIF orientation 跟 Stage E/K0 一致 (這個 hash 用於 admin_review 對齊圖檔)
        from PIL import Image, ImageOps
        from io import BytesIO
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, 'JPEG', quality=quality, optimize=True)
        return hash_bytes(buf.getvalue())
    except Exception:
        return ''
