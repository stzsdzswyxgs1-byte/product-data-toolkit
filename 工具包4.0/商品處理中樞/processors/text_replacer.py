# -*- coding: utf-8 -*-
"""
替換詞處理器
讀取 替換詞.xlsx, 對標題和說明做文本替換
"""
import pandas as pd
from typing import Callable, List, Tuple

LogFn = Callable[[str], None]


def load_replacements(xlsx_path: str, log_fn: LogFn = print) -> List[Tuple[str, str]]:
    """
    讀取 替換詞.xlsx, 返回 [(原來詞, 更新詞), ...]
    必需欄位: 原來詞, 更新詞
    """
    log_fn(f"讀取替換詞: {xlsx_path}")
    df = pd.read_excel(xlsx_path, engine='openpyxl')

    if not {'原來詞', '更新詞'}.issubset(df.columns):
        raise ValueError("替換詞.xlsx 必須包含欄位: 原來詞, 更新詞")

    pairs = []
    for _, row in df.iterrows():
        old = row['原來詞']
        new = row['更新詞']
        if pd.notna(old) and str(old).strip():
            pairs.append((str(old), str(new) if pd.notna(new) else ''))

    log_fn(f"替換詞載入完成: {len(pairs)} 條規則")
    return pairs


def apply_replacements(df: pd.DataFrame, pairs: List[Tuple[str, str]],
                       log_fn: LogFn = print) -> dict:
    """
    對 標題 和 說明 欄位做替換
    返回統計 dict
    """
    stats = {'title_hits': 0, 'desc_hits': 0}

    for col, stat_key in [('標題', 'title_hits'), ('說明', 'desc_hits')]:
        if col not in df.columns:
            continue
        for idx in df.index:
            val = df.at[idx, col]
            if pd.isna(val):
                continue
            text = str(val)
            changed = False
            for old, new in pairs:
                if old in text:
                    text = text.replace(old, new)
                    changed = True
            if changed:
                df.at[idx, col] = text
                stats[stat_key] += 1

    log_fn(f"替換完成: 標題{stats['title_hits']}處 | 說明{stats['desc_hits']}處")
    return stats
