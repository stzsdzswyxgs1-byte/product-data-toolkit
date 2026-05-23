# -*- coding: utf-8 -*-
"""
價格轉換處理器
- 煤爐: 日元 ÷ 除數 → 基礎價 → 梯度倍率 → TWD
- 鹹魚: 人民幣 → 基礎價 → 梯度倍率 → TWD
- 超出範圍的標記過濾
"""
import pandas as pd
from typing import Callable, List
from processors.utils import append_reason

LogFn = Callable[[str], None]

# 默認梯度 (可被 config 覆蓋)
DEFAULT_TIERS = [
    {'min': 10000, 'max': 999999, 'multiplier': 8},
    {'min': 5000,  'max': 9999,   'multiplier': 9},
    {'min': 2000,  'max': 4999,   'multiplier': 9.5},
    {'min': 500,   'max': 1999,   'multiplier': 10},
    {'min': 100,   'max': 499,    'multiplier': 12},
    {'min': 70,    'max': 99,     'multiplier': 13},
    {'min': 50,    'max': 69,     'multiplier': 14},
    {'min': 40,    'max': 49,     'multiplier': 17},
]


def _build_anchors(tiers: list, curve_hold: float) -> list:
    """
    從梯度表構建錨點列表 [(base, twd), ...]
    每個梯度產生兩個錨點:
      1. 起點: min × multiplier
      2. 保持點: 在該段 curve_hold 位置仍用原倍率
    從保持點到下一段起點之間線性過渡, 消除跳崖
    curve_hold: 0~1, 倍率保持的比例 (0.7 = 前70%保持, 後30%過渡)
    """
    sorted_tiers = sorted(tiers, key=lambda t: t['min'])
    anchors = []
    for i, tier in enumerate(sorted_tiers):
        # 錨點1: 梯度起點
        anchors.append((tier['min'], tier['min'] * tier['multiplier']))
        # 錨點2: 保持點 (在到下一梯度起點之間的 curve_hold 位置)
        if i < len(sorted_tiers) - 1:
            next_min = sorted_tiers[i + 1]['min']
        else:
            next_min = tier['max']
        hold_x = tier['min'] + (next_min - tier['min']) * curve_hold
        anchors.append((hold_x, hold_x * tier['multiplier']))
    # 保證單調遞增
    for i in range(1, len(anchors)):
        if anchors[i][1] < anchors[i - 1][1]:
            anchors[i] = (anchors[i][0], anchors[i - 1][1])
    return anchors


def tier_price(base_price: float, tiers: list = None, floor: int = 700,
               curve_hold: float = 0.7) -> int:
    """
    根據梯度錨點線性內插計算TWD價格
    每段倍率保持 curve_hold 比例後平滑過渡到下一段, 無跳崖
    curve_hold: 0.0=純線性(中段偏低) ~ 1.0=幾乎不過渡(接近舊版)
    """
    if tiers is None:
        tiers = DEFAULT_TIERS
    anchors = _build_anchors(tiers, max(0.0, min(1.0, curve_hold)))
    if not anchors:
        return floor
    # 低於最低錨點 → 兜底價
    if base_price <= anchors[0][0]:
        return floor
    # 高於最高錨點 → 按最高梯度的倍率延伸
    if base_price >= anchors[-1][0]:
        last_tier = sorted(tiers, key=lambda t: t['min'])[-1]
        return int(base_price * last_tier['multiplier'])
    # 線性內插
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= base_price <= x1:
            ratio = (base_price - x0) / (x1 - x0) if x1 != x0 else 0
            result = int(y0 + ratio * (y1 - y0))
            return max(result, floor)
    return floor


def apply_price_conversion(df: pd.DataFrame,
                           source_type: str,
                           price_config: dict,
                           log_fn: LogFn = print) -> dict:
    """
    轉換起標價
    source_type: 'mercari' 或 'goofish'
    price_config: config.json 中的 price 區段
    返回統計 dict
    """
    divisor = price_config.get('mercari_divisor', 20)
    min_price = price_config.get('min_price', 15)
    max_price = price_config.get('max_price', 80000)
    tiers = price_config.get('tiers', DEFAULT_TIERS)
    floor = price_config.get('floor_price', 700)
    curve_hold = price_config.get('curve_hold', 0.7)

    stats = {'converted': 0, 'filtered_low': 0, 'filtered_high': 0, 'invalid': 0}

    if '起標價' not in df.columns:
        log_fn("[警告] 無起標價欄位, 跳過價格轉換")
        return stats

    for idx in df.index:
        raw = df.at[idx, '起標價']
        try:
            price = float(str(raw))
        except (ValueError, TypeError):
            df.at[idx, '_filter_reason'] = append_reason(
                df.at[idx, '_filter_reason'], '價格無效')
            stats['invalid'] += 1
            continue

        # 煤爐要先除以除數 (日元→人民幣等值)
        base = price / divisor if source_type == 'mercari' else price

        # 範圍過濾
        if base < min_price:
            df.at[idx, '_filter_reason'] = append_reason(
                df.at[idx, '_filter_reason'], f'價格過低({base:.0f})')
            stats['filtered_low'] += 1
            continue
        if base > max_price:
            df.at[idx, '_filter_reason'] = append_reason(
                df.at[idx, '_filter_reason'], f'價格過高({base:.0f})')
            stats['filtered_high'] += 1
            continue

        # 梯度轉換
        twd = tier_price(base, tiers, floor, curve_hold)
        df.at[idx, '起標價'] = twd
        stats['converted'] += 1

    total_filtered = stats['filtered_low'] + stats['filtered_high'] + stats['invalid']
    log_fn(f"價格轉換完成: 成功{stats['converted']} | "
           f"過低{stats['filtered_low']} | 過高{stats['filtered_high']} | "
           f"無效{stats['invalid']}")
    return stats
