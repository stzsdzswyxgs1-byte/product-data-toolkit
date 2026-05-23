# -*- coding: utf-8 -*-
"""
煤爐 (Mercari) 數據適配器
將煤爐采集導出的 Excel 轉換為統一內部格式
支援兩種輸入: 原始export (7列) 和 已處理過的test格式 (16列)
"""
import pandas as pd
from typing import Callable

LogFn = Callable[[str], None]

# 煤爐導出的7列
MERCARI_EXPECTED_COLS = ['標題', '商品簡述', '起標價', '說明', '圖片', '連結', '分類ID']


def adapt_mercari(df: pd.DataFrame, force_raw: bool = False,
                  log_fn: LogFn = print) -> pd.DataFrame:
    """
    煤爐數據 → 統一格式 DataFrame
    force_raw=True: 強制當原始數據處理 (清空分類/默認值)
    force_raw=False: 自動檢測, 有拍賣類別則保留
    """
    log_fn(f"煤爐適配器: 輸入 {len(df)} 行, 欄位: {list(df.columns)}")
    cols = set(df.columns)

    if force_raw:
        log_fn("  模式: 原始數據 (從頭處理)")
        miss = [c for c in MERCARI_EXPECTED_COLS if c not in cols]
        if miss:
            log_fn(f"[警告] 煤爐數據缺少欄位: {miss}, 會以空值填充")
        return _adapt_raw(df, log_fn)

    # 自動檢測
    is_processed = ('拍賣類別' in cols or '拍賣類別名稱' in cols) and '連結' not in cols
    if is_processed:
        log_fn("  模式: 已處理數據 (保留現有值)")
        return _adapt_processed(df, log_fn)
    else:
        miss = [c for c in MERCARI_EXPECTED_COLS if c not in cols]
        if miss:
            log_fn(f"[警告] 煤爐數據缺少欄位: {miss}, 會以空值填充")
        return _adapt_raw(df, log_fn)


def _adapt_raw(df: pd.DataFrame, log_fn: LogFn) -> pd.DataFrame:
    """原始 export 格式: 標題/商品簡述/起標價/說明/圖片/連結/分類ID"""
    out = pd.DataFrame()
    out['標題'] = df.get('標題', pd.Series(dtype=str)).fillna('').astype(str)
    out['商品簡述'] = df.get('商品簡述', pd.Series(dtype=str)).fillna('').astype(str)
    out['起標價'] = pd.to_numeric(df.get('起標價', 0), errors='coerce').fillna(0)
    out['數量'] = 1
    out['說明'] = df.get('說明', pd.Series(dtype=str)).fillna('').astype(str)
    out['圖片'] = df.get('圖片', pd.Series(dtype=str)).fillna('').astype(str)
    out['商品條碼'] = df.get('連結', pd.Series(dtype=str)).fillna('').astype(str)

    out['_source_type'] = 'mercari'
    out['_source_category_id'] = df.get('分類ID', pd.Series(dtype=str)).fillna('').astype(str)
    out['_filter_reason'] = ''
    out['拍賣類別'] = ''
    out['拍賣類別名稱'] = ''

    return _finalize(out, log_fn)


def _adapt_processed(df: pd.DataFrame, log_fn: LogFn) -> pd.DataFrame:
    """已處理格式: 保留 商品條碼/拍賣類別/拍賣類別名稱 等現有欄位"""
    out = pd.DataFrame()
    out['標題'] = df.get('標題', pd.Series(dtype=str)).fillna('').astype(str)
    out['商品簡述'] = df.get('商品簡述', pd.Series(dtype=str)).fillna('').astype(str)
    out['起標價'] = pd.to_numeric(df.get('起標價', 0), errors='coerce').fillna(0)
    out['數量'] = pd.to_numeric(df.get('數量', 1), errors='coerce').fillna(1).astype(int)
    out['說明'] = df.get('說明', pd.Series(dtype=str)).fillna('').astype(str)
    out['圖片'] = df.get('圖片', pd.Series(dtype=str)).fillna('').astype(str)

    # 保留現有欄位
    out['商品條碼'] = df.get('商品條碼', pd.Series(dtype=str)).fillna('').astype(str)
    out['拍賣類別'] = df.get('拍賣類別', pd.Series(dtype=str)).fillna('').astype(str)
    out['拍賣類別名稱'] = df.get('拍賣類別名稱', pd.Series(dtype=str)).fillna('').astype(str)

    # 保留其他已有的默認值欄位
    for col in ['所在地', '商品類型', '商品狀況', '交貨方式', '付款方式', '出貨日期', '上架類型']:
        if col in df.columns:
            out[col] = df[col].astype(str)

    out['_source_type'] = 'mercari'
    out['_source_category_id'] = df.get('分類ID', pd.Series(dtype=str)) if '分類ID' in df.columns else ''
    out['_filter_reason'] = ''

    return _finalize(out, log_fn)


def _finalize(out: pd.DataFrame, log_fn: LogFn) -> pd.DataFrame:
    """共用的最終清理"""
    before = len(out)
    out = out[out['標題'].str.strip().astype(bool)].copy()
    dropped = before - len(out)
    if dropped:
        log_fn(f"刪除空標題行: {dropped}")
    out = out.reset_index(drop=True)
    log_fn(f"煤爐適配完成: {len(out)} 行有效數據")
    return out
