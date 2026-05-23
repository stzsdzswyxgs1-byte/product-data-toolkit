"""
Yahoo TW 拍賣 季節/節日標籤動態系統

公開 API:
    from seasonal import get_seasonal_boost
    boosted = get_seasonal_boost(category_path, original_tags, today=None)
"""
from .calendar import get_active_festivals, FESTIVALS
from .relevance import is_gift_suitable, get_relevance_score, CATEGORY_FESTIVAL_MAP
from .tags import load_hotness, pick_seasonal_tag, get_seasonal_boost, FESTIVAL_TAG_CANDIDATES

__all__ = [
    'get_active_festivals', 'FESTIVALS',
    'is_gift_suitable', 'get_relevance_score', 'CATEGORY_FESTIVAL_MAP',
    'load_hotness', 'pick_seasonal_tag', 'get_seasonal_boost', 'FESTIVAL_TAG_CANDIDATES',
]
