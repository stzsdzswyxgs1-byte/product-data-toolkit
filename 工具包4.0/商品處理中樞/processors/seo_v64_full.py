# -*- coding: utf-8 -*-
"""V65 完整 SEO 流程 — 取代舊 Step F + F2
5 個 stage:
  Stage A: 清洗 detail + 簡述 + 文字違禁 (V53c unified)
  Stage B: 主副詞 (用乾淨 detail+簡述 當 attrs)
  Stage C: ★ multimodal SEO 標題 + 視覺違禁判定 (1 call 雙輸出, 只看 1.jpg)
  Stage D: 5 標籤 (從 SEO+簡述+乾淨說明 抽)
  Stage E: ★ 全圖 reject 判定 (對所有 jpg 跑短 prompt, 標出該刪的 index)

寫回 df:
  - 標題 = SEO 標題
  - 商品簡述 = subtitle
  - 標籤 = 5 tags
  - 說明 = detail_clean
  - _filter_reason: 文字違禁 OR 視覺違禁 任一命中就標
  - _reject_indices: list of int (圖片欄路徑列表中該過濾的 index)
"""
import os, json, time, re, base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

# ─── Seasonal tag boost (節日動態 tag, 可選) ───
try:
    import sys as _sys
    _seasonal_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _seasonal_path not in _sys.path:
        _sys.path.insert(0, _seasonal_path)
    from seasonal import get_seasonal_boost as _seasonal_boost
    _SEASONAL_AVAILABLE = True
except Exception:
    _SEASONAL_AVAILABLE = False

API = 'https://api.example.com/v1/chat/completions'
KEY = '<TEST_API_KEY>'
MODEL = 'gpt-5.5'

# 品牌幻覺黑名單 (含中英文): 標題出現但原文沒提 → 程式自動移除
HALLUCINATION_BRANDS = [
    # 一線奢侈品
    'BV', 'Bottega Veneta', 'Bottega', '寶緹嘉', '宝缇嘉',
    'CHANEL', 'Chanel', '香奈兒', '香奈尔',
    'GUCCI', 'Gucci', '古馳', '古驰',
    'Hermes', 'Hermès', '愛馬仕', '爱马仕',
    'LV', 'Louis Vuitton', '路易威登', '路易威廷',
    'Prada', '普拉達', '普拉达',
    'Dior', '迪奧', '迪奥',
    'Tiffany', '蒂芙尼', 'Cartier', '卡地亞', '卡地亚',
    'Burberry', '巴寶莉', '博柏利',
    'Versace', '凡賽斯', '范思哲',
    'Fendi', '芬迪', 'Celine', '思琳',
    'Loewe', '羅意威', 'Bvlgari', '寶格麗', '宝格丽',
    'Balenciaga', '巴黎世家', 'Armani', '阿瑪尼', '阿玛尼',
    'YSL', 'Saint Laurent', '聖羅蘭', '圣罗兰',
    'Coach', '蔻馳', '蔻驰', 'MCM',
    # 一線奢侈手錶
    'Rolex', '勞力士', '劳力士', 'Patek Philippe', '百達翡麗',
    'Omega', '歐米茄', '欧米茄', 'IWC', '萬國', '万国',
    # IP 角色 (沒在原文一律砍)
    'Disney', '迪士尼', 'Mickey', '米奇', 'Minnie', '米妮',
    'Hello Kitty', '凱蒂貓', '凯蒂猫', 'Sanrio', '三麗鷗', '三丽鸥',
    'Pokemon', '寶可夢', '宝可梦', 'Snoopy', '史努比',
]

def _remove_brand_hallucination(title: str, source_text: str) -> str:
    """如果標題含品牌但原文沒提 → 移除. 保留 vintage 風格學詞 (老捷克/西德琉璃/Art Deco)"""
    if not title or not source_text:
        return title
    src_lower = source_text.lower()
    cleaned = title
    removed = []
    for brand in HALLUCINATION_BRANDS:
        if brand.lower() in cleaned.lower() and brand.lower() not in src_lower:
            # case-insensitive 移除
            cleaned = re.sub(re.escape(brand), '', cleaned, flags=re.IGNORECASE)
            removed.append(brand)
    if removed:
        # 清多餘空格 + 標點
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' |·,，')
    return cleaned

TAGS_PROMPT = """你是 Yahoo TW 拍賣標籤生成專家. 從「商品優化後的標題+簡述+說明」抽 5 個買家會搜的標籤.

【主副詞參考 — 這是客戶搜尋核心】
- 主詞 = Yahoo 買家最常搜的核心詞 (例: 顧景舟 / 翡翠手鐲 / 紫砂壺)
- 副詞 = 第二搜尋路徑 (例: 紫砂壺 / A貨翡翠)
- **標籤要貼近主副詞風格**, 但不能跟主副詞或標題完全重複 (避免冗餘)

【★★★ Yahoo 嚴格格式 (從 bundle 反編譯, 違反 100% 被拒) ★★★】
✅ 允許字元: 中文 + 半形英文 a-zA-Z + 半形數字 0-9
❌ 禁: 任何標點 (. , - _ / : ; 空格 等)
❌ 禁: 全形數字字母 (０ ＡＢＣ)
❌ 禁: 重音字母 (é à ñ ö 等)
❌ 禁: 純數字 (例: 1939 / 2017 / 4550 — 必須混入中文或英文字母)
❌ 禁: 超過 16 字元

【常見錯誤改寫】
- ❌ 2.4cm → ✅ 直徑2cm 或 24mm 或 小尺寸
- ❌ 11.8mm → ✅ 12mm 或 中等尺寸
- ❌ 1939 → ✅ 1939年 / 民國28年 / 民國錢幣
- ❌ 1956-1978 → ✅ 近代收藏 / 1956年
- ❌ Coca-Cola → ✅ CocaCola 或 可口可樂
- ❌ S.T.Dupont → ✅ STDupont 或 都彭
- ❌ JosédeMoura → ✅ 葡萄牙簽名 (重音字會壞)
- ✅ 維持: 200ml / Pt850 / MS68 / NGC / GBCA85 / 特56 / 2017年

【規則】
1. 必含「免運」「現貨」 (前 2 標)
2. 後 3 標從「主副詞同源」抽: 具體品牌/材質/年代/紋飾, 但符合 Yahoo 格式
3. ❌ 禁: 美品/收藏/老件/二手 / SKU / 跟標題或主副詞完全重複
4. 短料 → 寧 3-4 標, 不湊 5
5. 規格類 (尺寸/重量) 優先用「中文描述」(直徑/長度/輕量/中等), 避開純數字

純 JSON: {"1":["免運","現貨","顧景舟","紫泥","螭龍祥雲"],"2":[...]}"""


def _gen_tags_batch(batch_items, _chat_fn):
    items_text = []
    for j, it in enumerate(batch_items):
        cat = str(it.get('category','') or '').strip()
        cat_line = f"\n   分類: {cat}" if cat else ""
        items_text.append(
            f"{j+1}. 標題: {it['seo_title']}{cat_line}\n"
            f"   簡述: {it['subtitle']}\n"
            f"   說明: {it['detail_clean']}\n"
            f"   主詞: {it.get('main','')}\n"
            f"   副詞: {it.get('sub','')}"
        )
    body = {
        "messages":[{"role":"system","content":TAGS_PROMPT},
                    {"role":"user","content":f"請為 {len(batch_items)} 件抽 5 標籤:\n\n"+'\n\n'.join(items_text)}],
        "model":MODEL, "temperature":0.0,
    }
    parsed = None
    for attempt in range(3):
        try:
            content = _chat_fn(body, 90)
            content = re.sub(r"```json\s*","",content); content = re.sub(r"```\s*$","",content.strip())
            try: parsed = json.loads(content); break
            except:
                m = re.search(r'\{[\s\S]*\}', content)
                if m:
                    try: parsed = json.loads(m.group()); break
                    except: pass
            if attempt < 2: time.sleep(2); continue
        except Exception as _e:
            # ★ PauseException 不能吞, 必須傳到外面 (V64 → pipeline)
            if 'PauseException' in type(_e).__name__: raise
            if attempt < 2: time.sleep(2); continue
    out = {}
    for k, v in (parsed or {}).items():
        try:
            pos = int(k) - 1
            if 0 <= pos < len(batch_items) and isinstance(v, list):
                cleaned = []
                seen = set()
                for t in v:
                    s = sanitize_yahoo_hashtag(str(t))
                    if s and s not in seen:
                        seen.add(s)
                        cleaned.append(s)
                out[pos] = cleaned[:5]
        except: pass
    return out


# ─── Yahoo 拍賣 hashtag 嚴格規則 (從 bundle 反編譯, 109 筆失敗驗證) ───
# regex: ^(?!\d+$)[\u4E00-\u9FA5a-zA-Z0-9]+$
# 只允許: CJK 中文 + 半形英文 + 半形數字
# 禁止: 標點 / 全形 / 重音字 / 純數字 / >16 字
_YAHOO_HASHTAG_RE = re.compile(r'^(?!\d+$)[\u4E00-\u9FA5a-zA-Z0-9]+$')
_FW_TO_HW = str.maketrans(
    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ',
    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
)
_NOT_WHITELIST = re.compile(r'[^\u4E00-\u9FA5a-zA-Z0-9]')

def sanitize_yahoo_hashtag(tag: str):
    """清洗 tag 符合 Yahoo hashtag 規則, 不能救則回 None"""
    if not tag:
        return None
    # 1. 全形 → 半形 (數字 + 英文)
    tag = tag.translate(_FW_TO_HW)
    # 2. 移除所有非白名單字元 (. - _ 空格 重音字 全形符號 等)
    tag = _NOT_WHITELIST.sub('', tag)
    # 3. 純數字 (年份等) 丟棄
    if not tag or tag.isdigit():
        return None
    # 4. 16 字上限
    if len(tag) > 16:
        tag = tag[:16]
    # 5. 最終 regex 驗證
    if not _YAHOO_HASHTAG_RE.match(tag):
        return None
    return tag


# ─── Stage E reject scan: 短 prompt 快速判定單張圖是否該刪 ───
REJECT_PROMPT = """快速判斷商品圖是否含 Yahoo 不接受元素 (任一命中 reject=true):

❌ 必抓 (重點 7 類):
1. **整張無商品 + 純文字宣傳圖** (溫馨提示/本店承諾/誠信經營/公平交易/退換貨說明/客服說明/店鋪須知 等. 特徵: 看不到任何商品本體, 只有大段文字 + 裝飾背景)
2. 裝飾邊框 (整圈或單邊色條: 粉色/深藍/木紋等)
3. 售價/促銷標 (現貨包郵/24h極速/特價/$XX/質保一年/順豐包郵/坚持原裝/100% NEW)
4. ★ **賣場 logo 浮水印 (任何邊緣位置)** — 不限右下, **底部居中 / 左上 / 右上 / 任何角落** 都算:
   - 閒魚號:Wpy / 抖音號 / @古錢幣交流專用吧 / 寶藏單品推薦 / 防盜浮水印 (凌晨白手套)
   - ★ **複合 logo 結構**: 中文方框 (吉石到 / XX 收藏 / XX 古玩店 / XX 文玩) + **上下水平線**裝飾 + **英文 italic 小字** (integrity·innovate / shi.to / luxury 等)
     特徵: 浮在背景空白處 (不蓋在商品上), 整組裝飾很「設計感」
     vs 中國書畫作者印章: 純中文篆字方塊印, **無水平線無英文**, 蓋在畫紙上 (不浮空)
5. ★ **賣家自加的【方框印章 / 公司章 / 地名章】+ 時間戳 (例: 紅藍色框「茂名」「XX 收藏」「XX 古玩店」+ 「2026/05/04 20:44」格式時間)**: 角落明顯「規則矩形/橢圓框 包住 中文地名或人名 + 數字日期」就算
6. ★ **角落紅字尺寸/重量規格** (任何顏色但通常紅色, 在四角範圍內, 內容是「直徑X.X公分/厘米/cm」「厚度X.X公分」「重量X克/g」「長X寬X」「高X」之類純規格描述). **這是賣家事後加的, 必抓**
7. ★ **中央描述/讚美文字** (浮在商品上方或周圍的白色/帶陰影簡體中文形容詞, 例「精美的玉雕膠感溫潤光澤」「獨具神珠」「天然好料」「極品」「老料新工」). 不是商品上印的字, 是後製加的描述文字, 即使在中央也要抓

❌ 也算 (邊緣區域):
- 頂底 banner (金典嚴選/閒魚官方認證 等橫幅)
- 平台 logo (閒魚/咸鱼)
- 賣家自編商品標籤紙 (ZZ 280 / 編號標籤貼在商品旁)

⚠️ 不算問題 (reject=false):
✅ **純** 手機相機浮水印, 沒搭配印章 (HUAWEI/vivo/xiaomi/Apple/REDMI/HONOR 字+小型號, **單獨出現** 才放過)
   ⚠ 但若同一張圖另外還有【賣家自加印章 / 公司章 / 時間戳組合】→ 必抓 (見 ❌ 第 5 類)
✅ QR code 馬賽克 / 手指拿著 / 一般家居背景
✅ 商品本體認證標 (JRA/HB-40/MADE IN JAPAN/品牌 logo 縫在商品上)
✅ 商品紋飾/印章/朝代名 (永曆通寶/中華民國 等錢幣本體字)
✅ 評級盒鑑定資料 (PMG/NGC/公博/GBCA/北京公博 + 編號重量尺寸 — 整套鑑定條形貼紙)
✅ **商品本體上的字 (寫在錢幣/玉牌/印章/書畫上) → 不抓**
   ⚠ 但若整張圖看不到商品本體, 只有純文字, 必須抓 (見 ❌ 第 1 類)

⚠️ 賣家印章 vs 商品本體印章 區分原則:
- 賣家自加印章 (❌ 抓): **浮在背景空白處** 的方框/橢圓框 + 公司或地名 + 時間戳, 不接觸商品本體
- 商品本體印章 (✅ 不抓): **印在商品上、評級盒裡、包裝紙上** 的紅色篆書印章 (古董字畫蓋的私人印 / 文玩配的紙質背景印)

⚠️ 規格文字 vs 評級資料 區分 (新加 6 類):
- 賣家自加紅字規格 (❌ 抓): **浮在商品周圍空白處** 的「直徑/厚度/重量」純數字+單位, 字體電腦字, 通常紅色 / 白色描邊
- 評級條形貼紙 (✅ 不抓): **整套帶 logo 的鑑定貼紙** (PMG/NGC/GBCA/公博 等), 含品牌 + 編號 + 規格條碼, 是整體標籤而非散落紅字

⚠️ 描述文字 vs 商品本體字 區分 (新加 7 類):
- 賣家描述文字 (❌ 抓): **浮在商品本體之上或周圍** 的形容詞/讚美句, 帶白色描邊或陰影, **不是寫在商品表面**
- 商品本體字 (✅ 不抓): **物理上印/刻/寫在商品表面** 的字 (錢幣的朝代字/玉牌的銘文/印章的篆字)
- 區分線索: 描述文字字體都是黑體/手寫體電腦字 + 帶光暈陰影; 商品字是貼合表面紋理 + 古體字/銘文

⚠️ 容易誤判的 NOT 邊框 (不算 reject):
- 商品的陰影 / 反光 / 倒影
- 桌面/背景的色塊邊緣 (黑/灰/木紋, 不規則或漸變)
- 角落零碎物品 (木尺/織物角/紙片邊)
- 必須是【整邊或整圈】規則色條才算邊框, 散亂的色塊不算

★ 4.0.54: 必須帶 reject_score (0.0-1.0 該 reject 的機率, 跟 reject 互相印證):
  - 1.0 = 100% 確定該 reject
  - 0.7-0.9 = 大概該 reject (邊界偏高)
  - 0.5 = 邊界, AI 自己拿不準 (admin 該抽查)
  - 0.3 = 大概不該 reject (但有一點不確定)
  - 0.0 = 100% 完全乾淨
reject (true/false) 是你最終決定 (通常 score >= 0.5 → reject)

純 JSON: {"reject":true/false, "reject_score":0.95}
不要 markdown."""


def _img_to_b64_compressed(img_path, max_dim=1024, quality=85):
    """壓縮圖片到 max_dim 後 base64 編碼 (上傳更快, AI 判定不受影響)
    ★ 4.0.71: 應用 EXIF orientation — iPhone/Android 原圖預設 EXIF=6 右旋 90度,
       PIL 預設不 apply, 像素是「躺著的」, AI 看了會把「右下水印」當「左下」.
       Stage E 跟 K0 都用此函數, 一致應用 EXIF 對齊座標系.
    """
    from PIL import Image, ImageOps
    from io import BytesIO
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img).convert('RGB')
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize((int(img.size[0]*ratio), int(img.size[1]*ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, 'JPEG', quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _reject_scan_one(img_path, xtrace_header: str = ''):
    if not img_path or not os.path.exists(img_path):
        return False
    try:
        # ★ 壓縮到 1024px 上傳 (AI 判水印/邊框不需高解析度, 上傳省 80% 流量)
        img_b64 = _img_to_b64_compressed(img_path, max_dim=1024, quality=85)
    except:
        return False
    body = {
        'model': MODEL,
        'messages': [{
            'role':'user',
            'content':[
                {'type':'text','text':REJECT_PROMPT},
                {'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{img_b64}'}}
            ]
        }],
        'temperature': 0.0,
    }
    try:
        from checkpoint_manager import get_monitor
        _monitor = get_monitor()
    except Exception:
        _monitor = None
    last_status = None; last_err_code = None; last_err_type = None; last_err_msg = None; last_exc = None
    _headers = {'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
    if xtrace_header:
        _headers['X-Trace'] = xtrace_header
    # ★ 4.0.45: shared session (TLS keep-alive)
    try:
        from processors.utils import get_shared_session
        _http = get_shared_session()
    except Exception:
        _http = requests
    for attempt in range(2):
        try:
            r = _http.post(API,
                headers=_headers,
                json=body, timeout=60)
            last_status = r.status_code
            if r.status_code == 200:
                jr = r.json()
                # 中介 v3 合約: 200 也可能有 error (如 hourly_limit/unknown provider)
                err = jr.get('error') or {}
                if err:
                    last_err_code = err.get('code'); last_err_type = err.get('type'); last_err_msg = err.get('message')
                else:
                    content = jr['choices'][0]['message']['content']
                    content = re.sub(r"```json\s*","",content); content = re.sub(r"```\s*$","",content.strip())
                    # ★ 4.0.55 fix: 改 try-json-first + greedy regex fallback (跟 batch 對齊)
                    parsed = None
                    try:
                        parsed = json.loads(content)
                    except Exception:
                        m = re.search(r'\{[\s\S]*\}', content)  # greedy
                        if m:
                            try: parsed = json.loads(m.group())
                            except Exception: pass
                    if parsed is not None:
                        rj = parsed.get('reject', False)
                        if isinstance(rj, str): rj = rj.strip().lower() in ('true','是','yes','1')
                        if _monitor: _monitor.record_success()
                        return bool(rj)
            if attempt < 1: time.sleep(1)
        except Exception as e:
            last_exc = e
            if attempt < 1: time.sleep(1)
    if _monitor:
        # ★ 4.0.31: record_fail_safe 自帶 is_network_blip 檢查
        try:
            from processors.utils import record_fail_safe
            record_fail_safe(_monitor, status_code=last_status, error_code=last_err_code,
                             error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
        except ImportError:
            _monitor.record_fail(status_code=last_status, error_code=last_err_code,
                                 error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
    return False


# Stage E batch=6: 一次 6 張圖 1 call, 省 40-45% wall-time, 質量同 single (300 樣本驗證 Recall 43=43, FP 6=6)
BATCH_REJECT_PROMPT = """看這 {n} 張商品圖 (按順序編號 1-{n}), 任一張含 Yahoo 不接受元素 → reject 該 index.

❌ 必抓 (重點 6 類):
1. 裝飾邊框 (整圈或單邊色條: 粉色/深藍/木紋等)
2. 售價/促銷標 (現貨包郵/24h極速/特價/$XX/質保一年/順豐包郵)
3. ★ **賣場 logo (任何邊緣位置, 不限右下)**: 閒魚號/抖音號/寶藏單品推薦/防盜浮水印
   ★ **複合 logo 結構**: 中文方框 (吉石到 / XX 收藏 / XX 古玩) + **上下水平線** + **英文 italic** (integrity·innovate 等), 浮在背景空白處 — 即使在底部居中也必抓
   vs 中國書畫作者印章 (純中文篆字方塊, 無水平線無英文, 蓋在畫紙上): ✅ 不抓
4. ★ **賣家自加方框印章 + 時間戳組合** (角落紅藍框「茂名」「XX 收藏」+「2026/05/04 20:44」格式日期, 浮在背景非商品本體上)
5. ★ **角落紅字尺寸/重量規格** (任何顏色但通常紅色, 在四角範圍內, 內容是「直徑X.X公分/cm」「厚度X.X」「重量X克/g」純規格. 賣家事後加, 必抓)
6. ★ **中央描述/讚美文字** (浮在商品上方/周圍的白色帶陰影簡體中文形容詞, 例「精美的玉雕膠感溫潤光澤」「獨具神珠」「天然好料」「極品」「老料新工」. 字體是電腦字+陰影, 不是商品表面的銘文)

❌ 也算 (邊緣區域):
- 頂底 banner (金典嚴選/閒魚官方認證)
- 平台 logo (閒魚/咸鱼)
- 賣家編號標籤紙 (ZZ 280)

⚠️ 不算問題:
✅ **純** 手機相機浮水印 (HONOR/HUAWEI/vivo/xiaomi 字+型號, **單獨出現** 才放過; 若同時有印章+時間戳 → 抓)
✅ QR code 馬賽克 / 手指拿著 / 一般家居背景
✅ 商品本體認證標 / 商品紋飾/印章/朝代名 / 評級盒鑑定資料 (PMG/NGC/GBCA 條形貼紙)
✅ 包裝紙/字畫上的紅色篆書印 (印在商品/紙上, 不浮空)
✅ **物理上印在商品表面的字 (錢幣朝代字/玉牌銘文/印章篆字) = 商品本體, 不抓**
   ⚠ 但如是 PS 加上去的描述文字 (帶陰影電腦字浮在商品上) → 必抓 (見 ❌ 第 6 類)

⚠️ 賣家印章 vs 商品印章 區分:
- ❌ 賣家章: **背景空白處** 浮著的規則框 + 中文地名/人名 + 數字時間
- ✅ 商品章: **印在商品/評級盒/包裝紙上**, 跟商品本體一起

⚠️ 賣家紅字規格 vs 評級貼紙 區分 (新加 5 類):
- ❌ 紅字規格: 浮在商品周圍空白處的「直徑/厚度/重量」純數字+單位, 字體電腦字
- ✅ 評級貼紙: 整套帶 logo (PMG/NGC/GBCA), 含品牌+編號+規格條碼

⚠️ 賣家描述字 vs 商品本體字 區分 (新加 6 類):
- ❌ 描述字: 字體電腦黑體/手寫體 + 白色描邊+陰影 + 浮在商品上方/周圍
- ✅ 商品字: 物理上刻/印/寫在商品表面, 貼合紋理, 古體字/銘文

⚠️ NOT 邊框 (常見誤判):
- 商品陰影/反光/倒影
- 桌面/背景色塊邊緣 (黑/灰/木紋, 不規則或漸變)
- 角落零碎物品 (木尺/織物角/紙片邊)
- 必須【整邊或整圈】規則色條才算邊框, 散亂色塊不算

★ 必須**逐張獨立判定**, 不要混淆編號

★ 4.0.54: reject_scores 對**全部 N 張**都回 (0.0-1.0 該 reject 的機率):
  - 1.0 = 100% 確定該 reject
  - 0.7-0.9 = 大概該 reject
  - 0.5 = 邊界, AI 自己拿不準 (admin 重點抽查這區間)
  - 0.3 = 大概不該 reject (但仍有點不確定)
  - 0.0 = 100% 完全乾淨
reject_indices 是你最終決定 (通常 score >= 0.5 → reject), admin 用 reject_scores 找邊界 case
**所有 N 張都要回 reject_score, 包括沒 reject 的**

純 JSON: {{"reject_indices":[1,3], "reject_scores":{{"1":0.95,"2":0.10,"3":0.85,"4":0.05,"5":0.45,"6":0.20}}}}
reject_indices=[] 時 reject_scores 仍要回 (對所有 N 張回低分)
不要 markdown."""


def _reject_scan_batch(img_paths, xtrace_header: str = ''):
    """batch 版: 一次 N 張圖 1 call, 回 list of bool 對應每張.
    ★ API 失敗時回 None (跟「全沒 reject」的 [False]*n 區分, 主 loop 看 None 不 mark_done)
    """
    if not img_paths:
        return []
    valid = [(i, p) for i, p in enumerate(img_paths) if p and os.path.exists(p)]
    if not valid:
        return [False] * len(img_paths)  # 圖片檔案不存在, 視為 done (不重試)
    try:
        b64s = [_img_to_b64_compressed(p, max_dim=1024, quality=85) for _, p in valid]
    except:
        return [False] * len(img_paths)  # 編碼失敗, 個別圖問題
    n = len(b64s)
    content = [{'type':'text','text': BATCH_REJECT_PROMPT.format(n=n)}]
    for b in b64s:
        content.append({'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{b}'}})
    body = {'model': MODEL, 'temperature': 0.0,
            'messages':[{'role':'user','content': content}]}
    try:
        from checkpoint_manager import get_monitor
        _monitor = get_monitor()
    except Exception:
        _monitor = None
    last_status = None; last_err_code = None; last_err_type = None; last_err_msg = None; last_exc = None
    _headers = {'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
    if xtrace_header:
        _headers['X-Trace'] = xtrace_header
    # ★ 4.0.45: shared session (TLS keep-alive)
    try:
        from processors.utils import get_shared_session
        _http = get_shared_session()
    except Exception:
        _http = requests
    for attempt in range(2):
        try:
            r = _http.post(API,
                headers=_headers,
                json=body, timeout=120)
            last_status = r.status_code
            if r.status_code == 200:
                jr = r.json()
                err = jr.get('error') or {}
                if err:
                    last_err_code = err.get('code'); last_err_type = err.get('type'); last_err_msg = err.get('message')
                else:
                    cnt = jr['choices'][0]['message']['content']
                    cnt = re.sub(r"```json\s*","",cnt); cnt = re.sub(r"```\s*$","",cnt.strip())
                    # ★ 4.0.55 fix: 4.0.54 加 nested reject_scores dict, 原本 non-greedy regex
                    #   抓到 inner } 截掉, json.loads 失敗 → 70 次 record_fail → cascade pause.
                    #   先 try 整段 json.loads (常見 case), 失敗才 fall back 用 greedy regex.
                    parsed = None
                    try:
                        parsed = json.loads(cnt)
                    except Exception:
                        m = re.search(r'\{[\s\S]*\}', cnt)  # greedy
                        if m:
                            try: parsed = json.loads(m.group())
                            except Exception: pass
                    if parsed is not None:
                        rj_idx = parsed.get('reject_indices', []) or []
                        rj_idx = set(int(x) for x in rj_idx if isinstance(x, (int, str)) and str(x).isdigit())
                        out = [False] * len(img_paths)
                        for local_i, (orig_i, _) in enumerate(valid):
                            out[orig_i] = (local_i + 1) in rj_idx
                        if _monitor: _monitor.record_success()
                        return out
            if attempt < 1: time.sleep(1)
        except Exception as e:
            last_exc = e
            if attempt < 1: time.sleep(1)
    if _monitor:
        # ★ 4.0.31: record_fail_safe 自帶 is_network_blip 檢查
        try:
            from processors.utils import record_fail_safe
            record_fail_safe(_monitor, status_code=last_status, error_code=last_err_code,
                             error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
        except ImportError:
            _monitor.record_fail(status_code=last_status, error_code=last_err_code,
                                 error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
    # ★ API 失敗 → return None (主 loop 看 None 不 mark, 下次重跑)
    return None


def _seo_visual_multimodal(orig_title, subtitle, detail_clean, primary, secondary, brief,
                           img_path, seo_system_prompt, category='', xtrace_header: str = ''):
    """1 個 multimodal call: SEO 標題 + 視覺違禁 + 1.jpg 是否該刪除 + 是否商品全貌"""
    if not img_path or not os.path.exists(img_path):
        return ('', '否', '', False, True)
    try:
        # ★ 壓縮到 1024px 上傳 (Stage C SEO 不需高解析度, 上傳省 80% 流量)
        img_b64 = _img_to_b64_compressed(img_path, max_dim=1024, quality=85)
    except:
        return ('', '否', '', False, True)

    cat_line = f"- 商品分類 (採集來源, 高可信): {category}\n" if category else ""
    user_text = (
        f"商品事實源 (★ 標題權重核心 = 主副詞 + 說明前60字):\n"
        f"{cat_line}"
        f"- 簡述: {subtitle}\n"
        f"- 說明前60字 (權重核心): {detail_clean[:60]}\n"
        f"- 主詞: {primary}\n"
        f"- 副詞: {secondary}\n\n"
        f"請結合**附圖商品** + 文字事實源, 完成步驟 (★商品分類是高可信事實, 違禁判定要參考):\n\n"
        f"【Step 0: 視覺事實列表 (Rationale, 寫標題前必填)】★\n"
        f"看圖列出**真正能確認**的客觀事實 (沒看到的明確說「無」, 不要猜):\n"
        f"- 看到的品牌 logo/IP 角色字樣: 寫實際看到的 / 「無」\n"
        f"- 主品類 (項鍊/戒指/鏡頭/錢幣等): \n"
        f"- 風格特徵 (vintage 老捷克琉璃/西德琉璃/Art Deco/巴卡拉/一般家拍): \n"
        f"★ 此步是反幻覺核心 — 沒看到品牌就明確寫「無」, 後面標題就不能加品牌\n\n"
        f"【任務 1: SEO 標題】\n"
        f"★★★ 反幻覺鐵律 (寫標題時嚴格遵守): ★★★\n"
        f"  ✅ 圖片只用於『驗證主品類』(這是不是錢幣/服飾/相機鏡頭) — yes/no\n"
        f"  ❌ 禁止從圖識別/添加任何品牌 (BV/Bottega Veneta/CHANEL/GUCCI/Disney/三麗鷗 等)\n"
        f"  ❌ 禁止從圖識別/添加型號 (iPhone15Pro/AirPods Pro 等)\n"
        f"  ❌ 禁止從圖識別/添加 IP 角色 (米奇/Hello Kitty 等)\n"
        f"  ❌ 禁止從圖添加原文未提的: 規格/材質/狀態/年代/版本\n"
        f"  → 品牌/型號/IP/規格 只從『原文或主副詞』獲得, 原文沒寫就不寫\n"
        f"\n"
        f"標題寫法:\n"
        f"1. 主副詞 + 說明前60字 是權重核心, 從中抽具體賣點詞**必保留**\n"
        f"2. 圖片只用於確認主品類 (項鍊 vs 戒指 vs 手鐲), 不從圖加品牌/型號/IP\n"
        f"3. 主詞放開頭, 副詞前 13 字內\n"
        f"4. 字數 25-30 字 (依資訊量, 短料 12-18 字也 OK)\n"
        f"5. 砍助詞 + 黑名單形容詞 (絕版/頂級/原裝/正品/保真)\n"
        f"6. 繁體中文, 禁「中國」(改朝代名)\n"
        f"7. 攝影鏡頭類 — 加買家熱搜功能詞 (餅乾鏡/防手震/變焦/恆定光圈/IS防震/USM/STM)\n\n"
        f"【任務 2: 視覺違禁判定】★\n"
        f"從**圖片+分類**綜合判是否違禁 (Yahoo TW 嚴禁):\n"
        f"  - 真槍/真警械/甩棍 (現代非古董)\n"
        f"  - 含酒精飲品瓶身有液體 (空酒瓶不算)\n"
        f"  - 化妝品本體/口紅/粉底 (古董化妝盒不算)\n"
        f"  - 處方藥/醫療器材 (X光片/針劑/處方包裝)\n"
        f"  - 活物 (活體動物/植物盆栽)\n"
        f"  - 香煙/煙絲/雪茄 (現代品)\n"
        f"★ 用商品分類交叉驗證:\n"
        f"  - 分類含「古董/收藏/邮币/字画」→ 即使圖看到酒瓶/兵器/化妝盒, 大概率「否」\n"
        f"  - 分類含「美妝/個護/香水」→ 是化妝品本體, 違禁\n"
        f"  - 分類含「煙酒」→ 大概率違禁\n"
        f"古董/收藏/標本/化石/古兵器/木工刀/口袋刀 → 「否」\n\n"
        f"【任務 3: 圖片問題判定 (是否該刪這張首圖)】★\n"
        f"判斷圖片是否含以下**Yahoo 不接受元素**, 任一命中 → reject_first=true:\n"
        f"  ❌ 賣場 logo/品牌標 (例: 金典拍拍嚴選好物/某某二手店/拍拍嚴選)\n"
        f"  ❌ 平台水印 (例: 閒魚 號:Wpy004 / @古錢幣交流專用吧 / 咸鱼 / 抖音)\n"
        f"  ❌ 文字促銷 (現貨包郵/24小時極速/特價/$XX/限時/免運貼紙)\n"
        f"  ❌ 裝飾邊框 (粉色/深藍/木紋裝飾外框)\n"
        f"⚠️ 注意這些**不算問題**, reject_first=false:\n"
        f"  ✅ 手機相機浮水印 (HUAWEI/vivo/xiaomi/Apple/REDMI 自動加的型號+時間戳)\n"
        f"  ✅ QR code 馬賽克 (賣家保護)\n"
        f"  ✅ 手指拿著商品 (家拍正常)\n"
        f"  ✅ 一般家居背景\n\n"
        f"【任務 4: 是否商品全貌 (絕大多數預設 true, ★ 嚴格只擋 3 種)】★\n"
        f"  ✅ is_full_product=true (★ 預設, 95% 商品都是):\n"
        f"     - 整個商品**輪廓可見** (整把茶壺/整個錢幣/整件衣服/整個鏡頭)\n"
        f"     - **手指/手掌拿著商品** = 算完整 (家拍正常, 不該被當局部)\n"
        f"     - 商品邊緣被切 10-30% (仍算完整)\n"
        f"     - **PCGS/PMG/NGC/GBCA/ACG/CSIS 評級盒** = 算完整 (盒子=商品本體)\n"
        f"     - 紙幣/郵票/銅章/錢幣 等扁平商品 (含 barcode/序號 都算完整)\n"
        f"     - 商品 + 配件清晰可見 (鏡頭+收納袋, 錢幣+評級盒)\n"
        f"     - 商品有自帶字樣/紋飾/手機相機浮水印\n"
        f"  ❌ is_full_product=false (★ 嚴格, 只 3 種):\n"
        f"     1. **微觀紋理特寫** (玉表面只見一條黑線/錢幣只見半個字, 完全看不出整體商品)\n"
        f"     2. **商品被裁掉 > 50%** (只見極小部分, 例: 半個鏡頭只見鏡片)\n"
        f"     3. **多商品拼集 ≥ 5 件** (5+ 雙湯匙/5+ 張郵票/5+ 個胸針, AI 會搞錯數量)\n"
        f"  ★ 注意: 不要把『手指拿著』『含 barcode』『含序號』當 close_up — 這些是正常家拍\n"
        f"用途: 真正局部特寫/極端拼集 跳過 AI 重生, 其他都跑 (image_opt prompt 會保留所有字樣)\n\n"
        f'純 JSON 回覆: {{"seo_title":"...","forbidden_visual":"否或酒類/煙草/化妝品/藥品/武器/活物/其他","visual_note":"看到的商品本質一句話","reject_first":false,"is_full_product":true}}'
    )

    body = {
        'model': MODEL,
        'messages': [
            {'role':'system','content':seo_system_prompt},
            {'role':'user','content':[
                {'type':'text','text':user_text},
                {'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{img_b64}'}}
            ]}
        ],
        'temperature': 0.1,
    }
    try:
        from checkpoint_manager import get_monitor
        _mon_sc = get_monitor()
    except Exception:
        _mon_sc = None
    last_status = None; last_err_code = None; last_err_type = None; last_err_msg = None; last_exc = None
    _headers = {'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
    if xtrace_header:
        _headers['X-Trace'] = xtrace_header
    # ★ 4.0.45: shared session (TLS keep-alive)
    try:
        from processors.utils import get_shared_session
        _http = get_shared_session()
    except Exception:
        _http = requests
    for attempt in range(3):
        try:
            # ★ 4.0.82 Phase 1: timeout 120 → 60. audit 顯示 stage_c P99=47s, max=346s (上游卡死),
            #   60s buffer 夠 P99 (47s + 13s margin), max 346s 會被 abort 釋放 slot 給別人.
            #   worst case 3 retry × 60 = 180s 仍比 max 346s 短.
            r = _http.post(API,
                headers=_headers,
                json=body, timeout=60)
            last_status = r.status_code
            if r.status_code == 200:
                jr = r.json()
                err = jr.get('error') or {}
                if err:
                    last_err_code = err.get('code'); last_err_type = err.get('type'); last_err_msg = err.get('message')
                else:
                    content = jr['choices'][0]['message']['content']
                    content = re.sub(r"```json\s*","",content); content = re.sub(r"```\s*$","",content.strip())
                    # ★ 4.0.55: 用 greedy regex (4.0.54 nested dict 的修法)
                    try: parsed = json.loads(content)
                    except:
                        m = re.search(r'\{[\s\S]*\}', content)  # greedy
                        try: parsed = json.loads(m.group()) if m else {}
                        except Exception: parsed = {}
                    seo = str(parsed.get('seo_title','')).strip()
                    fv = str(parsed.get('forbidden_visual','否')).strip()
                    note = str(parsed.get('visual_note','')).strip()[:80]
                    rj = parsed.get('reject_first', False)
                    if isinstance(rj, str): rj = rj.strip().lower() in ('true','是','yes','1')
                    ifp = parsed.get('is_full_product', True)
                    if isinstance(ifp, str): ifp = ifp.strip().lower() in ('true','是','yes','1')
                    if _mon_sc: _mon_sc.record_success()
                    return (seo, fv, note, bool(rj), bool(ifp))
            if attempt < 2: time.sleep(2 + attempt*3); continue
        except Exception as e:
            last_exc = e
            if attempt < 2: time.sleep(2 + attempt*3); continue
    if _mon_sc:
        # ★ 4.0.31: 這就是這次 Stage C cascade root cause! _seo_visual_multimodal 直接 record_fail
        #   沒走 _patched_chat (那是 Stage A 用的). 218ms RTT × 38 並發圖上傳 → TLS timeout cascade.
        #   改 record_fail_safe 後純連線層 fail 不算 monitor.
        try:
            from processors.utils import record_fail_safe
            record_fail_safe(_mon_sc, status_code=last_status, error_code=last_err_code,
                             error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
        except ImportError:
            _mon_sc.record_fail(status_code=last_status, error_code=last_err_code,
                                error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
    return ('', '否', '', False, True)


def run_v64_full(df, log_fn: Callable, seo_dir: str,
                 progress_fn: Optional[Callable] = None,
                 enable_stage_e: bool = True,
                 log_detail_fn: Optional[Callable] = None,
                 ckpt=None) -> dict:
    """
    對 df 跑 V64 完整 4-stage 流程.
    log_detail_fn: 詳細日誌 callback (寫到檔案), None 則 fallback 到 log_fn
    """
    log_detail = log_detail_fn if log_detail_fn else log_fn
    import sys
    if seo_dir not in sys.path:
        sys.path.insert(0, seo_dir)
    import translator
    from unified_generator import gen_unified_ai
    from processors.utils import append_reason

    # 跳過已過濾商品
    has_reason = df['_filter_reason'].fillna('').str.strip().astype(bool) if '_filter_reason' in df.columns else None
    valid_idx = df.index[~has_reason] if has_reason is not None else df.index
    n = len(valid_idx)
    log_fn(f"[V64] 待處理 {n} 件 | 跳過已過濾 {len(df)-n} 件")
    if n == 0:
        return {'optimized': 0, 'visual_forbid': 0, 'text_forbid': 0, 'tag_ok': 0}

    # 取資料
    titles = [str(df.at[i, '標題'] or '') for i in valid_idx]
    briefs = [str(df.at[i, '商品簡述'] or '') if '商品簡述' in df.columns else '' for i in valid_idx]
    orig_details = [str(df.at[i, '說明'] or '') for i in valid_idx]
    # 分類: 優先 Yahoo「拍賣類別名稱」(已映射), 備援原採集「淘宝分類名稱」
    categories = []
    for i in valid_idx:
        cat = ''
        if '拍賣類別名稱' in df.columns:
            cat = str(df.at[i, '拍賣類別名稱'] or '').strip()
        if not cat and '淘宝分類名稱' in df.columns:
            cat = str(df.at[i, '淘宝分類名稱'] or '').strip()
        categories.append(cat)
    img_paths = []          # 第一張 (給 Stage C multimodal SEO)
    all_img_paths = []      # 所有圖列表 (給 Stage E reject scan)
    for i in valid_idx:
        field = str(df.at[i, '圖片'] or '') if '圖片' in df.columns else ''
        paths = [p.strip() for p in field.split('|') if p.strip()]
        img_paths.append(paths[0] if paths else '')
        all_img_paths.append(paths)

    SEO_MODEL = 'gpt-5.5'
    # ★ Bug A 修: 一次性 patch translator._chat 加 monitor 通報, Stage A/B/D 都自動受惠
    if not getattr(translator, '_chat_monitored_v65', False):
        from checkpoint_manager import get_monitor as _get_mon, PauseException as _PE
        _orig_chat = translator._chat
        def _patched_chat(body, timeout=180):
            import time as _t
            _t0 = _t.time()
            try:
                r = _orig_chat(body, timeout)
                try: _get_mon().record_success()
                except _PE: raise  # 不應在 success 時 raise, 但保險
                except Exception: pass
                return r
            except _PE:
                # PauseException 直接傳上去, 不要當 _orig_chat 失敗處理
                raise
            except Exception as e:
                # ★ 4.0.30: 純連線層失敗 (VPN 抖) 不通報 monitor, 避免 8 次累積誤觸發 PauseException
                # 之前 4.0.27 只 fix 了 image_optimizer, 沒 fix 這條 path → Stage A unified batch
                # 連續 fail 仍 cascade. 4.0.30 用共用 utils.is_network_blip 統一處理.
                try:
                    from processors.utils import is_network_blip
                    _elapsed = _t.time() - _t0
                    is_blip = is_network_blip(e, elapsed=_elapsed)
                except Exception:
                    is_blip = False
                if not is_blip:
                    try: _get_mon().record_fail(exception=e, error_message=str(e)[:200])
                    except _PE: raise
                    except Exception: pass
                raise e
        translator._chat = _patched_chat
        translator._chat_monitored_v65 = True

    def chat_fn(body, timeout=180):
        body['model'] = SEO_MODEL
        return translator._chat(body, timeout)
    def chat_fn_unified(body):
        body['model'] = SEO_MODEL
        return translator._chat(body, 180)

    # === Stage A: 清洗 detail + 簡述 + 文字違禁 ===
    log_fn(f"[V65 Stage A] 清洗說明 + 簡述 + 文字違禁判定 (含商品分類交叉驗證)...")
    items_a = [{
        'title': titles[i],
        'brief': briefs[i],
        'detail': orig_details[i],
        'category': categories[i],
        'code': str(df.at[valid_idx[i], '商品條碼'])[:20] if '商品條碼' in df.columns else '',
    } for i in range(n)]
    codes = [str(df.at[valid_idx[i], '商品條碼'])[:20] if '商品條碼' in df.columns else f'IDX{i}' for i in range(n)]
    ta0 = time.time()
    # ★ Checkpoint: 用 df 暫存欄位 _v65_a_detail / _v65_a_subtitle / _v65_a_forbidden 持久化結果
    for col in ['_v65_a_detail', '_v65_a_subtitle', '_v65_a_forbidden']:
        if col not in df.columns:
            df[col] = ''
    # 先從 df 讀已 done 的 (resume 後)
    pre_done_indices = []
    if ckpt is not None:
        for i in range(n):
            if ckpt.is_done('stage_a', codes[i]):
                pre_done_indices.append(i)
        if pre_done_indices:
            log_fn(f'  [Stage A] ⏭ Resume 已完成 {len(pre_done_indices)} 件, 待跑 {n - len(pre_done_indices)} 件')
    pending_local_idx = [i for i in range(n) if i not in set(pre_done_indices)]
    items_a_pending = [items_a[i] for i in pending_local_idx]
    # callback: batch 完成後即時寫 df + ckpt save
    _save_counter = [0]
    def _on_batch_a(res):
        # res = {position_in_pending_list: {subtitle, detail_clean, forbidden}}
        # ★ 個別 item: detail_clean+subtitle 任一非空 = 成功; 全空 = AI 給空 = 失敗 (重跑)
        # 注意: 整個 batch fail (chat_fn raise) 的話 res 為空 dict, 沒對應 entry → 不 mark → 下次重跑 ✅
        for pending_pos, val in res.items():
            global_i = pending_local_idx[pending_pos]
            df_idx = valid_idx[global_i]
            det = val.get('detail_clean', '')
            sub = val.get('subtitle', '')
            df.at[df_idx, '_v65_a_detail'] = det
            df.at[df_idx, '_v65_a_subtitle'] = sub
            df.at[df_idx, '_v65_a_forbidden'] = val.get('forbidden', '否')
            if ckpt is not None:
                try:
                    if det or sub:
                        ckpt.mark_done('stage_a', codes[global_i])
                    else:
                        ckpt.mark_failed('stage_a', codes[global_i])
                except: pass
            _save_counter[0] += 1
        # 每 20 件 save 一次
        if ckpt is not None and _save_counter[0] >= 20:
            try: ckpt.save(df=df); _save_counter[0] = 0
            except: pass
    # ★ Stage A = chat_medium (batch 10件, avg ~10s)
    try:
        from checkpoint_manager import get_optimal_workers
        stage_a_workers, w_info_a = get_optimal_workers(task_type='chat_medium', warmup=True, max_cap=96)
        # ★ 4.0.32: hub RTT-aware cap (218ms+ 砍到 30 避免 TLS 撐爆)
        try:
            from processors.utils import apply_hub_cap as _hcap
            _capped = _hcap(stage_a_workers)
            if _capped < stage_a_workers:
                log_fn(f'  [Stage A] 中介建議 {stage_a_workers} → hub RTT cap → {_capped}')
                stage_a_workers = _capped
        except Exception: pass
        log_fn(f'  [Stage A] 動態並發: {stage_a_workers} (來源={w_info_a.get("source")}, '
               f'active_accts={w_info_a.get("codex_active_accounts", "?")}, my_share={w_info_a.get("my_rpm_share", "?")})')
    except Exception:
        stage_a_workers = 96
    unified_pending = gen_unified_ai(items_a_pending, chat_fn_unified, batch_size=10, workers=stage_a_workers,
                                     log_fn=log_fn, on_batch_done=_on_batch_a) if items_a_pending else []
    # 最後 flush
    if ckpt is not None and _save_counter[0] > 0:
        try: ckpt.save(df=df)
        except: pass
    # 從 df 讀回完整結果 (混 done + 新跑)
    detail_cleans = []
    subtitles = []
    forbiddens_text = []
    for i in range(n):
        df_idx = valid_idx[i]
        detail_cleans.append(str(df.at[df_idx, '_v65_a_detail'] or ''))
        subtitles.append(str(df.at[df_idx, '_v65_a_subtitle'] or ''))
        fb = str(df.at[df_idx, '_v65_a_forbidden'] or '否')
        forbiddens_text.append(fb if fb else '否')
    log_fn(f"[V65 Stage A] {time.time()-ta0:.0f}s | 文字違禁: {sum(1 for f in forbiddens_text if f and f!='否')} 件")

    # === Stage B: 主副詞 (砍原標題! 只用清洗後簡述+說明60字, 60字是權重核心) ===
    log_fn(f"[V65 Stage B] 主副詞 (清洗後簡述當標題 + 清洗後說明前60字當屬性, 砍原標題)...")
    # ★ Checkpoint: 用 df 暫存 _v65_b_pri / _v65_b_sec
    for col in ['_v65_b_pri', '_v65_b_sec']:
        if col not in df.columns:
            df[col] = ''
    primary = [''] * n
    secondary = [''] * n
    # Stage B 是整批 single call, 用 stage-level ckpt: 全部 done 才視為完成
    stage_b_all_done = False
    if ckpt is not None:
        stage_b_all_done = all(ckpt.is_done('stage_b', codes[i]) for i in range(n)) and n > 0
    if stage_b_all_done:
        log_fn(f'  [Stage B] ⏭ Resume 全部已完成, 從 df 載回')
        for i in range(n):
            df_idx = valid_idx[i]
            primary[i] = str(df.at[df_idx, '_v65_b_pri'] or '')
            secondary[i] = str(df.at[df_idx, '_v65_b_sec'] or '')
    else:
        fake_titles = subtitles  # 清洗後簡述當"標題", 不送原標題
        attrs_combined = [detail_cleans[i][:60] for i in range(n)]  # 60字權重核心
        translator.CFG.seo_model = SEO_MODEL
        tb0 = time.time()
        primary, secondary = translator._extract_seo_keywords(fake_titles, log_fn, attrs=attrs_combined)
        # 跑完即時寫 df + mark done/failed (per-item)
        # ★ primary[i] 非空 = 成功; 空 = 失敗 → mark_failed 下次重跑
        if ckpt is not None:
            for i in range(n):
                df_idx = valid_idx[i]
                df.at[df_idx, '_v65_b_pri'] = primary[i] or ''
                df.at[df_idx, '_v65_b_sec'] = secondary[i] or ''
                try:
                    if primary[i]:
                        ckpt.mark_done('stage_b', codes[i])
                    else:
                        ckpt.mark_failed('stage_b', codes[i])
                except: pass
            try: ckpt.save(df=df)
            except: pass
        log_fn(f"[V65 Stage B] {time.time()-tb0:.0f}s | 主詞 {sum(1 for p in primary if p)}/{n}")

    # === Stage C: ★ multimodal SEO + 視覺違禁 ===
    log_fn(f"[V65 Stage C] multimodal SEO 標題 + 視覺違禁判定...")
    tc0 = time.time()
    # ★ Checkpoint: 用 df 暫存欄位持久化結果
    for col in ['_v65_c_seo', '_v65_c_fv', '_v65_c_note', '_v65_c_rj', '_v65_c_ifp']:
        if col not in df.columns:
            df[col] = ''
    seo_titles = [''] * n
    forbidden_visuals = ['否'] * n
    visual_notes = [''] * n
    reject_firsts = [False] * n
    is_full_products = [True] * n
    # 預先讀已 done 的 row
    pending_c = []
    if ckpt is not None:
        for i in range(n):
            if ckpt.is_done('stage_c', codes[i]):
                df_idx = valid_idx[i]
                seo_titles[i] = str(df.at[df_idx, '_v65_c_seo'] or '')
                forbidden_visuals[i] = str(df.at[df_idx, '_v65_c_fv'] or '否')
                visual_notes[i] = str(df.at[df_idx, '_v65_c_note'] or '')
                rj_v = str(df.at[df_idx, '_v65_c_rj'] or 'False')
                reject_firsts[i] = rj_v.strip().lower() in ('true','1','yes')
                ifp_v = str(df.at[df_idx, '_v65_c_ifp'] or 'True')
                is_full_products[i] = ifp_v.strip().lower() in ('true','1','yes')
            else:
                pending_c.append(i)
        if len(pending_c) < n:
            log_fn(f'  [Stage C] ⏭ Resume 已完成 {n - len(pending_c)} 件, 待跑 {len(pending_c)} 件')
    else:
        pending_c = list(range(n))
    done_c = 0
    save_counter_c = [0]
    save_lock = __import__('threading').Lock()
    def _run_c(i):
        try:
            from processors.feedback_collector import make_xtrace_single
            xtrace = make_xtrace_single('stage_c', codes[i] if i < len(codes) else f'IDX{i}', 0)
        except Exception:
            xtrace = ''
        return i, _seo_visual_multimodal(
            titles[i], subtitles[i], detail_cleans[i],
            primary[i], secondary[i], briefs[i], img_paths[i],
            translator.SEO_SYSTEM_PROMPT,
            category=categories[i],
            xtrace_header=xtrace,
        )
    try:
        from checkpoint_manager import get_monitor, get_optimal_workers
        _mon_c = get_monitor()
        # Stage C = chat_short (per-item multimodal, avg ~5s)
        stage_c_workers, w_info_c = get_optimal_workers(task_type='chat_short', warmup=False, max_cap=96)
        # ★ 4.0.32: hub RTT-aware cap
        try:
            from processors.utils import apply_hub_cap as _hcap
            _capped = _hcap(stage_c_workers)
            if _capped < stage_c_workers:
                log_fn(f'  [Stage C] 中介建議 {stage_c_workers} → hub RTT cap → {_capped}')
                stage_c_workers = _capped
        except Exception: pass
        log_fn(f'  [Stage C] 動態並發: {stage_c_workers} (來源={w_info_c.get("source")})')
    except Exception:
        _mon_c = None
        stage_c_workers = 96
    # ★ 4.0.36: AdaptiveCap (Stage C 受 RTT 影響, 多模態圖+chat). pool max 設高一點留 ratchet up 空間
    # ★ 4.0.38: 加 success_check — _seo_visual_multimodal 內部 swallow exception 並 return 空 seo,
    #   make_adaptive_worker 默認看 exception 看不到. success_check 從 result 判.
    try:
        from processors.utils import get_or_create_adaptive_cap, make_adaptive_worker
        _max_c = max(stage_c_workers * 2, 48)
        _ac_c = get_or_create_adaptive_cap('stage_c', initial_cap=stage_c_workers, max_cap=_max_c, min_cap=8)
        _ac_c.set_log_fn(log_fn)
        log_fn(f'  [Stage C] adaptive cap 啟用: 起點={stage_c_workers}, max={_max_c}, min=8')
        # _run_c return: (i, (seo, fv, note, rj, ifp))
        # success = 拿到 seo 標題 (空字串 = API fail, 內部 swallow 過)
        def _stage_c_success(r):
            if not r or len(r) < 2: return False
            payload = r[1]
            if not payload or len(payload) < 1: return False
            seo = payload[0]
            return bool(seo and str(seo).strip())
        _adaptive_run_c = make_adaptive_worker(_ac_c, _run_c, success_check=_stage_c_success)
        _pool_max_c = _max_c
    except Exception as _e:
        _ac_c = None
        _adaptive_run_c = _run_c
        _pool_max_c = stage_c_workers
        log_fn(f'  [Stage C] adaptive cap init 失敗 (退回靜態): {_e}')
    with ThreadPoolExecutor(max_workers=_pool_max_c) as pool:
        futs = [pool.submit(_adaptive_run_c, i) for i in pending_c]
        for f in as_completed(futs):
            if _mon_c and getattr(_mon_c, 'user_stop', False):
                # 4.0.36: 喚醒 adaptive cap acquire 等待者, 否則 with-block 退出 wait=True 會 deadlock
                # ★ 4.0.41: 加 cancel_futures=True (跟 K0/Stage E 對齊). 否則 pending future 還會
                #   被 schedule 一輪 (acquire 後 return None), 慢但不 deadlock — 用戶按停止時感受卡.
                if _ac_c is not None:
                    try: _ac_c.shutdown()
                    except Exception: pass
                pool.shutdown(wait=False, cancel_futures=True)
                log_fn(f'  [Stage C] 收到用戶停止, 提早結束 (已跑 {done_c}/{len(pending_c)})')
                break
            i, (t, fv, note, rj, ifp) = f.result()
            seo_titles[i] = t
            forbidden_visuals[i] = fv
            visual_notes[i] = note
            reject_firsts[i] = rj
            is_full_products[i] = ifp
            # ★ 即時寫 df + mark done/failed (per-item)
            # seo_title 非空 = 成功; 空 = API 失敗 (_seo_visual_multimodal 失敗回 ('','否','',False,True))
            if ckpt is not None:
                df_idx = valid_idx[i]
                df.at[df_idx, '_v65_c_seo'] = t
                df.at[df_idx, '_v65_c_fv'] = fv
                df.at[df_idx, '_v65_c_note'] = note
                df.at[df_idx, '_v65_c_rj'] = str(bool(rj))
                df.at[df_idx, '_v65_c_ifp'] = str(bool(ifp))
                try:
                    if t:
                        ckpt.mark_done('stage_c', codes[i])
                    else:
                        ckpt.mark_failed('stage_c', codes[i])
                except: pass
                with save_lock:
                    save_counter_c[0] += 1
                    if save_counter_c[0] >= 20:
                        try: ckpt.save(df=df); save_counter_c[0] = 0
                        except: pass
            done_c += 1
            if done_c == 1 or done_c % max(1, len(pending_c) // 10 if pending_c else 1) == 0 or done_c == len(pending_c):
                log_fn(f'  [V65 Stage C] {done_c}/{len(pending_c)} (Resume 後)')
    # 最後 flush
    if ckpt is not None and save_counter_c[0] > 0:
        try: ckpt.save(df=df)
        except: pass
    n_visual_forbid = sum(1 for f in forbidden_visuals if f and f != '否')
    n_reject_first = sum(1 for r in reject_firsts if r)
    n_close_up = sum(1 for ifp in is_full_products if not ifp)
    log_fn(f"[V65 Stage C] {time.time()-tc0:.0f}s | 視覺違禁: {n_visual_forbid} 件 | 1.jpg 該刪: {n_reject_first} 件 | 局部特寫: {n_close_up} 件")
    # ★ 詳情寫到詳細日誌檔 (GUI 不刷屏, 用戶 audit 看檔案)
    if n_reject_first > 0:
        log_detail(f'[V65 Stage C] 1.jpg 該刪詳情 ({n_reject_first} 件):')
        for j in range(n):
            if reject_firsts[j]:
                p = img_paths[j] or ''
                code = os.path.basename(os.path.dirname(p)) if p else '?'
                note = (visual_notes[j] or '')[:60]
                log_detail(f'    🗑 {code}/1.jpg | {note}')
    if n_close_up > 0:
        log_detail(f'[V65 Stage C] 局部特寫詳情 ({n_close_up} 件, 跳過 image_opt 重生):')
        for j in range(n):
            if not is_full_products[j]:
                p = img_paths[j] or ''
                code = os.path.basename(os.path.dirname(p)) if p else '?'
                note = (visual_notes[j] or '')[:60]
                log_detail(f'    🔍 {code}/1.jpg | {note}')
    if n_visual_forbid > 0:
        log_detail(f'[V65 Stage C] 視覺違禁詳情 ({n_visual_forbid} 件):')
        for j in range(n):
            fv = (forbidden_visuals[j] or '否').strip()
            if fv != '否':
                p = img_paths[j] or ''
                code = os.path.basename(os.path.dirname(p)) if p else '?'
                log_detail(f'    ❌ {code}/1.jpg | 違禁類: {fv}')

    # === Stage D: 5 標籤 (從 SEO+簡述+乾淨說明+主副詞 抽, 主副詞=客戶搜尋核心) ===
    log_fn(f"[V65 Stage D] 5 標籤 (用主副詞當參考)...")
    # ★ Checkpoint: 用 df 暫存欄位 _v65_d_tags 持久化結果 (用 | 分隔的字串)
    if '_v65_d_tags' not in df.columns:
        df['_v65_d_tags'] = ''
    all_tags = [[] for _ in range(n)]
    pending_d_indices = []
    if ckpt is not None:
        for i in range(n):
            if ckpt.is_done('stage_d', codes[i]):
                df_idx = valid_idx[i]
                tag_str = str(df.at[df_idx, '_v65_d_tags'] or '')
                all_tags[i] = [t for t in tag_str.split('|') if t.strip()]
            else:
                pending_d_indices.append(i)
        if len(pending_d_indices) < n:
            log_fn(f'  [Stage D] ⏭ Resume 已完成 {n - len(pending_d_indices)} 件, 待跑 {len(pending_d_indices)} 件')
    else:
        pending_d_indices = list(range(n))
    items_d = [{
        'seo_title': seo_titles[i],
        'subtitle': subtitles[i],
        'detail_clean': detail_cleans[i],
        'main': primary[i],
        'sub': secondary[i],
        'category': categories[i],
    } for i in pending_d_indices]
    batches_d = [(i, items_d[i:i+10]) for i in range(0, len(items_d), 10)]
    td0 = time.time()
    done_d = 0
    total_d = len(batches_d)
    save_counter_d = [0]
    save_lock_d = __import__('threading').Lock()
    try:
        from checkpoint_manager import get_monitor, get_optimal_workers
        _mon_d = get_monitor()
        # Stage D = chat_medium (batch 10件, avg ~10s)
        stage_d_workers, w_info_d = get_optimal_workers(task_type='chat_medium', warmup=False, max_cap=96)
        # ★ 4.0.32: hub RTT-aware cap
        try:
            from processors.utils import apply_hub_cap as _hcap
            _capped = _hcap(stage_d_workers)
            if _capped < stage_d_workers:
                log_fn(f'  [Stage D] 中介建議 {stage_d_workers} → hub RTT cap → {_capped}')
                stage_d_workers = _capped
        except Exception: pass
        log_fn(f'  [Stage D] 動態並發: {stage_d_workers} (來源={w_info_d.get("source")})')
    except Exception:
        _mon_d = None
        stage_d_workers = 96
    with ThreadPoolExecutor(max_workers=stage_d_workers) as pool:
        futs = {pool.submit(_gen_tags_batch, b, translator._chat): (start, b) for start, b in batches_d}
        for f in as_completed(futs):
            if _mon_d and getattr(_mon_d, 'user_stop', False):
                log_fn(f'  [Stage D] 收到用戶停止, 提早結束 (已跑 {done_d}/{total_d} batches)')
                break
            start, _ = futs[f]
            res = f.result()
            for pos, tags in res.items():
                global_i = pending_d_indices[start + pos]
                all_tags[global_i] = tags
                # ★ 即時寫 df + mark done/failed (per-item)
                # tags 非空 list = 成功; 空 list = AI 失敗
                # 注意: batch 整個 fail 時 res = {}, 該 batch 的 items 不會被 mark → 下次重跑 ✅
                if ckpt is not None:
                    df_idx = valid_idx[global_i]
                    df.at[df_idx, '_v65_d_tags'] = '|'.join(tags) if tags else ''
                    try:
                        if tags:
                            ckpt.mark_done('stage_d', codes[global_i])
                        else:
                            ckpt.mark_failed('stage_d', codes[global_i])
                    except: pass
                    with save_lock_d:
                        save_counter_d[0] += 1
                        if save_counter_d[0] >= 20:
                            try: ckpt.save(df=df); save_counter_d[0] = 0
                            except: pass
            done_d += 1
            if done_d == 1 or done_d % max(1,total_d//5) == 0 or done_d == total_d:
                log_fn(f'  [V65 Stage D] {done_d}/{total_d} batch')
    if ckpt is not None and save_counter_d[0] > 0:
        try: ckpt.save(df=df)
        except: pass
    n_tag_ok = sum(1 for t in all_tags if t)
    log_fn(f"[V65 Stage D] {time.time()-td0:.0f}s | 標籤 {n_tag_ok}/{n}")

    # === Stage E: ★ 全圖 reject 判定 (對非違禁商品的所有圖跑短 prompt) ===
    if not enable_stage_e:
        log_fn(f"[V65 Stage E] 跳過 (用戶關閉, 只跑 Stage A-D)")
    else:
        log_fn(f"[V65 Stage E] 全圖 reject 判定 (對所有圖跑判定)...")
    te0 = time.time()
    # 構造任務: (j, img_index, img_path)
    # ★ A. 跳過 1.jpg (img_i=0) — Stage C 已用完整 multimodal 判過, 結果在 reject_firsts
    # ★ B. 跳過違禁商品 (Stage A/C 已標)
    scan_tasks = []
    if enable_stage_e:  # Stage E 關閉時不掃, 只用 Stage C 的 reject_firsts
        for j in range(n):
            ft = (forbiddens_text[j] or '否').strip()
            fv = (forbidden_visuals[j] or '否').strip()
            if ft != '否' or fv != '否': continue
            for img_i, p in enumerate(all_img_paths[j]):
                if img_i == 0: continue  # ★ A 跳過 1.jpg
                if p and os.path.exists(p):
                    scan_tasks.append((j, img_i, p))

    # ★ Checkpoint: 用 df 暫存欄位 _v65_e_rj 持久化 reject indices (避免 paused resume 後丟失)
    if '_v65_e_rj' not in df.columns:
        df['_v65_e_rj'] = ''
    reject_indices_per_item = [[] for _ in range(n)]  # j → [img_index list]
    # 先把 Stage C 對 1.jpg 的判定 (reject_firsts) 合併進來
    for j in range(n):
        if reject_firsts[j]:
            reject_indices_per_item[j].append(0)
    # ★ Resume: 從 df 載回已 done 的 reject 結果
    if ckpt is not None:
        for j in range(n):
            df_idx = valid_idx[j]
            saved_rj = str(df.at[df_idx, '_v65_e_rj'] or '')
            if saved_rj:
                try:
                    saved_indices = [int(x) for x in saved_rj.split(',') if x.strip()]
                    for si in saved_indices:
                        if si not in reject_indices_per_item[j]:
                            reject_indices_per_item[j].append(si)
                except: pass
    n_scanned, n_rejected = 0, sum(len(rl) for rl in reject_indices_per_item)
    if scan_tasks:
        # batch=6: 6 張圖 1 call, 省 40-45% wall (300 樣本驗證 batch=6 vs single Recall/FP 完全一致)
        # ★ 4.0.82 Phase 1: 6 → 4. audit 顯示 stage_e_batch 中介 OH P95=24s, 推測 chat slot 排隊 +
        #   大 body (6 × base64 ~6MB) 上傳慢. 縮小 batch size 33% 減 body, 減 slot 占用, 預期 OH 大幅降.
        #   throughput trade-off: 省 wall-time 從 40-45% 略降到 ~30%, 但 P95 OH 改善遠超.
        BATCH = 4
        # ★ Checkpoint: 跳過已完成 (resume 時)
        if ckpt is not None:
            before = len(scan_tasks)
            scan_tasks = [t for t in scan_tasks if not ckpt.is_done('stage_e', t[2])]
            skipped = before - len(scan_tasks)
            if skipped > 0:
                log_fn(f'  [Stage E] ⏭ Resume 跳過 {skipped} 張已完成')
        chunks = [scan_tasks[i:i+BATCH] for i in range(0, len(scan_tasks), BATCH)]
        log_fn(f'  [Stage E] 待掃 {len(scan_tasks)} 張圖 (batch={BATCH} → {len(chunks)} calls)')
        done_e = 0
        n_imgs_done = 0
        _stage_e_t0 = time.time()
        rj_save_lock = __import__('threading').Lock()
        def _run_batch(chunk):
            paths = [t[2] for t in chunk]
            # X-Trace: chunk 是 [(j, img_i, p), ...], j 對應 codes[j] (商品條碼)
            try:
                from processors.feedback_collector import make_xtrace_batch
                items = [(codes[t[0]] if t[0] < len(codes) else f'IDX{t[0]}', t[1]) for t in chunk]
                xtrace = make_xtrace_batch('stage_e_batch', items)
            except Exception:
                xtrace = ''
            results = _reject_scan_batch(paths, xtrace_header=xtrace)
            return chunk, results
        # ★ Stage E = chat_short (batch=6 圖, avg ~5-8s)
        try:
            from checkpoint_manager import get_monitor, get_optimal_workers
            _mon_e = get_monitor()
            stage_e_workers, w_info = get_optimal_workers(task_type='chat_short', warmup=False, max_cap=96)
            # ★ 4.0.32: hub RTT-aware cap
            try:
                from processors.utils import apply_hub_cap as _hcap
                _capped = _hcap(stage_e_workers)
                if _capped < stage_e_workers:
                    log_fn(f'  [Stage E] 中介建議 {stage_e_workers} → hub RTT cap → {_capped}')
                    stage_e_workers = _capped
            except Exception: pass
            log_fn(f'  [Stage E] 動態並發: {stage_e_workers} (來源={w_info.get("source")})')
        except Exception:
            _mon_e = None
            stage_e_workers = 96
        # ★ 4.0.36: AdaptiveCap (Stage E 量大 — 1000+ 張圖時動態調並發)
        # ★ 4.0.38: success_check — _reject_scan_batch fail return None, _run_batch 包成 (chunk, None)
        try:
            from processors.utils import get_or_create_adaptive_cap, make_adaptive_worker
            _max_e = max(stage_e_workers * 2, 48)
            _ac_e = get_or_create_adaptive_cap('stage_e', initial_cap=stage_e_workers, max_cap=_max_e, min_cap=8)
            _ac_e.set_log_fn(log_fn)
            log_fn(f'  [Stage E] adaptive cap 啟用: 起點={stage_e_workers}, max={_max_e}, min=8')
            # _run_batch return: (chunk, results) — results=None 表示 API call fail
            def _stage_e_success(r):
                if not r or len(r) < 2: return False
                return r[1] is not None
            _adaptive_run_batch = make_adaptive_worker(_ac_e, _run_batch, success_check=_stage_e_success)
            _pool_max_e = _max_e
        except Exception as _e:
            _ac_e = None
            _adaptive_run_batch = _run_batch
            _pool_max_e = stage_e_workers
            log_fn(f'  [Stage E] adaptive cap init 失敗 (退回靜態): {_e}')
        pool = ThreadPoolExecutor(max_workers=_pool_max_e)
        futs = [pool.submit(_adaptive_run_batch, c) for c in chunks]
        try:
            for f in as_completed(futs):
                # ★ 用戶按停止 → 取消 pending + 提早結束
                if _mon_e and getattr(_mon_e, 'user_stop', False):
                    # 4.0.36: 喚醒 adaptive cap acquire 等待者, 否則 finally pool.shutdown(wait=True) deadlock
                    if _ac_e is not None:
                        try: _ac_e.shutdown()
                        except Exception: pass
                    pool.shutdown(wait=False, cancel_futures=True)
                    log_fn(f'  [Stage E] 收到用戶停止, 提早結束 (已跑 {done_e}/{len(chunks)} batches, 取消 pending)')
                    break
                chunk, results = f.result()
                # ★ API 失敗時 _reject_scan_batch 回 None → 整 chunk 不 mark, 下次重跑
                if results is None:
                    if ckpt is not None:
                        for (j, img_i, p) in chunk:
                            try: ckpt.mark_failed('stage_e', p)
                            except: pass
                    done_e += 1
                    n_imgs_done += len(chunk)
                    continue
                # 收集本 batch 涉及的 j 集合, 之後一次更新 df
                affected_j = set()
                for (j, img_i, p), rj in zip(chunk, results):
                    if rj:
                        reject_indices_per_item[j].append(img_i)
                        n_rejected += 1
                        affected_j.add(j)
                    # ★ Checkpoint mark done (成功 = 知道結果不論 reject 與否)
                    if ckpt is not None:
                        try: ckpt.mark_done('stage_e', p)
                        except: pass
                # ★ 把本 batch 涉及到的 j 的 reject indices 寫進 df 暫存
                if ckpt is not None and affected_j:
                    with rj_save_lock:
                        for j in affected_j:
                            df_idx = valid_idx[j]
                            df.at[df_idx, '_v65_e_rj'] = ','.join(str(x) for x in sorted(set(reject_indices_per_item[j])))
                done_e += 1
                n_imgs_done += len(chunk)
                # 每 10 個 batch (~60 張圖) save 一次
                if ckpt is not None and done_e % 10 == 0:
                    try: ckpt.save(df=df)
                    except: pass
                # ★ print 加密 (4.0.14): 改成每 5% 進度一次 (約 20 個點), 加 ETA
                #    Stage E 整體只跑 ~20s, user 之前每 10% 才看一次, 容易誤判為卡住
                _print_every = max(1, len(chunks) // 20)
                if done_e == 1 or done_e % _print_every == 0 or done_e == len(chunks):
                    elapsed = time.time() - _stage_e_t0
                    if done_e > 0 and elapsed > 0.5:
                        eta = elapsed * (len(chunks) - done_e) / done_e
                        eta_str = f' | 已 {elapsed:.0f}s, ETA {eta:.0f}s' if eta >= 1 else f' | 已 {elapsed:.0f}s'
                    else:
                        eta_str = ''
                    log_fn(f'  [Stage E] batch {done_e}/{len(chunks)} ({n_imgs_done}/{len(scan_tasks)} 張) | 已 reject: {n_rejected}{eta_str}')
        finally:
            pool.shutdown(wait=True)
        # 最後 flush
        if ckpt is not None:
            try: ckpt.save(df=df)
            except: pass
        n_scanned = len(scan_tasks)
    log_fn(f"[V65 Stage E] {time.time()-te0:.0f}s | 掃 {n_scanned} 張 | reject {n_rejected}")

    # ★ V65 寫回 df 前: 自動 retry Stage A/C 失敗的 items (避免一次失敗就丟)
    # Stage A 失敗 = detail+subtitle 都空; Stage C 失敗 = seo_title 空
    failed_a_indices = [j for j in range(n) if (not detail_cleans[j]) and (not subtitles[j])]
    failed_c_indices = [j for j in range(n) if not seo_titles[j]]
    if failed_a_indices or failed_c_indices:
        log_fn(f'[V65] 🔄 文字處理 auto-retry: Stage A 失敗 {len(failed_a_indices)} 件, Stage C 失敗 {len(failed_c_indices)} 件')
        log_fn(f'  等 30s 讓中介穩定...')
        time.sleep(30)
        # Stage A retry: 用同樣 chat_fn_unified 跑失敗的 items
        if failed_a_indices:
            items_a_retry = [items_a[j] for j in failed_a_indices]
            try:
                unified_retry = gen_unified_ai(items_a_retry, chat_fn_unified, batch_size=10, workers=stage_a_workers, log_fn=log_fn)
                rt_ok = 0
                for new_pos, val in enumerate(unified_retry):
                    if not val: continue
                    j = failed_a_indices[new_pos]
                    det = val.get('detail_clean', '')
                    sub = val.get('subtitle', '')
                    fb = val.get('forbidden', '否')
                    if det or sub:
                        detail_cleans[j] = det
                        subtitles[j] = sub
                        forbiddens_text[j] = fb
                        df.at[valid_idx[j], '_v65_a_detail'] = det
                        df.at[valid_idx[j], '_v65_a_subtitle'] = sub
                        df.at[valid_idx[j], '_v65_a_forbidden'] = fb
                        if ckpt: ckpt.mark_done('stage_a', codes[j])
                        rt_ok += 1
                log_fn(f'  Stage A retry: 救回 {rt_ok}/{len(failed_a_indices)}')
            except Exception as e:
                # ★ 4.0.74: PauseException 必須往上傳, 否則 monitor 觸發 8 次暫停會被吞 → 無限 retry
                if 'PauseException' in type(e).__name__: raise
                log_fn(f'  Stage A retry 失敗: {str(e)[:80]}')
        # Stage C retry: per-item
        if failed_c_indices:
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
            def _rt_c(j):
                try:
                    from processors.feedback_collector import make_xtrace_single
                    xtrace = make_xtrace_single('stage_c', codes[j] if j < len(codes) else f'IDX{j}', 0, retry=1)
                except Exception:
                    xtrace = ''
                return j, _seo_visual_multimodal(
                    titles[j], subtitles[j], detail_cleans[j],
                    primary[j], secondary[j], briefs[j], img_paths[j],
                    translator.SEO_SYSTEM_PROMPT, category=categories[j],
                    xtrace_header=xtrace)
            try:
                with _TPE(max_workers=stage_c_workers) as _rp:
                    _futs = [_rp.submit(_rt_c, j) for j in failed_c_indices]
                    rt_ok = 0
                    for _f in _ac(_futs):
                        j, (t, fv, note, rj, ifp) = _f.result()
                        if t:
                            seo_titles[j] = t
                            forbidden_visuals[j] = fv
                            visual_notes[j] = note
                            reject_firsts[j] = rj
                            is_full_products[j] = ifp
                            df.at[valid_idx[j], '_v65_c_seo'] = t
                            df.at[valid_idx[j], '_v65_c_fv'] = fv
                            if ckpt: ckpt.mark_done('stage_c', codes[j])
                            rt_ok += 1
                    log_fn(f'  Stage C retry: 救回 {rt_ok}/{len(failed_c_indices)}')
            except Exception as e:
                # ★ 4.0.74: PauseException 必須往上傳 (用戶 5-11 撞 fails=80 沒暫停就是吞這裡)
                if 'PauseException' in type(e).__name__: raise
                log_fn(f'  Stage C retry 失敗: {str(e)[:80]}')
        if ckpt:
            try: ckpt.save(df=df)
            except: pass

    # === 寫回 df ===
    if '_reject_first_img' not in df.columns:
        df['_reject_first_img'] = False
    if '_reject_indices' not in df.columns:
        df['_reject_indices'] = ''
    if '_skip_image_opt' not in df.columns:
        df['_skip_image_opt'] = False
    n_optimized = 0
    n_text_fail = 0
    text_fail_codes = []
    for j, idx in enumerate(valid_idx):
        # 違禁優先 (文字 OR 視覺)
        ft = (forbiddens_text[j] or '否').strip()
        fv = (forbidden_visuals[j] or '否').strip()
        if ft != '否':
            df.at[idx, '_filter_reason'] = append_reason(df.at[idx, '_filter_reason'], f'違禁品-{ft}')
            continue
        if fv != '否':
            df.at[idx, '_filter_reason'] = append_reason(df.at[idx, '_filter_reason'], f'視覺違禁-{fv}')
            continue
        # ★ 文字處理失敗攔截 (Stage A 沒清洗 / Stage C 沒生 SEO 標題 → 不放行)
        # Stage A 失敗 → detail_clean 空 + subtitle 空 (兩個都空才算 Stage A 失敗, 因為 Stage A 一個 batch fail 通常兩個都沒)
        # Stage C 失敗 → SEO 標題 空
        stage_a_failed = (not detail_cleans[j]) and (not subtitles[j])
        stage_c_failed = not seo_titles[j]
        if stage_a_failed or stage_c_failed:
            reasons = []
            if stage_a_failed: reasons.append('Stage A 清洗失敗')
            if stage_c_failed: reasons.append('Stage C 標題失敗')
            df.at[idx, '_filter_reason'] = append_reason(df.at[idx, '_filter_reason'], f'V65 文字處理失敗: {"+".join(reasons)}')
            n_text_fail += 1
            try:
                code = str(df.at[idx, '商品條碼'])[:20]
                text_fail_codes.append(code)
            except: pass
            continue
        # 正常寫回 (★ 程式後處理: 移除原文沒提的品牌幻覺, 保留 vintage 風格學詞)
        if seo_titles[j]:
            source_text = (titles[j] or '') + ' ' + (briefs[j] or '') + ' ' + (orig_details[j] or '')
            cleaned_title = _remove_brand_hallucination(seo_titles[j], source_text)
            df.at[idx, '標題'] = cleaned_title
        if subtitles[j]:
            if '商品簡述' not in df.columns: df['商品簡述'] = ''
            df.at[idx, '商品簡述'] = subtitles[j]
        if all_tags[j]:
            # ─── 節日動態 boost: 適合的商品塞 1 個節日 tag (取代最低相關性那個) ───
            tags_final = all_tags[j]
            if _SEASONAL_AVAILABLE and '拍賣類別名稱' in df.columns:
                try:
                    cat = str(df.at[idx, '拍賣類別名稱'] or '')
                    if cat:
                        tags_final = _seasonal_boost(cat, all_tags[j])
                except Exception:
                    tags_final = all_tags[j]  # 失敗 fallback 用原 tag, 不擋流程
            if '標籤' not in df.columns: df['標籤'] = ''
            df.at[idx, '標籤'] = ' '.join(tags_final)
        if detail_cleans[j]:
            df.at[idx, '說明'] = detail_cleans[j]
        # 標記 1.jpg 該刪 (V64 兼容欄位, V65 後 image_optimizer 優先讀 _reject_indices)
        df.at[idx, '_reject_first_img'] = bool(reject_firsts[j])
        # 寫入全圖 reject indices (Stage E 結果, list of int 序列化成逗號分隔)
        rj_idx = sorted(set(reject_indices_per_item[j]))
        # 整合 Stage C 的 reject_first (1.jpg→index 0) 進來
        if reject_firsts[j] and 0 not in rj_idx:
            rj_idx = sorted(set(rj_idx + [0]))
        df.at[idx, '_reject_indices'] = ','.join(str(x) for x in rj_idx)
        # 局部特寫商品: 跳過 image_opt (避免 AI 把局部腦補成完整商品)
        df.at[idx, '_skip_image_opt'] = bool(not is_full_products[j])
        n_optimized += 1

    n_reject_idx_total = sum(len((reject_indices_per_item[j] or []) + ([0] if reject_firsts[j] else [])) for j in range(n))
    if n_text_fail > 0:
        log_fn(f'[V65] ⚠ 文字處理失敗攔截: {n_text_fail} 件 (Stage A/C 失敗 → 歸入不合格, 不跑 image_opt)')
        log_detail(f'[V65] 文字處理失敗詳情 ({n_text_fail} 件):')
        for code in text_fail_codes:
            log_detail(f'    ✗ {code}')

    # ★ 詳細日誌: 每件商品 SEO 標題 + 說明 + 簡述 前後對比 (debug 質量)
    log_detail(f'\n[V65] === 處理前後對比 ({n} 件) ===')
    for j in range(n):
        idx = valid_idx[j]
        try:
            code = str(df.at[idx, '商品條碼'])[:20]
        except: code = '?'
        orig_title = (titles[j] or '')[:80]
        new_title = (seo_titles[j] or '(空)')[:80]
        orig_brief = (briefs[j] or '')[:80]
        new_sub = (subtitles[j] or '(空)')[:80]
        orig_detail = (orig_details[j] or '')[:120].replace('\n', ' ')
        new_detail = (detail_cleans[j] or '(空)')[:120].replace('\n', ' ')
        tags_list = all_tags[j] if all_tags[j] else []
        tags = ' '.join(tags_list)[:60] if tags_list else '(空)'
        main = primary[j] or '?'
        sub_kw = secondary[j] or '?'
        log_detail(f'\n  [{code}]')
        log_detail(f'    標題: {orig_title}')
        log_detail(f'      → {new_title}')
        log_detail(f'    簡述: {orig_brief}')
        log_detail(f'      → {new_sub}')
        log_detail(f'    說明前120: {orig_detail}')
        log_detail(f'      → {new_detail}')
        log_detail(f'    標籤: {tags} | 主: {main} | 副: {sub_kw}')

    # ★ 詳細日誌: Stage E reject 詳情 (哪些圖被判 reject)
    if enable_stage_e and n_rejected > 0:
        log_detail(f'\n[V65 Stage E] reject 圖列表 ({n_rejected} 張):')
        for j in range(n):
            try:
                code = str(df.at[valid_idx[j], '商品條碼'])[:20]
            except: code = '?'
            for img_i in sorted(reject_indices_per_item[j]):
                log_detail(f'    🚫 {code}/{img_i+1}.jpg' + (' (Stage C 判 1.jpg 該刪)' if img_i == 0 and reject_firsts[j] else ' (Stage E 掃判 reject)'))

    # ★ V65 結束: 清掉 _v65_* 暫存欄位 (避免污染最終輸出, 但保留到 ckpt save 結束)
    _v65_temp_cols = ['_v65_a_detail', '_v65_a_subtitle', '_v65_a_forbidden',
                      '_v65_b_pri', '_v65_b_sec',
                      '_v65_c_seo', '_v65_c_fv', '_v65_c_note', '_v65_c_rj', '_v65_c_ifp',
                      '_v65_d_tags',
                      '_v65_e_rj']
    for col in _v65_temp_cols:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)
    return {
        'total_input': n,
        'optimized': n_optimized,
        'text_fail': n_text_fail,
        'text_forbid': sum(1 for f in forbiddens_text if f and f!='否'),
        'visual_forbid': n_visual_forbid,
        'reject_first_img': n_reject_first,
        'reject_indices_total_imgs': n_rejected,  # Stage E 直接 reject 的圖數
        'tag_ok': n_tag_ok,
    }
