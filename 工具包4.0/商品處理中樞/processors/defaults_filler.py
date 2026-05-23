# -*- coding: utf-8 -*-
"""
默認值填充處理器
根據來源填充: 所在地/商品類型/商品狀況/交貨方式/付款方式/出貨日期/上架類型/數量
煤爐商品狀況支援 condition mapping (日文→中文)
"""
import pandas as pd
from typing import Callable

LogFn = Callable[[str], None]

# 煤爐商品狀況映射 (condition欄位值→Yahoo格式)
MERCARI_CONDITION_MAP = {
    '全新，未使用': '全新品',
    '新品、未使用': '全新品',
    '近全新':       '二手品',
    '未使用に近い': '二手品',
    '無明顯傷痕或污漬': '二手品',
    '目立った傷や汚れなし': '二手品',
    '略有傷痕或污漬': '二手品',
    'やや傷や汚れあり': '二手品',
    '有傷痕或污漬':   '二手品',
    '傷や汚れあり':   '二手品',
    '整體狀況較差':   '二手品',
    '全体的に状態が悪い': '二手品',
}


def apply_defaults(df: pd.DataFrame,
                   source_type: str,
                   defaults_config: dict,
                   force_overwrite: bool = True,
                   log_fn: LogFn = print) -> dict:
    """
    填充默認值
    force_overwrite=True (原始模式): 全部覆蓋
    force_overwrite=False (已處理模式): 只填空值, 有值的跳過
    """
    stats = {'filled': 0, 'skipped': 0}
    cfg = defaults_config

    # 確保欄位存在
    for col in ['所在地', '商品類型', '商品狀況', '交貨方式', '付款方式',
                '出貨日期', '上架類型', '數量']:
        if col not in df.columns:
            df[col] = ''

    for idx in df.index:
        # 已處理模式: 有值就跳過
        if not force_overwrite:
            existing_loc = str(df.at[idx, '所在地']).strip() if pd.notna(df.at[idx, '所在地']) else ''
            if existing_loc and existing_loc != '' and existing_loc != 'nan':
                stats['skipped'] += 1
                continue

        # 數量
        df.at[idx, '數量'] = cfg.get('quantity', 1)

        # 所在地
        df.at[idx, '所在地'] = cfg.get('location', '台北市')

        # 商品類型
        df.at[idx, '商品類型'] = cfg.get('product_type', '直購商品')

        # 商品狀況
        cond_mode = cfg.get('condition', '二手品')
        if cond_mode == 'mapping' and source_type == 'mercari':
            # 用商品簡述映射
            brief = str(df.at[idx, '商品簡述']) if pd.notna(df.at[idx, '商品簡述']) else ''
            mapped = MERCARI_CONDITION_MAP.get(brief.strip(), '二手品')
            df.at[idx, '商品狀況'] = mapped
        else:
            df.at[idx, '商品狀況'] = cond_mode

        # 交貨方式
        df.at[idx, '交貨方式'] = cfg.get('shipping', '套用全店運費設定')

        # 付款方式
        df.at[idx, '付款方式'] = cfg.get('payment', '')

        # 出貨日期
        df.at[idx, '出貨日期'] = cfg.get('ship_date', '現貨商品')

        # 上架類型
        df.at[idx, '上架類型'] = cfg.get('listing_type', '立即刊登')

        stats['filled'] += 1

    # 商品簡述截取70字
    if '商品簡述' in df.columns:
        df['商品簡述'] = df['商品簡述'].apply(
            lambda x: str(x)[:70] if pd.notna(x) else '')

    if stats['skipped']:
        log_fn(f"默認值填充完成: 填充{stats['filled']}行, 跳過{stats['skipped']}行(已有值)")
    else:
        log_fn(f"默認值填充完成: {stats['filled']} 行")
    return stats
