# -*- coding: utf-8 -*-
"""
去重處理器 (2026-04-27 改版)
1. 同商品條碼: 整件刪除重複 (保留第一件) — 防止同商品被採集多次重複上架
2. 同標題不同條碼: 加 (2)(3) 後綴 — 同款不同實物
3. 異常標題: nan/純(N)/賣家分組詞 → 標記不合格
"""
import re
import pandas as pd
from collections import defaultdict
from typing import Callable

LogFn = Callable[[str], None]

# 賣家分組標籤詞 (不是商品名, 應過濾)
SELLER_GROUP_WORDS = {
    '粉粉', '粉橘色', '橘色', '二級', '一級', '特價',
    '免運', '包郵', '280元包郵', '低價清理', '清倉',
    '隨意挑', '自選', '看圖',
}

# 純單字「商品類別」也應視為無效 (沒有區分度, 主詞防撞池等問題)
SINGLE_WORD_BLACKLIST = {
    '木頭', '銅壺', '面具', '花瓶', '碗', '盤', '杯', '罐',
    '銀勺', '木雕', '陶瓷', '銅器',
}


def _is_anomaly_title(title: str) -> tuple:
    """檢查是否異常標題, 返回 (是否異常, 原因)"""
    t = (title or '').strip()
    if not t or t.lower() == 'nan':
        return True, '無有效標題(nan/空)'
    # 純 (N) 後綴
    if re.fullmatch(r'\s*\(\d+\)\s*', t):
        return True, '無有效標題(純後綴)'
    # 賣家分組標籤
    t_norm = re.sub(r'\s+', '', t)
    if t_norm in SELLER_GROUP_WORDS:
        return True, f'賣家分組標籤({t_norm})'
    # 純單字商品類別
    if t_norm in SINGLE_WORD_BLACKLIST:
        return True, f'單字過短缺資訊({t_norm})'
    # 標題只有 1-3 字 (沒空格)
    if len(t_norm) <= 3 and ' ' not in t and '\u3000' not in t:
        return True, f'標題過短({len(t_norm)}字)'
    return False, ''


def apply_dedup(df: pd.DataFrame, log_fn: LogFn = print) -> dict:
    """
    新去重邏輯:
    1. 條碼重複: 標 _filter_reason='重複條碼' (保留第一件)
    2. 標題重複但條碼不同: 加 (N) 後綴
    3. 異常標題: 標 _filter_reason='無有效標題' 或 '賣家分組標籤'
    """
    stats = {'total': 0, 'code_duplicated': 0, 'title_suffixed': 0, 'anomaly': 0}

    if '標題' not in df.columns:
        log_fn("[警告] 無標題欄位, 跳過去重")
        return stats

    if '_filter_reason' not in df.columns:
        df['_filter_reason'] = ''

    # ─── Step 1: 條碼去重 (保留第一件, 後續標不合格) ───
    code_seen = set()
    code_col = '商品條碼' if '商品條碼' in df.columns else None

    for idx in df.index:
        stats['total'] += 1
        title = str(df.at[idx, '標題']) if pd.notna(df.at[idx, '標題']) else ''

        # 異常標題檢查
        is_anomaly, reason = _is_anomaly_title(title)
        if is_anomaly:
            existing = str(df.at[idx, '_filter_reason'] or '').strip()
            df.at[idx, '_filter_reason'] = f'{existing}|{reason}'.strip('|') if existing else reason
            stats['anomaly'] += 1

        # 條碼重複檢查
        if code_col:
            code = str(df.at[idx, code_col]).strip()
            if code and code != 'nan':
                if code in code_seen:
                    existing = str(df.at[idx, '_filter_reason'] or '').strip()
                    new_reason = f'重複條碼({code})'
                    df.at[idx, '_filter_reason'] = f'{existing}|{new_reason}'.strip('|') if existing else new_reason
                    stats['code_duplicated'] += 1
                else:
                    code_seen.add(code)

    # ─── Step 2: 標題去重 (同標題但不同條碼才加 (N) 後綴) ───
    # 只對「未被標不合格」的行做標題後綴
    counter = defaultdict(int)
    new_titles = []

    for idx in df.index:
        # 跳過已標不合格的
        existing_reason = str(df.at[idx, '_filter_reason'] or '').strip()
        if existing_reason:
            new_titles.append((idx, str(df.at[idx, '標題']) if pd.notna(df.at[idx, '標題']) else ''))
            continue

        title = str(df.at[idx, '標題']) if pd.notna(df.at[idx, '標題']) else ''
        clean_title = re.sub(r'\s*\(\d+\)$', '', title)
        key = re.sub(r'\s+', '', clean_title).lower()
        counter[key] += 1

        if counter[key] > 1:
            new_titles.append((idx, f"{clean_title} ({counter[key]})"))
            stats['title_suffixed'] += 1
        else:
            new_titles.append((idx, clean_title))

    for idx, title in new_titles:
        df.at[idx, '標題'] = title

    log_fn(
        f"去重完成: 共{stats['total']}條 | "
        f"異常標題{stats['anomaly']} | "
        f"重複條碼{stats['code_duplicated']} | "
        f"同標題不同條碼後綴{stats['title_suffixed']}"
    )
    return stats
