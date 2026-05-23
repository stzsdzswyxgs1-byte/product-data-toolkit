"""
節日標籤選擇器 — 從 v2.csv (hashtag hotness) + 節日字典挑出最佳 tag

主要 API:
    load_hotness(csv_path) → dict {tag: hotness_dict}
    pick_seasonal_tag(festival_key, hotness, exclude=set()) → str | None
    get_seasonal_boost(category_path, original_tags, today, hotness) → list[str]
"""
import csv
import os
import re
from datetime import date
from typing import Dict, List, Optional, Set

from .calendar import get_active_festivals
from .relevance import is_gift_suitable


# ─── 節日候選 tag — 保守版 ───
# 原則: 只用「節日名本身」+「明確的送禮意圖」. 砍掉所有促銷感/通用詞.
#   ✅ 留: 母親節, 送媽媽, 父親節, 對戒, 傳家寶 (節日明確 + 物品具體)
#   ❌ 砍: 便宜賣, 超低價, 限時下殺, 特賣, 優惠 (促銷感)
#   ❌ 砍: 禮物, 送禮, 送禮自用 (太通用)
#   ❌ 砍: 招財, 吉祥, 開運, 紅包, 交換禮物 (調性偏低端/儀式)
#
# 商品分類差異 (古董 vs 茶具 vs 飾品) 不再分 pool, 全店一律保守.
# 將來若拓品類 (3C/服飾) 想要「便宜賣」這類詞, 再開 pool.
FESTIVAL_TAG_CANDIDATES: Dict[str, List[str]] = {
    'mom':           ['母親節', '送媽媽', '媽媽禮物', '母親節禮物'],
    'dad':           ['父親節', '送爸爸', '爸爸禮物', '父親節禮物'],
    'cny':           ['春節', '新年', '過年', '新春', '傳家寶'],  # 古董線特用「傳家寶」
    'valentine':     ['情人節', '送女友', '送男友', '對戒'],
    'qixi':          ['七夕', '情人節', '對戒', '送女友', '送男友'],
    'mid_autumn':    ['中秋', '中秋節', '茶禮', '團圓'],
    'dragon_boat':   ['端午', '端午節'],
    'christmas':     ['聖誕', '聖誕節', '送女友', '送男友'],
    'teachers_day':  ['教師節', '送老師'],
    'childrens_day': [],  # 古董線不適合兒童節
    'qingming':      ['清明', '傳家寶'],
    'lantern':       ['元宵'],
    'ghost':         [],  # 中元節跳過
    'womens_day':    ['婦女節'],
    'double11':      ['雙11'],            # 只留節日名本身, 不加任何促銷詞
    'double12':      ['雙12'],
    'national_day':  ['雙10', '紀念幣'],
    'new_year':      ['元旦', '新年'],
    'halloween':     [],  # 古董線跳過
}


# Yahoo hashtag regex (跟 sanitize_yahoo_hashtag 一致)
_YAHOO_HASHTAG_RE = re.compile(r'^(?!\d+$)[\u4E00-\u9FA5a-zA-Z0-9]+$')


def _is_valid_yahoo_tag(tag: str) -> bool:
    if not tag or len(tag) > 16:
        return False
    return bool(_YAHOO_HASHTAG_RE.match(tag))


def load_hotness(csv_path: Optional[str] = None) -> Dict[str, dict]:
    """
    載入 hashtag hotness CSV (v2 格式, 11 欄).
    回傳 {tag: {score, buy, likes, cvr_per_mille, total_products, yahoo_rank}}
    若檔案不存在, 回傳空 dict (不會拋例外).
    """
    if csv_path is None:
        csv_path = os.path.join(os.path.dirname(__file__), 'hotness_latest.csv')
    if not os.path.exists(csv_path):
        return {}
    out = {}
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            tag = row.get('tag', '').strip()
            if not tag:
                continue
            try:
                out[tag] = {
                    'heat_rank': int(row.get('heat_rank', 9999)),
                    'score': float(row.get('score', 0)),
                    'buy': int(row.get('buy', 0)),
                    'likes': int(row.get('likes', 0)),
                    'bids': int(row.get('bids', 0)),
                    'cvr_per_mille': float(row.get('cvr_per_mille', 0)),
                    'total_products': int(row.get('total_products', 0) or 0),
                    'yahoo_rank': int(row.get('yahoo_rank', 9999) or 9999),
                }
            except (ValueError, TypeError):
                continue
    return out


def pick_seasonal_tag(festival_key: str,
                      hotness: Optional[Dict[str, dict]] = None,
                      exclude: Optional[Set[str]] = None,
                      category_path: str = '') -> Optional[str]:
    """
    從節日候選 tag 中挑第一個合格的.
    按 FESTIVAL_TAG_CANDIDATES 順序 (我們手動 curate, 第一個最重要).
    必須通過 Yahoo regex + 不在 exclude.

    hotness 參數已棄用 (保留以向後相容), 不再參考外部熱度數據.
    """
    if exclude is None:
        exclude = set()
    candidates = FESTIVAL_TAG_CANDIDATES.get(festival_key, [])
    if not candidates:
        return None

    valid = [t for t in candidates if _is_valid_yahoo_tag(t) and t not in exclude]
    return valid[0] if valid else None


def get_seasonal_boost(category_path: str,
                       original_tags: List[str],
                       today: Optional[date] = None,
                       hotness: Optional[Dict[str, dict]] = None,
                       max_seasonal: int = 1) -> List[str]:
    """
    主要對外 API. 接收商品類別 + AI 生的 5 標籤, 回傳「節日加持後」的 5 標籤.

    流程:
        1. 拿今日活躍節日 (依 priority × weight 排序)
        2. 對每個節日, 檢查商品是否適合 (is_gift_suitable)
        3. 適合的話, 從 FESTIVAL_TAG_CANDIDATES 挑一個 tag
        4. 取代 original_tags 中最後 1 個
        5. 最多注入 max_seasonal 個節日 tag (預設 1)

    參數:
        category_path: 商品的 拍賣類別名稱
        original_tags: AI 生的 5 個 tag (順序代表優先度, 第一個最重要)
        today: 用哪天判斷節日 (None = 今天)
        hotness: 棄用參數 (向後相容用), 不再參考
        max_seasonal: 最多塞幾個節日 tag (預設 1)

    回傳: 處理後的 tag list (長度 == len(original_tags))
    """
    if not original_tags:
        return original_tags

    # hotness 已棄用, 不再 load (省 disk I/O)
    active = get_active_festivals(today=today)
    if not active:
        return list(original_tags)

    # 已注入的節日 tag, 避免重複
    injected: List[str] = []
    exclude = set(original_tags)

    for entry in active:
        if len(injected) >= max_seasonal:
            break
        fest = entry['festival']
        if not is_gift_suitable(category_path, fest.key):
            continue
        tag = pick_seasonal_tag(fest.key, hotness, exclude=exclude, category_path=category_path)
        if tag:
            injected.append(tag)
            exclude.add(tag)

    if not injected:
        return list(original_tags)

    # 注入策略 (硬限制 Yahoo 上限 5):
    #   total <= 5 → append (保留全部 + 加節日)
    #   total > 5  → 取前 (5 - len(injected)) 個原 tag, 接節日
    # 永不超過 5 個
    n_keep = max(0, 5 - len(injected))
    return list(original_tags[:n_keep]) + injected
