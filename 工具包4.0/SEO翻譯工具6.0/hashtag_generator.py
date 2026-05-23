# -*- coding: utf-8 -*-
"""Yahoo TW 拍賣 5 組 hashtag — V42 (基於 Yahoo 官方規範 + TOP 賣家實證)

V42 修正 (2026-05-03 深度研究):
  ★ Yahoo 標籤被獨立索引 (URL ?ht=XXX 是真實 hashtag 搜尋池)
  ★ 「免運」對應 64,924 筆大池 (短 tag 才命中 autosuggest)
  ★ 不要寫「免運費可開發票」這種複合詞 (沒人搜, 浪費槽位)
  ★ 季節 tag (母親節/父親節) ±2週 內超有用, 季節外浪費

5 組策略:
  槽 1: #免運           固定 (全店免運)
  槽 2: #現貨           固定 (高購買意圖)
  槽 3: #商品核心同義詞   動態 (取自標題首詞 / 品類映射)
  槽 4: #規格賣點       動態 (從原料抽具體規格)
  槽 5: #季節 OR #第二長尾 動態 (依當月)

規範:
  - 每組 ≤ 20 字
  - 不可空格/符號 (除中英數)
  - 不可重複標題已有的詞
"""
import re
from datetime import datetime
from typing import List

BAN_WORDS = {'精美','絕版','稀有','頂級','極品','原裝','正品','保真','經典','限量','撿漏','難得','品相佳'}

# 商品核心 → 同義詞對映 (槽 3)
# 規則: 標題已有「翡翠手鐲」, 標籤就放「玉鐲」或「A貨翡翠」(同義不重複)
CAT_SYNONYM_MAP = {
    # 古董類同義
    '翡翠手鐲': '玉鐲',
    '翡翠戒指': '玉戒指',
    '翡翠吊墜': '玉墜',
    '翡翠': 'A貨翡翠',
    '和田玉': '和闐玉',
    '古玉': '老玉',
    '瑪瑙': '瑪瑙石',
    '蜜蠟': '琥珀',
    '紫砂壺': '紫砂',
    # 錢幣
    '袁大頭': '銀元',
    '光緒元寶': '清代銀幣',
    '鹹豐重寶': '清代銅錢',
    '咸豐重寶': '清代銅錢',
    '乾隆通寶': '清代古錢',
    '康熙通寶': '清代古錢',
    '雍正通寶': '清代古錢',
    '紀念幣': '紀念章',
    '紀念鈔': '紙鈔',
    '人民幣': '紙鈔',
    # 飾品
    '手錶': '腕錶',
    '項鍊': '項鏈',
    '戒指': '戒子',
    '耳環': '耳飾',
    '胸針': '別針',
    '手鐲': '腕鐲',
    '吊墜': '墜飾',
    '髮簪': '頭飾',
    # 服飾
    '洋裝': '連身裙',
    '外套': '夾克',
    'T恤': '短袖',
    '襯衫': '上衣',
    # 日本陶瓷
    '日本花瓶': '陶瓷花器',
    '日本茶碗': '抹茶碗',
    '備前燒': '日本陶器',
    '九穀燒': '日本瓷',
    # 銅器
    '古銅器': '老銅器',
    '銅佛像': '老佛像',
    # 國際品牌 (標題有就放官方/簡寫)
    'Cartier': '卡地亞',
    'Tiffany': '蒂芙尼',
    'HERMES': '愛馬仕',
    'GUCCI': '古馳',
    'Prada': '普拉達',
    'Coach': '蔻馳',
    'Rolex': '勞力士',
    'Omega': '歐米茄',
    'Patek': '百達翡麗',
    'SEIKO': '精工',
    'CITIZEN': '西鐵城',
    '浪琴': 'Longines',
    '寶珀': 'Blancpain',
    '帝陀': 'Tudor',
    'Dunhill': '登喜路',
    'VISVIM': 'visvim',
    'Carhartt': '工裝外套',
    'Meissen': '梅森',
    'Wedgwood': '威基伍德',
}


def get_season_tag() -> str:
    """季節活動 tag — 只在活動期 ±2 週內回傳, 平日空字串"""
    today = datetime.now()
    m, d = today.month, today.day

    # 活動日期 (近似)
    events = [
        (1, 20, 2, 15, '春節'),         # 1/20-2/15
        (2, 1, 2, 28, '情人節'),        # 2/14 ±2週
        (4, 25, 5, 15, '母親節'),       # 5/8 ±2週
        (5, 25, 6, 15, '618'),          # 6/18 ±2週
        (7, 20, 8, 20, '父親節七夕'),    # 8/8 ±2週
        (9, 1, 9, 25, '中秋'),
        (10, 1, 10, 31, '雙10'),
        (11, 1, 11, 15, '雙11'),
        (12, 1, 12, 28, '聖誕節'),
    ]
    for sm, sd, em, ed, name in events:
        if (m, d) >= (sm, sd) and (m, d) <= (em, ed):
            return name
    return ''  # 平日不填季節 tag


def detect_synonym(title: str) -> str:
    """從標題偵測核心詞, 回傳同義詞 (用於槽 3)"""
    for kw, syn in CAT_SYNONYM_MAP.items():
        if kw in title and syn not in title:  # 同義詞還不在標題
            return syn
    return ''


def extract_spec_tag(title: str, brief: str = '', detail: str = '') -> str:
    """抽 1 個規格賣點 tag (槽 4) — 從原料抽具體屬性"""
    full = f'{brief} {detail}'

    # 翡翠種色
    for kw in ['冰種', '玻璃種', '冰糯種', '糯化種', '飄花', '陽綠', '帝王綠', '老坑']:
        if kw in full and kw not in title:
            return kw

    # 玉石/材質具體
    for kw in ['緬甸玉', '俄玉', '青玉', '碧玉', '羊脂玉', '田黃石', '雞血石']:
        if kw in full and kw not in title:
            return kw

    # 錢幣評級
    for m in re.finditer(r'(PMG|PCGS|公博|華夏|NGC)\s*\d+', full):
        s = m.group(0).replace(' ', '')[:20]
        return s

    # 規格 (圈口/直徑/重量)
    for m in re.finditer(r'(圈口|直徑|長|高|重量)\s*\d+(?:\.\d+)?\s*(?:mm|cm|g|公分)', full):
        return m.group(0).replace(' ', '')[:20]

    # 朝代具體
    for kw in ['乾隆', '康熙', '雍正', '嘉慶', '道光', '咸豐', '同治', '光緒', '宣統', '大正', '昭和', '明治']:
        if kw in full and kw not in title:
            return kw

    # 工藝
    for kw in ['鎏金', '釉裡紅', '青花', '粉彩', '鬥彩', '琺瑯彩', '釉下', '柴燒', '手繪']:
        if kw in full and kw not in title:
            return kw

    return ''


def extract_long_tail(title: str) -> str:
    """槽 5 fallback — 從標題抽第二個有意義的長尾詞"""
    parts = re.findall(r'[\u4e00-\u9fffA-Za-z0-9]+', title)
    parts = [p for p in parts if p not in BAN_WORDS and len(p) >= 2]
    if len(parts) >= 2:
        return parts[1][:20]  # 第二個 token
    return ''


def clean_tag(s: str) -> str:
    """清空格/符號, 截到 20 字"""
    s = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', s)
    return s[:20]


HASHTAG_AI_PROMPT = """你是 Yahoo TW 拍賣標籤 (hashtag) 優化專家。

【Yahoo SEO 研究發現 V42】
1. 標籤是真實被 Yahoo 搜尋池索引 (URL ?ht=XXX 是 hashtag 專屬池)
2. 「免運」對應 64,924 筆大池 — 短 tag 才命中 autosuggest
3. ❌ 7字複合詞「免運費可開發票」沒人會搜 — 浪費槽位
4. ✅ 短詞 (2-5字) 才有 autosuggest 命中

【任務】
為每件商品產生 5 組 hashtag, 每組 ≤ 20 字 (短越好), 不可空格符號

【規則】
1. 每組 2-10 字最佳, 嚴格 ≤ 20 字
2. 不可空格、符號 (除中英數)
3. 不重複「標題已有的詞」(那是浪費)
4. 黑名單禁用: 精美/絕版/稀有/頂級/極品/原裝/正品/保真/經典/限量/撿漏/難得

【5 組 槽位策略 — 必固定的 + 動態的】
槽 1: 免運     ← 全店免運, 必固定填
槽 2: 現貨     ← 高購買意圖, 必固定填
槽 3: 商品同義詞 (動態) — 標題沒有的長尾同義
   例: 標題「翡翠手鐲」→ 標籤「玉鐲」或「A貨翡翠」
   例: 標題「Cartier 腕錶」→ 標籤「卡地亞」或「瑞士錶」
   例: 標題「袁大頭」→ 標籤「銀元」或「龍洋」
槽 4: 規格賣點 (動態) — 種色/工藝/評級/具體規格
   例: 「冰糯種」「飄花」「PCGS XF45」「56mm」
   例: 「鎏金」「青花」「釉下彩」
槽 5: 季節 OR 第二長尾 (動態)
   今日 {{TODAY_SEASON}} (空字串 = 平日不填季節, 填第二長尾)

【輸出格式】
純 JSON: {"1": ["免運","現貨","tag3","tag4","tag5"], "2": [...], ...}

【範例】
原:
  標題: Cartier 山度士 自動腕錶 1970年代 18K 男錶
  原料: 卡地亞瑞士製 機械錶 編號2526
✅ 標籤: ["免運", "現貨", "卡地亞", "瑞士錶", "機械錶"]

原:
  標題: 翡翠手鐲 緬甸 A貨 玉鐲
  原料: 圈口56mm 冰糯種 翠綠色
✅ 標籤: ["免運", "現貨", "A貨翡翠", "冰糯種", "緬甸玉"]

原:
  標題: 袁大頭三年 PCGS XF45 銀元
  原料: 民國 老彩包漿 26.5g
✅ 標籤: ["免運", "現貨", "民國銀幣", "PCGS評級", "龍洋"]

原:
  標題: 牙買加100元紙鈔 2014 全新UNC 首發年
  原料: 紙塑版 外國紙幣
✅ 標籤: ["免運", "現貨", "紙鈔", "外國紙幣", "UNC"]

只回覆 JSON, 不要其他文字。"""


def gen_hashtags_ai(items: List[dict], chat_fn, batch_size: int = 10, workers: int = 6) -> List[List[str]]:
    """AI 批次並發產生 5 組 hashtag

    items: [{'title':..., 'brief':..., 'detail':..., 'free_shipping': True}, ...]
    chat_fn: LLM call function (應 thread-safe)
    workers: 並發 batch 數
    回傳: [[tag1,tag2,tag3,tag4,tag5], ...]
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    season = get_season_tag()
    prompt = HASHTAG_AI_PROMPT.replace('{{TODAY_SEASON}}', season if season else '平日 (不填季節)')

    results = [[] for _ in items]
    batches = [(i, items[i:i+batch_size]) for i in range(0, len(items), batch_size)]

    def run_batch(start_idx, batch):
        items_text = []
        for j, it in enumerate(batch):
            items_text.append(
                f"{j+1}. 標題: {it.get('title','')}\n"
                f"   原料: {(it.get('brief','') + ' ' + it.get('detail',''))[:150]}"
            )
        user_msg = "請為以下 {} 件商品產生 5 組 hashtag:\n\n{}".format(len(batch), '\n\n'.join(items_text))
        body = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
        }
        try:
            content = chat_fn(body)
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*$", "", content.strip())
            try:
                parsed = _json.loads(content)
            except:
                m = re.search(r'\{.*\}', content, re.DOTALL)
                parsed = _json.loads(m.group()) if m else {}
            local = {}
            for k, v in parsed.items():
                try:
                    idx = int(k) - 1
                    if 0 <= idx < len(batch) and isinstance(v, list):
                        cleaned = [clean_tag(str(t)) for t in v]
                        cleaned = [c for c in cleaned if c and len(c) >= 2]
                        local[start_idx + idx] = cleaned[:5]
                except: pass
            return local
        except Exception as e:
            print(f'  [hashtag batch err {start_idx}] {str(e)[:80]}')
            return {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_batch, s, b) for s, b in batches]
        for f in as_completed(futures):
            for idx, val in f.result().items():
                results[idx] = val
    return results


def gen_hashtags(
    title: str,
    brief: str = '',
    detail: str = '',
    free_shipping: bool = True,
    condition: str = '二手',
) -> List[str]:
    """產生 5 組 hashtag (V42 規範: 短詞 + 動態季節)"""
    tags = []

    # 槽 1: 免運 (固定, 假設全店免運)
    if free_shipping:
        tags.append('免運')

    # 槽 2: 現貨 (固定, 高購買意圖)
    tags.append('現貨')

    # 槽 3: 商品核心同義詞
    syn = detect_synonym(title)
    if syn:
        tags.append(syn)

    # 槽 4: 規格賣點
    spec = extract_spec_tag(title, brief, detail)
    if spec:
        tags.append(spec)

    # 槽 5: 季節 OR 第二長尾
    season = get_season_tag()
    if season:
        tags.append(season)
    else:
        long_tail = extract_long_tail(title)
        if long_tail:
            tags.append(long_tail)

    # 清洗 + dedup + 確保 5 組
    cleaned = []
    for t in tags:
        c = clean_tag(t)
        # 不能是空, 不能太短, 不能黑名單, 不能標題已有的詞 (避免重複)
        if c and len(c) >= 2 and c not in cleaned and c not in BAN_WORDS:
            # 簡單 substring 檢查 — 避免標籤是標題子串
            if c in title:
                continue
            cleaned.append(c)

    # 補齊到 5 組 (fallbacks 都是大池有效 tag)
    fallbacks = ['二手', '收藏', '送禮', '台灣賣家', '免運費']
    for fb in fallbacks:
        if len(cleaned) >= 5: break
        c = clean_tag(fb)
        if c and c not in cleaned:
            cleaned.append(c)

    return cleaned[:5]


# === 測試 ===
if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    TESTS = [
        # (title, brief, detail)
        ('Cartier 山度士 自動腕錶 1970年代 18K 男錶',
         '成色：輕微舊痕無損傷',
         '1970年代Cartier山度士 直徑38mm 18K黃金錶殼 機械機芯 編號2526 瑞士製'),
        ('翡翠手鐲 緬甸 A貨 玉鐲',
         '成色：輕微舊痕無損傷',
         '緬甸A貨翡翠手鐲 圈口56mm 條粗9mm 冰糯種 翠綠色'),
        ('袁大頭三年 PCGS XF45 銀元',
         '評級：PCGS\n品相：美品',
         '民國三年袁大頭 PCGS XF45 老彩包漿 26.5g 直徑38mm'),
        ('VISVIM 21AW 復古夾克 男款',
         '尺碼：XL',
         'VISVIM 21AW 工裝夾克 XL 肩寬58cm 胸圍140cm'),
        ('日本花瓶 深川製 彩瓷蝴蝶蘭 陶瓷擺飾',
         '年代：當代\n材質：陶瓷',
         '日本深川製花瓶 高31cm 直徑18cm 重量2.5kg 彩瓷蝴蝶蘭紋飾 底款 深川製'),
        ('日本茶碗 江戶期 古赤膚山 黑釉',
         '',
         '江戶期古赤膚山黑釉茶碗 抹茶碗 12cm 老件'),
        ('Tiffany 925銀項鍊 復古 18K',
         '材質：925 銀',
         'Tiffany 復古 18K 鍍金項鍊 925 銀'),
        ('古玉吊墜 和田玉 龍紋 明清',
         '',
         '明清和田玉古玉吊墜 龍紋雕工 5cm 包漿自然'),
    ]

    today = datetime.now()
    season = get_season_tag()
    print(f'今日 {today.strftime("%Y-%m-%d")} 季節 tag: 「{season}」 (空字串 = 平日不填)\n')

    print('=== V42 5 組 hashtag 測試 ===\n')
    for title, brief, detail in TESTS:
        tags = gen_hashtags(title, brief, detail, free_shipping=True)
        total = sum(len(t) for t in tags)
        print(f'  標題: {title}')
        for i, t in enumerate(tags, 1):
            print(f'    Tag {i}: #{t} ({len(t)}字)')
        print(f'  總 SEO 字符: {total}')
        print()
