"""
Taiwan 節日日曆 — 含農曆轉換, 提前 lookahead 觸發

主要 API:
    get_active_festivals(today, lookahead=45) → [Festival, ...]
        回傳今天起 lookahead 天內活躍的節日 (按 priority 排序)
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional
from lunardate import LunarDate


@dataclass(frozen=True)
class Festival:
    key: str            # 內部 key (e.g. 'mom', 'cny')
    name: str           # 顯示名 (e.g. '母親節')
    audience: str       # 送禮對象 ('mom'/'dad'/'lover'/'family'/'all')
    priority: int       # 1-10 (10 最重要)
    lookahead_days: int # 提前幾天開始 (人們提前搜禮物)
    afterglow_days: int # 過節後幾天降權 (拖尾)

    def get_date(self, year: int) -> Optional[date]:
        """子類覆寫: 回傳該年的國曆日期"""
        raise NotImplementedError


@dataclass(frozen=True)
class GregorianFestival(Festival):
    month: int = 1
    day: int = 1

    def get_date(self, year: int) -> date:
        return date(year, self.month, self.day)


@dataclass(frozen=True)
class LunarFestival(Festival):
    """農曆節日 (春節/中秋/端午/七夕/中元)"""
    lunar_month: int = 1
    lunar_day: int = 1

    def get_date(self, year: int) -> Optional[date]:
        try:
            return LunarDate(year, self.lunar_month, self.lunar_day).toSolarDate()
        except Exception:
            return None


@dataclass(frozen=True)
class MothersDayFestival(Festival):
    """母親節: 5月第二個週日"""
    def get_date(self, year: int) -> date:
        d = date(year, 5, 1)
        # 找第一個週日
        d += timedelta(days=(6 - d.weekday()) % 7)
        # 第二個週日
        d += timedelta(days=7)
        return d


# ─── 節日清單 ───
# lookahead_days 設「真實搜尋窗」(人實際在 Yahoo 搜禮物的時間), 不是「想禮物時間」
# 大節日: 14 天 (2 週前才開始有人搜)
# 中節日: 10 天
# 小節日: 7 天
# afterglow_days: 過節後搜尋量斷崖式下降, 設 1-3 天
FESTIVALS: List[Festival] = [
    # 國曆固定日
    GregorianFestival(key='valentine', name='情人節', audience='lover', priority=8,
                      lookahead_days=10, afterglow_days=2, month=2, day=14),
    GregorianFestival(key='womens_day', name='婦女節', audience='mom', priority=4,
                      lookahead_days=7, afterglow_days=2, month=3, day=8),
    GregorianFestival(key='childrens_day', name='兒童節', audience='family', priority=4,
                      lookahead_days=10, afterglow_days=2, month=4, day=4),
    GregorianFestival(key='qingming', name='清明節', audience='family', priority=3,
                      lookahead_days=7, afterglow_days=2, month=4, day=5),
    GregorianFestival(key='dad', name='父親節', audience='dad', priority=10,
                      lookahead_days=14, afterglow_days=3, month=8, day=8),
    GregorianFestival(key='teachers_day', name='教師節', audience='all', priority=3,
                      lookahead_days=7, afterglow_days=2, month=9, day=28),
    GregorianFestival(key='national_day', name='雙10國慶', audience='all', priority=3,
                      lookahead_days=5, afterglow_days=2, month=10, day=10),
    GregorianFestival(key='halloween', name='萬聖節', audience='all', priority=2,
                      lookahead_days=7, afterglow_days=1, month=10, day=31),
    GregorianFestival(key='double11', name='雙11', audience='all', priority=10,
                      lookahead_days=14, afterglow_days=2, month=11, day=11),
    GregorianFestival(key='double12', name='雙12', audience='all', priority=8,
                      lookahead_days=10, afterglow_days=2, month=12, day=12),
    GregorianFestival(key='christmas', name='聖誕節', audience='lover', priority=8,
                      lookahead_days=14, afterglow_days=2, month=12, day=25),
    GregorianFestival(key='new_year', name='元旦', audience='all', priority=4,
                      lookahead_days=7, afterglow_days=2, month=1, day=1),
    # 農曆
    LunarFestival(key='cny', name='春節', audience='family', priority=10,
                  lookahead_days=14, afterglow_days=5, lunar_month=1, lunar_day=1),  # 春節 search 窗約 2 週
    LunarFestival(key='lantern', name='元宵節', audience='family', priority=3,
                  lookahead_days=5, afterglow_days=2, lunar_month=1, lunar_day=15),
    LunarFestival(key='dragon_boat', name='端午節', audience='family', priority=4,
                  lookahead_days=10, afterglow_days=2, lunar_month=5, lunar_day=5),
    LunarFestival(key='qixi', name='七夕', audience='lover', priority=7,
                  lookahead_days=10, afterglow_days=2, lunar_month=7, lunar_day=7),
    LunarFestival(key='ghost', name='中元節', audience='family', priority=2,
                  lookahead_days=7, afterglow_days=1, lunar_month=7, lunar_day=15),
    LunarFestival(key='mid_autumn', name='中秋節', audience='family', priority=10,
                  lookahead_days=14, afterglow_days=3, lunar_month=8, lunar_day=15),
    # 浮動日
    MothersDayFestival(key='mom', name='母親節', audience='mom', priority=10,
                       lookahead_days=14, afterglow_days=3),
]


def get_active_festivals(today: Optional[date] = None,
                         lookahead: int = 45) -> List[dict]:
    """
    回傳今日起 lookahead 天內活躍的節日.

    回傳格式: [{'festival': Festival, 'date': date, 'days_ahead': int, 'weight': float}]
    weight: 1.0 = 節日當天, 0.0 = 完全過期
    按 priority × weight 由高至低排序.
    """
    if today is None:
        today = date.today()
    results = []
    # 檢查今年 + 明年 (跨年農曆春節需要)
    for year in (today.year, today.year + 1):
        for fest in FESTIVALS:
            fest_date = fest.get_date(year)
            if fest_date is None:
                continue
            days_to = (fest_date - today).days
            # 在 lookahead 窗內 (節前) 或在 afterglow 內 (節後)
            if -fest.afterglow_days <= days_to <= max(lookahead, fest.lookahead_days):
                # 計算權重
                if days_to >= 0:
                    # 節前: 越靠近權重越高
                    if days_to > fest.lookahead_days:
                        weight = 0.0  # 太遠, lookahead 外
                    else:
                        # 線性: lookahead_days 距離 → 0.3, 0 距離 → 1.0
                        weight = 0.3 + 0.7 * (1 - days_to / fest.lookahead_days)
                else:
                    # 節後 afterglow 內: 線性遞減
                    weight = max(0.0, 1.0 + days_to / fest.afterglow_days * 0.7)
                if weight > 0:
                    results.append({
                        'festival': fest,
                        'date': fest_date,
                        'days_ahead': days_to,
                        'weight': weight,
                    })
    # 按 priority × weight 排序
    results.sort(key=lambda r: -(r['festival'].priority * r['weight']))
    return results
