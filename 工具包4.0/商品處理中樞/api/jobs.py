"""ETL 非同步 job manager — 單 active job 機制。

設計原則:
  - threading.Lock + 單一 active job dict,P0 拒絕第 2 個 concurrent
  - job thread 共用 server 啟動時的 sys.path / cwd,不每 request toggle
  - Pipeline.stop() 走原本的 _stop flag(rule 1: 不改 pipeline.py)
  - log_fn 拆解 step 進度,寫到 in-memory job state
  - 結束狀態保留 1 小時(查詢用),超過自動 GC
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .service import HUB_DIR

# ────────────────────── step 中文名 → 字母對照 ──────────────────────
# pipeline.py 內 _progress() 傳的是中文名
_STEP_NAME_TO_LETTER = {
    "分類": "A_category",
    "價格": "B_price",
    "翻譯": "C_translate",
    "替換詞": "D_replace",
    "價格清洗": "E_price_clean",
    "SEO": "F_seo",
    "關鍵詞": "G_keyword",
    "默認值": "H_defaults",
    "AI過濾": "H_ai_filter",  # pipeline.py 內也叫 Step H,共用
    "去重": "I_dedup",
    "說明模板": "J_desc_append",
}

# 正則:從 pipeline log 抓 SEO/翻譯內部進度 "[stage] 100/489 (20%)"
_INNER_PROGRESS_RE = re.compile(r"\[(\w+)\]\s*(\d+)\s*/\s*(\d+)\s*\((\d+)%\)")

# 結束後保留 1 小時供查詢
_KEEP_DONE_SEC = 3600.0


# ────────────────────── Job 狀態 ──────────────────────

class _JobState:
    __slots__ = ("job_id", "status", "started_at", "ended_at",
                  "current_step", "step_progress", "log_tail",
                  "result", "error", "input_path", "source_type",
                  "_pipeline_ref", "_thread_ref")

    def __init__(self, job_id: str, input_path: str, source_type: str):
        self.job_id = job_id
        self.status = "queued"            # queued | running | done | failed | cancelled
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.current_step: Optional[str] = None
        self.step_progress: Dict[str, Dict[str, Any]] = {}
        self.log_tail: List[str] = []
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.input_path = input_path
        self.source_type = source_type
        self._pipeline_ref = None
        self._thread_ref = None

    def to_public(self) -> Dict[str, Any]:
        elapsed = (self.ended_at or time.time()) - self.started_at
        out = {
            "job_id": self.job_id,
            "status": self.status,
            "input_path": self.input_path,
            "source_type": self.source_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_sec": round(elapsed, 1),
            "current_step": self.current_step,
            "step_progress": self.step_progress,
            "log_tail": self.log_tail[-30:],  # 只回最近 30 行
        }
        if self.result:
            out.update({
                "good_path": self.result.get("good_path", ""),
                "bad_path": self.result.get("bad_path", ""),
                "uncat_path": self.result.get("uncat_path", ""),
                "good_count": self.result.get("good_count", 0),
                "bad_count": self.result.get("bad_count", 0),
                "uncat_count": self.result.get("uncat_count", 0),
                "stats": self.result.get("stats", {}),
            })
        if self.error:
            out["error"] = self.error
        return out


# ────────────────────── Manager(單 active) ──────────────────────

_lock = threading.Lock()
_active_job_id: Optional[str] = None
_jobs: Dict[str, _JobState] = {}


def _gc():
    """過期 job GC(只清 done/failed/cancelled,running 永不清)。"""
    now = time.time()
    expired = [jid for jid, j in _jobs.items()
                if j.status in ("done", "failed", "cancelled")
                and j.ended_at and (now - j.ended_at) > _KEEP_DONE_SEC]
    for jid in expired:
        _jobs.pop(jid, None)


def submit(input_path: str, source_type: str,
           output_dir: str = "", output_name: str = "",
           steps_override: Optional[Dict[str, bool]] = None
           ) -> Dict[str, Any]:
    """嘗試啟動 ETL job。回 {ok, job_id?, error?}。

    若已有 active job,回 409 (in caller)。
    """
    global _active_job_id
    with _lock:
        _gc()
        if _active_job_id is not None:
            return {"ok": False, "code": 409,
                      "error": "已有 active job 在跑,P0 限單 active",
                      "active_job_id": _active_job_id}

        job_id = uuid.uuid4().hex[:12]
        st = _JobState(job_id, input_path, source_type)
        _jobs[job_id] = st
        _active_job_id = job_id

    # thread 在 lock 外啟動(避免 lock 內做 I/O)
    t = threading.Thread(
        target=_runner,
        args=(job_id, input_path, source_type, output_dir, output_name, steps_override),
        daemon=True,
        name=f"hub-etl-{job_id}",
    )
    st._thread_ref = t
    t.start()
    return {"ok": True, "job_id": job_id, "started_at": st.started_at}


def get_status(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        st = _jobs.get(job_id)
        if st is None:
            return None
        return st.to_public()


def cancel(job_id: str) -> Dict[str, Any]:
    with _lock:
        st = _jobs.get(job_id)
        if st is None:
            return {"ok": False, "error": "job_id 不存在"}
        if st.status not in ("queued", "running"):
            return {"ok": False, "error": f"狀態 {st.status} 已結束,無法取消"}
        if st._pipeline_ref is None:
            return {"ok": False, "error": "Pipeline 尚未初始化,稍後再試"}
        try:
            st._pipeline_ref.stop()
            return {"ok": True, "job_id": job_id, "msg": "已發送停止信號"}
        except Exception as e:
            return {"ok": False, "error": f"stop() 失敗: {e}"}


def list_active() -> Dict[str, Any]:
    with _lock:
        _gc()
        return {
            "active_job_id": _active_job_id,
            "all_jobs": [j.to_public() for j in _jobs.values()],
        }


# ────────────────────── 內部 runner ──────────────────────

def _runner(job_id: str, input_path: str, source_type: str,
             output_dir: str, output_name: str,
             steps_override: Optional[Dict[str, bool]]):
    global _active_job_id
    st = _jobs.get(job_id)
    if st is None:
        return

    try:
        st.status = "running"

        # 動態 import — 走 hub 既有 pipeline.py(不改它)
        from pipeline import Pipeline, load_config

        cfg = load_config()
        # 套 daemon 給的 override(不寫盤)
        if output_dir:
            cfg["output_dir"] = output_dir
        if output_name:
            cfg["output_name"] = output_name
        if steps_override:
            steps_cfg = cfg.setdefault("steps", {})
            for k, v in steps_override.items():
                if k in steps_cfg and isinstance(steps_cfg[k], dict):
                    steps_cfg[k]["enabled"] = bool(v)

        # log_fn:抓行尾 + 進度解析
        def _capture_log(msg: str):
            try:
                line = str(msg).strip()
                if not line:
                    return
                st.log_tail.append(line)
                if len(st.log_tail) > 200:
                    st.log_tail = st.log_tail[-200:]
                # 抓內部 progress: SEO/翻譯會吐 "[stage] 100/489 (20%)"
                m = _INNER_PROGRESS_RE.search(line)
                if m and st.current_step:
                    sp = st.step_progress.setdefault(st.current_step, {})
                    sp.update({
                        "stage": m.group(1),
                        "current": int(m.group(2)),
                        "total": int(m.group(3)),
                        "pct": int(m.group(4)),
                    })
            except Exception:
                pass

        pipeline = Pipeline(cfg, log_fn=_capture_log)
        st._pipeline_ref = pipeline

        # progress_fn 由 pipeline 在每 step 開始時呼叫
        def _on_step(name: str, current: int, total: int):
            letter = _STEP_NAME_TO_LETTER.get(name, name)
            # 上一個 step 標 done(若有)
            if st.current_step and st.current_step != letter:
                prev = st.step_progress.setdefault(st.current_step, {})
                prev["done"] = True
            st.current_step = letter
            sp = st.step_progress.setdefault(letter, {})
            sp.update({"started": True, "step_index": current, "step_total": total})

        result = pipeline.run(input_path, source_type, progress_fn=_on_step)

        # 最後一個 step 標 done
        if st.current_step:
            st.step_progress.setdefault(st.current_step, {})["done"] = True

        # 把 pipeline.stats 合併進 step_progress(每 step 的數字結果)
        stats = result.get("stats", {}) if isinstance(result, dict) else {}
        for stkey, val in stats.items():
            # stats key 是 'category'/'price'/... 要對應 step letter
            for letter_key in st.step_progress.keys():
                if letter_key.endswith("_" + stkey) or letter_key.lower().endswith(stkey.lower()):
                    if isinstance(val, dict):
                        st.step_progress[letter_key].update(val)
                    break

        st.result = result
        st.status = "done"
        st.ended_at = time.time()

    except KeyboardInterrupt:
        st.status = "cancelled"
        st.ended_at = time.time()
    except Exception as e:
        # 包括 pipeline.py raise 的 InterruptedError("用戶中止處理")
        if isinstance(e, InterruptedError) or "中止" in str(e):
            st.status = "cancelled"
        else:
            st.status = "failed"
            st.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
        st.ended_at = time.time()
    finally:
        with _lock:
            if _active_job_id == job_id:
                _active_job_id = None
