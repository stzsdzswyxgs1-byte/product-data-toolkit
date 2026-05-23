"""
商品 × 節日 相關性閘 — 避免亂塞節日 tag 造成不相關流量

核心邏輯:
    is_gift_suitable(category_path, festival_key) → bool
        category_path: e.g. '古董、藝術與礦石 > 玉石 > 古玉 > 配件'
        festival_key:  e.g. 'mom', 'dad', 'cny'

設計原則:
    - 寧可漏掉節日, 不可亂塞 (false positive 比 false negative 傷)
    - prefix match (子類別繼承父類別)
"""
from typing import Dict, List


# 節日 → 適合的類別前綴清單
# 用 prefix 比對, 越具體越優先
CATEGORY_FESTIVAL_MAP: Dict[str, List[str]] = {
    # 母親節: 女性飾品/玉/翡翠/銀器/茶具/化妝鏡
    'mom': [
        '手錶與飾品配件 > 女性流行飾品',
        '手錶與飾品配件 > 化妝包',
        '古董、藝術與礦石 > 玉石',
        '古董、藝術與礦石 > 翡翠',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具 > 銀器',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具 > 金器',
        '古董、藝術與礦石 > 琉璃/水晶/玻璃',
        '居家、家具與園藝 > 廚房鍋具/餐具/用品 > 茶具/茶盤',
    ],

    # 父親節: 紫砂壺/銀器/古董/字畫/相機/打火機/酒器/文房
    'dad': [
        '居家、家具與園藝 > 廚房鍋具/餐具/用品 > 茶具/茶盤 > 中式',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具',
        '古董、藝術與礦石 > 文獻收藏品',
        '古董、藝術與礦石 > 文房四寶',
        '古董、藝術與礦石 > 木雕藝術品',
        '古董、藝術與礦石 > 民俗古早收藏',
        '相機、攝影與周邊 > 鏡頭',
        '相機、攝影與周邊 > 相機',
        '收藏品 > 打火機',
        '古董、藝術與礦石 > 其他',
    ],

    # 春節: 古董/字畫/銀器/金飾/招財/錢幣 (傳承送禮)
    'cny': [
        '古董、藝術與礦石',
        '偶像、球員卡與郵幣 > 錢幣',
        '偶像、球員卡與郵幣 > 鈔票',
        '手錶與飾品配件 > 女性流行飾品 > 項鍊',
        '居家、家具與園藝 > 廚房鍋具/餐具/用品 > 茶具/茶盤 > 中式',
    ],

    # 情人節 / 七夕: 戒指/項鍊/玉鐲/翡翠
    'valentine': [
        '手錶與飾品配件 > 女性流行飾品 > 戒指',
        '手錶與飾品配件 > 女性流行飾品 > 項鍊',
        '手錶與飾品配件 > 女性流行飾品 > 手鍊',
        '手錶與飾品配件 > 男性流行飾品',
        '古董、藝術與礦石 > 翡翠',
        '古董、藝術與礦石 > 玉石',
    ],
    'qixi': [
        '手錶與飾品配件 > 女性流行飾品 > 戒指',
        '手錶與飾品配件 > 女性流行飾品 > 項鍊',
        '手錶與飾品配件 > 女性流行飾品 > 手鍊',
        '古董、藝術與礦石 > 翡翠',
        '古董、藝術與礦石 > 玉石',
    ],

    # 中秋: 茶具/銀器/月餅相關
    'mid_autumn': [
        '居家、家具與園藝 > 廚房鍋具/餐具/用品 > 茶具/茶盤',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具 > 銀器',
        '古董、藝術與礦石 > 瓷器',
    ],

    # 端午: 中藥香囊/銅器
    'dragon_boat': [
        '古董、藝術與礦石 > 民俗古早收藏',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具',
    ],

    # 聖誕節: 對戒/銀飾/交換禮物
    'christmas': [
        '手錶與飾品配件 > 女性流行飾品 > 戒指',
        '手錶與飾品配件 > 女性流行飾品 > 項鍊',
        '古董、藝術與礦石 > 金/銀/銅/鐵金屬器具 > 銀器',
        '古董、藝術與礦石 > 琉璃/水晶/玻璃',
    ],

    # 教師節: 鋼筆/文房
    'teachers_day': [
        '古董、藝術與礦石 > 文房四寶',
        '古董、藝術與礦石 > 文獻收藏品',
    ],

    # 兒童節: 童玩/卡帶/球員卡
    'childrens_day': [
        '偶像、球員卡與郵幣 > 偶像/球員卡',
        '偶像、球員卡與郵幣 > 卡帶',
        '玩具、模型與公仔',
    ],

    # 清明節: 古錢幣/香爐 (傳承)
    'qingming': [
        '偶像、球員卡與郵幣 > 錢幣/古錢幣',
        '古董、藝術與礦石 > 民俗古早收藏',
    ],

    # 元宵 / 中元 / 教師節 / 婦女節 等小節 — 普適性低, 設空 list 表示不主推
    'lantern': [],
    'ghost': [],
    'womens_day': [
        '手錶與飾品配件 > 女性流行飾品',
    ],

    # 雙11/雙12: 全品類購物節, 不挑類別 (用 '*' 表示)
    'double11': ['*'],
    'double12': ['*'],

    # 國慶/元旦/萬聖節: 跟你古董線不太合, 設空
    'national_day': [
        '偶像、球員卡與郵幣 > 錢幣/古錢幣 > 紀念幣',
    ],
    'new_year': [],
    'halloween': [],
}


def is_gift_suitable(category_path: str, festival_key: str) -> bool:
    """
    判斷該商品 (依其 拍賣類別名稱) 是否適合該節日當禮物 / 主題.

    參數:
        category_path: 完整類別路徑, e.g. '古董、藝術與礦石 > 玉石 > 古玉'
        festival_key:  節日 key, e.g. 'mom'

    回傳:
        True  = 適合, 可以注入該節日 tag
        False = 不適合, 跳過該節日
    """
    if not category_path or not festival_key:
        return False

    prefixes = CATEGORY_FESTIVAL_MAP.get(festival_key)
    if prefixes is None:
        return False
    if prefixes == ['*']:
        return True  # 全品類節日 (e.g. 雙11)
    if not prefixes:
        return False  # 空 list = 不主推

    # prefix match (容忍大小寫無關緊要, 但這裡保持原樣)
    return any(category_path.startswith(p) for p in prefixes)


def get_relevance_score(category_path: str, festival_key: str) -> float:
    """
    回傳相關度分數 0~1.
    完全 prefix match 得 1.0, 不符合 0.0.
    支援將來擴充為加權分數.
    """
    if not is_gift_suitable(category_path, festival_key):
        return 0.0
    prefixes = CATEGORY_FESTIVAL_MAP.get(festival_key, [])
    if prefixes == ['*']:
        return 0.5  # 全品類較通用, 分數低於精準 match
    # 找最長匹配 (越具體分數越高)
    matches = [p for p in prefixes if category_path.startswith(p)]
    if not matches:
        return 0.0
    longest = max(len(p) for p in matches)
    return min(1.0, longest / 30.0)  # 30 字以上認為高精準
