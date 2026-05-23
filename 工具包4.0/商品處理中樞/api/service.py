"""規則快取 + processor 純函數封裝層。

設計原則:
  - 啟動時延遲載入(lazy);第一次 call 才 load,避免 server 啟動慢
  - 每次 call 前檢查規則檔 mtime,變了就 reload(熱更新但不寫盤)
  - 全部走現有 processors/*.load_X 函數,不複製邏輯
  - 純函數 in/out,不 mutate 任何全局,thread-safe
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# api/ 父目錄 = 商品處理中樞 根
HUB_DIR = Path(__file__).resolve().parent.parent
if str(HUB_DIR) not in sys.path:
    sys.path.insert(0, str(HUB_DIR))


# ────────────────────── 配置讀取(read-only) ──────────────────────

def _hub_config() -> Dict[str, Any]:
    """讀 hub config.json,**不寫盤、不改記憶體**。"""
    cfg_path = HUB_DIR / "config.json"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve_path(rel: str) -> Optional[Path]:
    """config.paths 裡的相對路徑 → 絕對路徑(以 HUB_DIR 為基準)。"""
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = HUB_DIR / rel
    return p if p.exists() else None


# ────────────────────── 規則快取 ──────────────────────

class _MtimeCache:
    """單檔 mtime-aware 快取。檔案變了自動 reload。"""

    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._path: Optional[Path] = None
        self._value: Any = None

    def get(self, path: Optional[Path]) -> Any:
        if path is None:
            return None
        try:
            cur_mtime = path.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if self._value is not None and self._path == path and self._mtime == cur_mtime:
                return self._value
            try:
                value = self._loader(str(path))
            except Exception as e:
                # loader 失敗:回傳 None,讓 endpoint 報 503
                self._value = None
                return None
            self._value = value
            self._path = path
            self._mtime = cur_mtime
            return value


# 各規則的 loader(走 processors 既有函數,不複製邏輯)

def _load_keywords(path: str):
    from processors.keyword_filter import load_keywords
    return load_keywords(path, log_fn=lambda m: None)


def _load_replacements(path: str):
    from processors.text_replacer import load_replacements
    return load_replacements(path, log_fn=lambda m: None)


def _load_mercari_mapping(path: str):
    from processors.category_mapper import load_mercari_mapping
    return load_mercari_mapping(path, log_fn=lambda m: None)


def _load_goofish_mapping(path: str):
    from processors.category_mapper import load_goofish_mapping
    return load_goofish_mapping(path, log_fn=lambda m: None)


def _load_full_mercari(path: str):
    from processors.auto_mapper import load_full_mercari
    return load_full_mercari(path, log_fn=lambda m: None)


def _load_yahoo_categories(path: str):
    from processors.auto_mapper import load_yahoo_categories
    return load_yahoo_categories(path, log_fn=lambda m: None)


_keyword_cache = _MtimeCache("keyword", _load_keywords)
_replace_cache = _MtimeCache("replace", _load_replacements)
_mercari_map_cache = _MtimeCache("mercari_map", _load_mercari_mapping)
_goofish_map_cache = _MtimeCache("goofish_map", _load_goofish_mapping)
_full_mercari_cache = _MtimeCache("full_mercari", _load_full_mercari)
_yahoo_cats_cache = _MtimeCache("yahoo_cats", _load_yahoo_categories)


# ────────────────────── 對外服務函數 ──────────────────────

def keyword_check_batch(items: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    """批次違規詞檢查。

    items: [{title, desc?}, ...]
    回 (status, body)
      body = {results: [{i, hit_keyword, exclude_matched, verdict}], total, hits}
    """
    cfg = _hub_config()
    kw_path = _resolve_path(cfg.get("paths", {}).get("keyword_xlsx", ""))
    if kw_path is None:
        return 503, {"error": "keyword_xlsx 未配置或檔案不存在",
                       "hint": "config.json paths.keyword_xlsx"}

    kw_list = _keyword_cache.get(kw_path)
    if kw_list is None:
        return 503, {"error": "load_keywords 失敗"}

    results = []
    hits = 0
    for i, item in enumerate(items or []):
        title = str(item.get("title") or "").strip()
        if not title:
            results.append({"i": i, "verdict": "skip",
                              "hit_keyword": None, "reason": "empty title"})
            continue
        hit_kw = None
        exclude_matched = None
        for kw, excludes in kw_list:
            if kw in title:
                if excludes and any(ex in title for ex in excludes):
                    exclude_matched = next((ex for ex in excludes if ex in title), None)
                    continue  # 豁免
                hit_kw = kw
                break
        if hit_kw:
            hits += 1
            results.append({"i": i, "verdict": "violation",
                              "hit_keyword": hit_kw, "exclude_matched": None})
        else:
            results.append({"i": i, "verdict": "pass",
                              "hit_keyword": None,
                              "exclude_matched": exclude_matched})
    return 200, {
        "total": len(items or []),
        "hits": hits,
        "rule_count": len(kw_list),
        "results": results,
    }


def auto_mapper_infer(cat_ids: List[int],
                      threshold: float = 0.0) -> Tuple[int, Dict[str, Any]]:
    """批次未映射分類推斷(走 processors/auto_mapper.auto_map_missing)。

    cat_ids: [int, ...]  目前實作支援煤爐 cat_id (auto_map_missing 限制)
    threshold: confidence 過濾閾值,0=不過濾
    回 (status, body)
      body = {results: [{mercari_id, yahoo_id, yahoo_path, confidence, ...}], ...}
    """
    if not cat_ids:
        return 200, {"results": [], "total": 0}

    cfg = _hub_config()
    paths = cfg.get("paths", {})
    full_path = _resolve_path(paths.get("full_mercari_xlsx", ""))
    yahoo_path = _resolve_path(paths.get("yahoo_cat_xlsx", ""))
    api_cfg = cfg.get("api", {}) or {}

    if full_path is None:
        return 503, {"error": "full_mercari_xlsx 未配置",
                       "hint": "config.paths.full_mercari_xlsx 需指向煤爐分類完整.xlsx"}
    if yahoo_path is None:
        return 503, {"error": "yahoo_cat_xlsx 未配置",
                       "hint": "config.paths.yahoo_cat_xlsx 需指向奇摩分類.xlsx"}
    if not (api_cfg.get("translate_key") or api_cfg.get("seo_key") or api_cfg.get("api_key")):
        return 503, {"error": "API key 未配置(config.api),GPT 二級匹配需要"}

    try:
        from processors.auto_mapper import auto_map_missing
    except Exception as e:
        return 503, {"error": f"auto_mapper import 失敗: {e}"}

    try:
        ids_int = []
        for cid in cat_ids:
            try:
                ids_int.append(int(cid))
            except (ValueError, TypeError):
                continue
        df = auto_map_missing(ids_int, str(full_path), str(yahoo_path),
                              api_cfg, log_fn=lambda m: None)
    except Exception as e:
        return 500, {"error": f"auto_map_missing crashed: {type(e).__name__}: {e}"}

    results = []
    if df is not None and len(df) > 0:
        for _, row in df.iterrows():
            try:
                mid = int(row.get("煤爐ID", 0))
                yid = str(row.get("奇摩ID", "") or "").strip()
                ypath = str(row.get("奇摩分類", "") or "").strip()
                conf_raw = row.get("信心度", row.get("confidence", 0))
                try:
                    conf = float(conf_raw) if conf_raw is not None else 0.0
                except Exception:
                    conf = 0.0
                if threshold and conf < threshold:
                    continue
                results.append({
                    "mercari_id": mid,
                    "yahoo_id": yid,
                    "yahoo_path": ypath,
                    "confidence": conf,
                    "method": "bm25+gpt",
                })
            except Exception:
                continue
    return 200, {
        "input_count": len(cat_ids),
        "matched_count": len(results),
        "threshold": threshold,
        "results": results,
    }


def health_snapshot() -> Dict[str, Any]:
    """server 自身存活探測。回各規則檔的可用狀態,不觸發 reload。"""
    cfg = _hub_config()
    paths = cfg.get("paths", {})

    def _stat(rel: str) -> Dict[str, Any]:
        if not rel:
            return {"configured": False, "exists": False}
        p = _resolve_path(rel)
        if p is None:
            return {"configured": True, "exists": False, "path": rel}
        try:
            mt = p.stat().st_mtime
            sz = p.stat().st_size
            return {"configured": True, "exists": True,
                      "path": str(p), "size": sz, "mtime": mt}
        except OSError:
            return {"configured": True, "exists": False, "path": str(p)}

    return {
        "hub_dir": str(HUB_DIR),
        "config_loaded": bool(cfg),
        "rules": {
            "keyword_xlsx": _stat(paths.get("keyword_xlsx", "")),
            "replace_xlsx": _stat(paths.get("replace_xlsx", "")),
            "mercari_mapping": _stat(paths.get("mapping_xlsx", "")),
            "goofish_mapping": _stat(paths.get("goofish_mapping_xlsx", "")),
            "full_mercari": _stat(paths.get("full_mercari_xlsx", "")),
            "yahoo_cats": _stat(paths.get("yahoo_cat_xlsx", "")),
            "number_removal": _stat(paths.get("number_removal_xlsx", "")),
            "full_removal": _stat(paths.get("full_removal_xlsx", "")),
        },
        "seo_tool_dir": paths.get("seo_tool_dir", ""),
    }
