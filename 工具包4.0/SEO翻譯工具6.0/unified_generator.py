# -*- coding: utf-8 -*-
"""V45 統一生成器 — 一次 call 出 V44簡述 + V42 5標籤 + 說明清洗

設計動機:
  - 原本 3 個 LLM call (subtitle / hashtag / detail-clean) 重複送 brief+detail
  - 合併成 1 call 省 30% token, 三任務共享上下文一致性更好
  - 失敗回退: 用各自的 fallback (gen_subtitle_simple / gen_hashtags)
"""
from typing import List, Dict, Optional
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


def get_season_tag() -> str:
    """動態季節標籤 — 母親節/父親節 ±2週 內最有用"""
    today = datetime.now()
    m, d = today.month, today.day
    # 母親節 5/11 (大陸通用日期, 第二個週日附近 ±2週)
    if (m == 5 and 1 <= d <= 25) or (m == 4 and d >= 25):
        return '母親節'
    # 父親節 8/8
    if (m == 8 and 1 <= d <= 22) or (m == 7 and d >= 25):
        return '父親節'
    # 端午 6/初, 中秋 9/中, 春節 1/末-2/初
    if m == 6 and d <= 15:
        return '端午'
    if (m == 9 and 5 <= d <= 25):
        return '中秋'
    if (m == 12 and d >= 15) or (m == 1) or (m == 2 and d <= 15):
        return '新年'
    return ''


UNIFIED_PROMPT = """你是 Yahoo TW 拍賣商品優化專家 V48, 一次完成 4 件事:

═══ 共通原則 ═══
- 角色: 台灣 Yahoo 拍賣資深賣家, 一切**以銷量為導向**, 但符合台灣慣用語
- 簡轉繁 + 大陸詞→台灣詞 (科技用語/電商用語/單位)
- 不加表情符號 [微笑]/[心]/emoji, 不用 # hashtag
- 不主動加任何「全台免運/24小時/3天/當日/隔天」等時間或運費承諾
- 保留原文有的銷售形容詞 (精美/絕版/稀有/頂級/經典/限量/完美/包漿一流/開門)

═══ [任務 1] 商品簡述 (subtitle) ═══
**核心定位 (奇摩官方)**: 「商品簡述用來**補充標題說明的不足**」
→ 簡述 = 標題沒講的細節 (絕不重複標題詞)

規範:
- 字數 20-50 字典型, 單行, 空格分隔
- ⚠️ **絕不重複標題已有的詞** (這是奇摩官方規範核心)
  例: 標題「翡翠手鐲 藍水 A貨 冰糯種」 → 簡述應該寫「GTC證書 56.8x9.9mm 起膠感」
  ❌ 不寫「天然翡翠A貨藍水手鐲」(全部跟標題重)
- **保留** 原始閒魚 brief 結構化屬性 (年代/材質/品相/尺寸/品牌) — **但跟標題重的就跳過**
- 從「說明」補具體規格 (尺寸/重量/容量/序號/卡口/編號) — 這些通常標題沒有, 是簡述黃金內容
- 移除冒號分行 (年代:當代 → 直接寫「當代」)
- **不加結尾「全台免運」** (上架工具會自動標)
- ⚠️ **brief 為空或極短 (<10字) 時, 必須從說明抽 3+ 個有用點補入**:
  - 規格 (尺寸/容量/重量/編號)
  - 品相 (使用程度/缺陷)
  - 特色 (產地/工藝/材質特徵)
  - 配件 (原盒/原證/吊牌)
  - 不要只給單一短詞 (例: 「全新」只有 2 字 是不可接受的)

═══ [任務 2] hashtag (3-5 個, **品質優於數量**) ═══
Yahoo TW 規範: 中英數字, 每個 ≤ 20 字, 不可空格/符號
**核心原則**: 標籤 = **買家在 Yahoo 搜尋框真實會打的詞** (= 標題的主詞/副詞概念)

【槽位】
- 槽 1: 免運 (固定)
- 槽 2: 現貨 (固定)
- 槽 3-5: **0 到 3 個動態詞, 沒把握寧可只給 1 個也不要硬湊**
  → 只要找不到「買家會打的真實搜尋詞」, **少給比給垃圾好**

【動態詞優先順序 — 從這裡找】
1. **標題核心主詞同義** (買家會用的搜尋變體):
   - 標題「翡翠手鐲」 → 標籤「玉鐲」「翡翠手環」(同義詞)
   - 標題「古錢幣」 → 標籤「銅錢」「老錢」
   - 標題「Leica」 → 標籤「徠卡」(中文搜尋者會打)
2. **品牌/型號** (標題若沒提到, 補上去):
   - Carhartt / 卡地亞 / Tiffany / 梵克雅寶 / 哈雷
3. **具體材質/年代/產地**:
   - 冰種 / 和田玉 / 紫檀 / 民國三年 / 景德鎮 / 京燒 / 緬甸
4. **季節** (見下方動態注入, 僅商品有送禮場景):
   - 母親節 / 父親節 / 中秋 / 新年

【❌ 絕對禁止】
- SKU / 序號 / 型號編號: 11310 / 7866129 / 1120018387 (買家不會搜數字)
- 鑑賞描述詞 (內行人才用, 熱搜極低):
  字口清晰 / 包漿自然 / 結晶點綴 / 微起光 / 起膠感 / 玉質細膩 / 開臉端莊 / 手工精雕
- 通用空泛: 美品 / 有憑證 / 收藏 / 老件 / 二手 / 居家 / 飾品 / 古玩 / 配件 / 相機配件 / 日常百搭 / 適合送禮
- **跟標題完全重複的詞** (沒加成, 浪費曝光) — 必須用同義詞或補資訊

【判斷標準】問自己:
「會有買家在 Yahoo 搜尋框打這個詞嗎?」
- 「11310」「字口清晰」 → ❌ 不會
- 「Leica」「冰種」「徠卡」「景德鎮」 → ✅ 會
**找不到? 寧可只給 3 個 (免運/現貨/+1) 也不要硬湊垃圾**

═══ [任務 3] 清洗優化說明 (detail_clean) ═══

【絕對要刪】
1. 外部聯絡: LINE / 微信 / QQ / 電話 / Email / 外網
2. 閒魚平台殘留: 閒魚/咸魚/閑魚/驗貨寶/閒魚優選/順豐/中通/EMS/到付
3. 引流話: 加我私聊 / 感興趣請聯絡 / 詳情看主頁 / 私聊
4. 賣家內部筆記: 括號內私人記錄 (存於客廳/放抽屜)
5. **時間/出貨承諾** ⚠️ 全部刪: 24小時內出貨 / 當日寄出 / 隔天到貨 / 3天內發 /
   能下單的都是現貨 / 24h 出貨 (Yahoo 賣家有自己的時間設定, 商品說明不該寫死)
6. **絕對化禁用詞** ⚠️ Yahoo 廣告法: 100%/最/唯一/絕對/保證升值/全網第一
7. 退換條款絕對承諾: 假一賠十 / 包真包老 / 售出不退換
   (注意: 一般「不退換/包退」這種輕度表述, 看上下文判斷, 不一定全刪)
8. **商家自介/自吹/貨源管道** ⚠️ 全部刪 (買家不在乎, 是廢話):
   - 「自家工廠源頭貨」「一手貨源」「源頭直供」「廠家直銷」
   - 「20年/十餘年知名/老店/實體店嚴選」「專業技術團隊」
   - 「文玩圈口碑老店」「業界領先」「行業領先」
   - 「優中選優」「精選好貨」「層層篩選」
   - 「本店經營/出售/銷售世界各國 XXX」
   - 「以及 XX/批發零售」 (經營範圍外提, 跟單品無關)
   - 「廣交天下幣友」「以誠信為宗旨」「明碼標價」
   ⚠️ 注意區分:
   - 商家自介 (刪): 「20 年老店嚴選」
   - 商品歷史 (保留): 「1985 年購於拍賣會」「明治時代日本回流」

【可保留】(對銷售有用)
- 形容詞: 精美 / 絕版 / 稀有 / 頂級 / 經典 / 限量 / 完美
- 賣點: 包漿一流 / 老味道 / 開門 / 收藏佳品 / 升值空間 / 古韻 / 韻味
- 應用場景: 適合送禮 / 收藏 / 把玩 / 日常使用 / 養壺容易
- 商品故事/年代背景

【台灣化詞表】(必做)
- 視頻→影片, 軟件→軟體, 屏幕→螢幕, 鼠標→滑鼠, 計算機→電腦
- 質量→品質, 信息→資訊, 文件→檔案, 默認→預設, 短信→簡訊
- 寶貝→商品, 親→(刪), 拍下→下單, 收貨→收件, 衝鴨→(刪)
- 入手→購入, 撿漏→撿便宜, 不刀/小刀→不議/可議
- 釐米/厘米/公分 → cm, 公克 → g, 公斤 → kg, 毫升 → ml, 毫米 → mm

【格式】
- 短說明 (<60字): 自然單段
- 中長說明: 自然分段
- 不擠一團, 也不過度結構化

═══ [任務 4] 違禁品判斷 (forbidden) — 寬鬆版 (古董/空瓶/收納盒放行) ═══

**核心原則**: 攔截的是**商品本身**會違反 Yahoo TW 規範, 而不是相關類別的所有東西.

【攔截的真正違禁類】
- **煙草** — 點燃式香煙/雪茄/煙絲/煙葉
  ❌ 純收藏品 (Zippo/古董煙具/古董煙盒/煙斗收藏) → forbidden=否
- **酒類** — **含酒精的飲品** (紅酒/白酒/清酒/威士忌/啤酒/烈酒)
  ❌ 空瓶/花器/陳列品/古董酒瓶 (不含酒精) → forbidden=否
- **化妝品/保養品** — 口紅/粉底/精華/乳液/香水/面霜**本體** (有實際內容物)
  ❌ 收納盒/化妝盒/化妝品收納盒/化妝箱/化妝包/化妝鏡/化妝台 (容器/家具不是化妝品本體) → forbidden=否
  ❌ 蒔繪盒/銅鍍金盒/古董盒 (即使原本是化妝品盒, 現在當收藏) → forbidden=否
  ❌ 空瓶 (香水空瓶/化妝品空瓶 收藏) → forbidden=否
- **醫療/藥品** — 處方藥/補品/宣稱療效的商品/醫療器材 (吸入器/呼吸器/針具)
  ❌ 古董醫療器具收藏 (沒實際使用功能) → forbidden=否
- **武器** — **現代真槍/真彈藥/警械/甩棍/伸縮棍/電擊棒/警棍/防身電擊**
  ❌ 古董兵器/古董箭頭/古董刀劍/收藏級武具 → forbidden=否
  ❌ **廚刀/水果刀/木工刀/木質刀/口袋刀/折疊刀/工藝刀/拆信刀** (日常/收藏品) → forbidden=否
  ❌ 武士刀道具/裝飾刀/古董刃物/老件刃物 → forbidden=否
  ❌ 池田刃物/越前打刃物/堺打刃物/關市刃物 (日本刀具品牌, 多為古董/工藝) → forbidden=否
- **活物** — 寵物/活體動物/植物苗 (有生命的)
  ❌ 動物標本/植物標本/化石/貝殼 → forbidden=否

【判斷原則】問自己:
1. 這商品**含酒精**嗎? 沒 → 不算酒類
2. 這商品**是化妝品本體**嗎 (有內容物)? 不是 → 不算化妝品
3. 這商品**是現代殺傷性武器**嗎? 古董/廚具/工藝品 → 不算武器
4. 這商品**是處方藥/有療效宣稱**嗎? 古董醫療收藏 → 不算

【寧鬆勿緊】古董/收藏/藝術品/日常用品 默認 forbidden="否"

forbidden 輸出:
- 「否」 → 安全可上架 (大多數情況)
- 「酒類」/「煙草」/「化妝品」/「藥品」/「武器」/「活物」/「其他」 → 確認違規才攔

═══ ⚠️ 刪句邊界規則 ═══
含必刪詞的句子或子句, **整句完整刪除, 不留半句**:
- ❌ 錯: 「售出不退換」→「售出不」 (留半句)
- ✅ 對: 「售出不退換」→ 整句刪
- ❌ 錯: 「能下單的都是現貨, 24小時出貨」→「能下單的都是現貨」 (留前半)
- ✅ 對: 整段刪 (時間承諾整段不留)
- 刪句後若該行只剩標點/空白, 整行也刪

═══ 輸出格式 ═══
純 JSON, 不要 markdown 圍欄, 不要其他文字:
{
  "1": {"subtitle":"...", "tags":["免運","現貨","..","..",".."], "detail_clean":"...", "forbidden":"否"},
  "2": {...}
}

═══ 範例 ═══

【範例 1 — 古玩 (保留銷售形容詞 + 釋米→mm + 刪聯絡)】
原:
  標題: 翡翠玉鐲 冰糯藍水 A貨 正圈 56圈口
  原簡述: 鑑定機構：GTC 透明度：透明 材質：冰糯種
  說明: 精美翡翠手鐲 內徑56.8釐米 寬9.9釐米 厚6.8mm 微微起光 無紋裂 收藏佳品 順豐包郵 微信13800138000 24小時出貨
✅ {
  "subtitle": "GTC證書 透明 內徑56.8x9.9x6.8mm 微微起光 無紋裂 冰糯種",
  "tags": ["免運","現貨","玉鐲","GTC證書","起光"],
  "detail_clean": "精美翡翠手鐲 內徑56.8mm 寬9.9mm 厚6.8mm 微微起光 無紋裂 收藏佳品",
  "forbidden": "否"
}
注意: 「精美/收藏佳品」保留, 釐米→mm, 順豐/微信/24小時整段刪, 簡述**不加全台免運**

【範例 2 — 違禁品 (酒類)】
原: 1985年法國紅酒 拉菲莊園 750ml 12.5%
✅ forbidden="酒類", pipeline 自動攔截

【範例 3 — Zippo 收藏 (不算煙草)】
原: Zippo 哈雷打火機 美國產 1985 復古收藏
✅ forbidden="否" (純收藏品)

只回覆 JSON, 不要其他文字, 不要 markdown 圍欄。"""


# 後處理: 最小安全網 (主要靠 GPT 用原則判斷, 這裡只清技術性殘留)
# 注意: 不再列 60+ 詞的黑名單 — 用判斷原則由 GPT 完成

# 大陸→台灣單位轉換 (技術性, GPT 偶爾忘做)
_UNIT_CONVERSIONS = [
    (re.compile(r'釐米|厘米'), 'cm'),
    (re.compile(r'公分'), 'cm'),
    (re.compile(r'毫米'), 'mm'),
    (re.compile(r'毫升'), 'ml'),
    (re.compile(r'公克(?![力])'), 'g'),
    (re.compile(r'公斤'), 'kg'),
]

# 表情符號標記 (純技術清掉)
_EMOJI_TAG = re.compile(r'\[(?:微笑|笑哭|心|哭|愛心|tx|玫瑰|釘子|擁抱|可愛|笑|哭笑|羞|呆)\]')

# 閒魚遺留 hashtag (#xxx)
_HASHTAG_LINE = re.compile(r'#[\u4e00-\u9fffA-Za-z0-9_/\-]+')

# 子句含這些關鍵詞 → 整子句刪 (精準, 不傷其他句子)
_CRITICAL_KEYWORDS = (
    # 聯絡 / 引流
    '私聊','私訊','加我','加你','微信','wechat',
    '可聊','喜歡的朋友','喜歡可以','歡迎私','歡迎聯絡','歡迎聯繫',
    '我想要','感興趣請','詳情看主頁',
    # 退換 / 絕對承諾
    '售出不退','不退不換','非假不退',
    '假一賠十','假一賠百','假一賠千','假一賠萬',
    '正品保證','真品保證','非假不退',
    '官網可查','保證升值','保證真品',
    # 大刀 / 議價術語
    '大刀不回','小刀不回','小人鴿子','鴿子繞行',
    # 大陸電商套話
    '不售假貨','售假','權威認證真假無憂','真假無憂',
)

_VAGUE_TAGS = {
    # 過於空泛的鑑賞詞 (買家不會搜尋)
    '美品','有憑證','無憑證','字口清晰','包漿自然','結晶點綴',
    '微起光','起膠感','玉質細膩','開臉端莊','手工精雕',
    '近全新','輕微舊痕','收藏把玩','成色完好','品相完好','品相佳',
    # 過於通用 (槽 1-2 已固定免運/現貨, 不要再用)
    '收藏','老件','二手','居家','飾品','古玩','配件','C服',
    '相機配件','日常百搭','適合送禮','未修補','可加購','配飾齊全',
}

def _is_supp_valid_tag(t: str) -> bool:
    """供 pipeline 補標籤時驗證 (跟 gen_unified_ai 內的 _is_valid_tag 對齊)"""
    if not t or len(t) > 20: return False
    if re.match(r'^\d+$', t): return False
    digits = sum(1 for c in t if c.isdigit())
    if len(t) >= 4 and digits / len(t) > 0.7: return False
    if t in _VAGUE_TAGS: return False
    return True


# 簡述末尾「全台免運」(安全網: GPT 偶爾還是加)
_SUBTITLE_TRAILING_FREE = re.compile(r'[\s，,]*全[店台]?免運[\s，,。]*$')

# 時間/出貨承諾 — 用戶不要 (整句刪除)
_TIME_PROMISES = [
    re.compile(r'24\s*小時(?:內)?(?:出貨|寄出|發貨|到貨)?[\s，,。!?]*'),
    re.compile(r'(?:24h|48h)\s*(?:內)?(?:出貨|寄出|到貨)?[\s，,。!?]*', re.IGNORECASE),
    re.compile(r'當[日天](?:內)?(?:寄出|出貨|發貨|到貨)[\s，,。!?]*'),
    re.compile(r'隔[日天](?:寄|到|出貨|發貨)[\s，,。!?]*'),
    re.compile(r'[2-9]\s*[日天](?:內)?(?:出貨|寄出|發貨|到貨|送達)[\s，,。!?]*'),
    re.compile(r'(?:能下單|可下單)的都是現貨[\s，,。!?]*'),
    re.compile(r'(?:現貨|庫存)\s*\d+\s*(?:件|個|只)?(?:出貨|寄出)?[\s，,。!?]*'),
    re.compile(r'快速出貨[\s，,。!?]*'),
    re.compile(r'即日?寄出[\s，,。!?]*'),
]


def _clean_residue(detail: str) -> str:
    """最小安全網: 單位轉換 + 表情清掉 + 純標點殘行 (主要靠 GPT 用原則處理)"""
    if not detail or not detail.strip():
        return ''

    s = detail

    # 1. 大陸單位 → 台灣 (技術性, GPT 偶爾忘做)
    for pat, repl in _UNIT_CONVERSIONS:
        s = pat.sub(repl, s)

    # 2. 表情符號標記
    s = _EMOJI_TAG.sub('', s)

    # 3. 閒魚遺留 hashtag (例 #翡翠 #A貨)
    s = _HASHTAG_LINE.sub('', s)

    # 3b. 時間/出貨承諾 (24小時/當日/隔天/3日內)
    for pat in _TIME_PROMISES:
        s = pat.sub('', s)

    # 3c. 子句級殘片清洗 — 含關鍵詞的子句整段刪 (精準, 保留其他內容)
    # 按 「。!?\n」 切句, 每句再按 「，,」 切子句, drop 含 critical keyword 的子句
    new_sentences = []
    for sent_part in re.split(r'([。!?\n])', s):
        if sent_part in '。!?\n':
            new_sentences.append(sent_part)
            continue
        sub_parts = re.split(r'([，,])', sent_part)
        keep_subs = []
        for j in range(0, len(sub_parts), 2):
            sub = sub_parts[j]
            sub_sep = sub_parts[j+1] if j+1 < len(sub_parts) else ''
            # 子句含 critical → 整子句刪
            if any(kw in sub for kw in _CRITICAL_KEYWORDS):
                continue
            keep_subs.append(sub + sub_sep)
        rebuilt = ''.join(keep_subs).rstrip('，,')
        if rebuilt.strip():
            new_sentences.append(rebuilt)
    s = ''.join(new_sentences)

    # 4. 收斂連續分隔符
    s = re.sub(r'[，,]\s*[，,]+', '，', s)
    s = re.sub(r'[。]\s*[。]+', '。', s)
    s = re.sub(r'\n\s*\n+', '\n', s)

    # 5. 清空白/純標點行 + 行首尾標點 (technical)
    lines = s.split('\n')
    keep = []
    for line in lines:
        stripped = line.strip()
        stripped = re.sub(r'^[，,。\s!？？！～~\.…—\-]+', '', stripped)
        stripped = re.sub(r'[，,。\s!？？！～~\.…—\-]+$', '', stripped)
        if not stripped:
            continue
        if re.match(r'^[～~\s,.，。、!?！？\-—…．\s]+$', stripped):
            continue
        if len(stripped) < 3:
            continue
        keep.append(stripped)
    s = '\n'.join(keep).strip()
    s = re.sub(r'  +', ' ', s)

    # 5. 結尾規整化 — 每行最後若是中文/英數/規格詞, 補上句號
    final_lines = []
    for line in s.split('\n'):
        line = line.rstrip()
        if not line:
            continue
        last_ch = line[-1]
        # 已是中文標點/英文 .!? — 不動
        if last_ch in '。！？.!?…':
            final_lines.append(line)
        # 結尾連接詞 (，、) — 收尾改句號
        elif last_ch in '，,、':
            final_lines.append(line[:-1] + '。')
        # 結尾是中文字/英數 (沒標點) — 補句號
        elif re.match(r'[\u4e00-\u9fffA-Za-z0-9]', last_ch):
            final_lines.append(line + '。')
        else:
            final_lines.append(line)
    return '\n'.join(final_lines)


def gen_unified_ai(items: List[dict], chat_fn,
                   batch_size: int = 8, workers: int = 6, log_fn=None,
                   on_batch_done=None) -> List[Dict]:
    """並發批次, 一次 call 出 3 個結果

    items: [{'title':..., 'brief':..., 'detail':...}, ...]
    chat_fn: 接受 dict, 回傳 LLM content (應 thread-safe)
    workers: 並發 batch 數
    log_fn: 進度回呼 (GUI 接住), None = 用 print

    回傳: [{'subtitle':..., 'tags':[...], 'detail_clean':...}, ...]
    """
    import json as _json
    season = get_season_tag()
    prompt = UNIFIED_PROMPT
    if season:
        prompt += f"\n\n[本日季節提示] 今日適合用「{season}」當槽 5 (僅限商品有送禮場景)"

    _log = log_fn if log_fn else (lambda s: print(s, flush=True))

    results: List[Dict] = [{} for _ in items]
    batches = [(i, items[i:i+batch_size]) for i in range(0, len(items), batch_size)]

    def run_batch(start_idx, batch):
        items_text = []
        for j, it in enumerate(batch):
            # Stage A 只用「原簡述 + 原說明」 (砍標題+分類, 避免 GPT 被原標題噪音帶歪)
            items_text.append(
                f"{j+1}. 原簡述: {it.get('brief','')[:120]}\n"
                f"   說明: {(it.get('detail','') or '')[:500]}"
            )
        user_msg = f"請為以下 {len(batch)} 件商品同時產出簡述+5標籤+清洗後說明:\n\n" + '\n\n'.join(items_text)
        body = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
        }
        # 中介已升級 chat-chunked-keepalive (CF 524 不再發生),hub 端不需 retry,直接等中介回應
        try:
            content = chat_fn(body)
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*$", "", content.strip())
            try:
                parsed = _json.loads(content)
            except Exception:
                m = re.search(r'\{[\s\S]*\}', content)
                parsed = _json.loads(m.group()) if m else {}
            local = {}
            for k, v in parsed.items():
                try:
                    pos = int(k) - 1
                    if 0 <= pos < len(batch) and isinstance(v, dict):
                        sub = str(v.get('subtitle', '') or '').replace('\n', ' ').strip()
                        sub = _SUBTITLE_TRAILING_FREE.sub('', sub).strip()  # 強制去尾「全台免運」
                        tags_raw = v.get('tags', []) or []
                        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
                        # 清掉空格符號
                        tags = [re.sub(r'[\s#@!\$%\^&\*\(\)+=\[\]\{\}|\\<>?,.\/:;"\']', '', t) for t in tags]
                        # 過濾無價值的標籤 (SKU + 空泛詞), 共用 _is_supp_valid_tag
                        tags = [t for t in tags if _is_supp_valid_tag(t)]
                        detail = str(v.get('detail_clean', '') or '').strip()
                        detail = _clean_residue(detail)  # 最小安全網
                        forbidden = str(v.get('forbidden', '') or '').strip()
                        # 規範化: "否" / "" 都當作非違禁
                        if forbidden in ('否', '', 'no', 'No', 'false', 'False'):
                            forbidden = ''
                        local[start_idx + pos] = {
                            'subtitle': sub,
                            'tags': tags[:5],
                            'detail_clean': detail,
                            'forbidden': forbidden,
                        }
                except: pass
            return local
        except Exception as e:
            # ★ PauseException 不能吞, 傳到外面讓 pipeline 進入 paused 流程
            if 'PauseException' in type(e).__name__: raise
            # ★ 一般失敗時記錄具體商品條碼 (方便排查哪些 V65 攔截)
            codes = []
            for it in batch:
                code = str(it.get('code', '') or it.get('商品條碼', '') or '?')[:20]
                codes.append(code)
            _log(f'  ✗ unified batch fail (start={start_idx}, 影響 {len(batch)} 件): {str(e)[:80]}')
            _log(f'     ↳ 受影響商品條碼: {", ".join(codes)}')
            return {}

    total_batches = len(batches)
    done = 0
    import threading
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_batch, s, b) for s, b in batches]
        for f in as_completed(futures):
            res = f.result()
            for idx, val in res.items():
                results[idx] = val
            # ★ batch 完成後即時 callback (用於 ckpt 中間 save)
            # 不吞 PauseException — 讓它傳到外面 (V64 → pipeline.run)
            if on_batch_done and res:
                try: on_batch_done(res)
                except Exception as _e:
                    if 'PauseException' in type(_e).__name__:
                        raise
                    # 其他 callback 內錯誤吞掉, 不影響 main flow
            with lock:
                done += 1
                pct = int(done * 100 / total_batches)
                # 每 10% 印一次, 避免 log 暴增
                if done == 1 or done == total_batches or pct % 10 == 0:
                    _log(f'  [V65 Stage A] {done}/{total_batches} ({pct}%)')
    return results
