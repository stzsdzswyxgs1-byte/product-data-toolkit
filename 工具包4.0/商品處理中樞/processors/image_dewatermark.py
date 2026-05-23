# -*- coding: utf-8 -*-
"""AI 定位水印 + LaMa 救援 — 對 V65 Stage E 標記 reject 的圖嘗試救援
- AI (gpt-5.5 multimodal) 定位水印 bbox (能識別 emoji + 區分商品字 vs 水印字)
- 若水印面積 < 30% → LaMa inpaint 救援, 替換原圖, 從 _reject_indices 移除
- 若 > 30% → 整圖是廣告, 仍丟棄

成功救援的圖: 商品 100% 保真 + 水印乾淨
"""
import os, json, time, re, base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

API = 'https://api.example.com/v1/chat/completions'
KEY = '<TEST_API_KEY>'
MODEL = 'gpt-5.5'
WORKERS_AI = 96   # AI 並發 (gpt-5.5 multimodal): 壓測甜蜜點
MAX_AREA_RATIO = 0.60  # 水印面積 > 60% 才不救 (實測 padding=15 + 60% 能救多邊框/促銷貼類)
HARD_CAP_AREA = 0.75   # 絕對上限: > 75% LaMa 會破壞商品本體 (Disney 整張海報類)
# ★ 4.0.39: padding 從 8 → 16. 反覆 rollback 樣本 (吉石到/integrity·innovate logo)
#   證實 8px 對「方框中文 logo + 上下水平線 + 英文 italic」這類複雜結構不夠, 邊緣字輪廓殘留.
#   16px 多吞一點背景 (商品本體不會跨到邊緣 0.85 外, 安全).
MASK_PADDING = 16
SKIP_VERIFY_SMALL = 0  # ★ 4.0.53: 永遠跑 verify (admin 要看 LaMa 修復後圖確認質量, 不省 AI call)

# ★ 4.0.39 LOCATE_PROMPT 改進 — 修「只抓底部漏左上」bias:
#   實際 case (5 張同賣家 rollback): 底部+左上 同 logo 同時出現, AI 只回底部一個 bbox,
#   verify 看到左上殘留 → rollback. 改進: 強制 9 區域掃 + 多水印標準回 2-4 個 bbox.
LOCATE_PROMPT = """找出圖中「Yahoo TW 拍賣不接受的水印/廣告/裝飾」bbox 座標 (0-1).

★★★ 4.0.75 入口判定 — 整張該丟類型 (K0 救不了, 直接 return 空 watermarks): ★★★
如果整張圖是以下類型, **直接 return {"watermarks":[]} 不要框任何 bbox**
(讓 K0 走 no_watermark 路徑, 維持 Stage E reject; 強行 LaMa 救只會誤救起髒圖):

  ❌ **整張 app/網站截圖** — 有以下任 2 項組合就算:
     狀態欄 (時間 + 信號 + 電量) / 搜尋欄 + 放大鏡 / 商品標題 + 價格 (¥XXX) /
     按鈕 (加入購物車/立即購買/申請售後/賣了換錢/關注/订阅) / 評論數 + 瀏覽數 /
     訂單編號 / 「商品失效」「為您找到同款」 / app 名稱 (淘宝/閒魚/百度百科/腕表之家/抖音)
  ❌ **賣家拼貼版型** — 2-9 格商品圖拼成一張 + 帶白色相框 + 標題手寫字 (Day 1/Day 2/hooray/foodie) + 邊角愛心 sticker
  ❌ **賣家設計海報** — 整張是品牌/系列 reference 圖, 有大段英文/中文 marketing 文案 + 商品在中央 (例: TIGER "A GROUP OF PEOPLE WHO PROTECT UNIVERSE")
  ❌ **代言人拼貼** — 商品 + 名人/明星/模特照 + 品牌字 (商品本體外有人臉 + 品牌名)
  ❌ **影片 storyboard** — 4-9 格不同 frame, 含「人物臉/手/工作場景」+ 字幕條, 像影片截圖拼貼
  ❌ **規格詳情頁** — 整張是表格 (品牌/系列/型號/規格 條列) + 大段中文段落, 沒商品實拍

這幾類 K0 LaMa 救不了 (整張該丟而不是清水印), 強行救會誤救起髒圖.
返回空 watermarks → 維持 Stage E reject 是正確結果.

★★★ 普通商品圖 (有商品本體實拍 + 邊角水印) 的區域鐵律: ★★★
  1. 圖片**四邊** (頂 y<0.15 / 底 y>0.85 / 左 x<0.15 / 右 x>0.85)
  2. 圖片**四角** (角落 0.2×0.2 範圍)
  3. **整圈邊框色條** (拆 4 條 bbox)

★ 中心區域 (x: 0.15-0.85, y: 0.15-0.85) 是**商品本體**, 絕對不要框, 寫的字都是商品自帶的!
★ 寧可漏抓中心水印, 也不要把商品框成水印!

★★★ 重要: 同一張圖**經常有 2-4 處水印** (不是只一處!): ★★★
  - 賣家很愛把同一個 logo 蓋在**底部 + 左上 + 右上** 這 2-3 處. 漏抓任何一處都會 rollback!
  - 必須**全圖 9 區域掃描** 再寫 bbox, 不要看到底部那個就 return:
      上左(x<0.15,y<0.15) 上中(0.15<x<0.85,y<0.15) 上右(x>0.85,y<0.15)
      左中(x<0.15,中)                                右中(x>0.85,中)
      下左(x<0.15,y>0.85) 下中(0.15<x<0.85,y>0.85) 下右(x>0.85,y>0.85)
  - 找完底部還要回頭掃**頂部三區 (上左/上中/上右)** 跟 **右上角** — 這兩處最常被漏!
  - 同一個 logo 出現多次 → 每次都列一個獨立 bbox. 不要合併.

★★★ bbox 必須包含水印**外圍** (LaMa 才能蓋乾淨, 不殘留輪廓): ★★★
  - 半透明光暈 / 陰影 / 邊緣模糊區 / 抗鋸齒邊
  - 文字外圍的描邊 / 發光效果
  - logo 邊緣的色階過渡帶
  - **複合 logo (中文方框 + 上下水平線 + 英文小字) 必須整體框進去, 不要只框中文部分漏水平線!**
  寧可框大 5-10%, 不要剛好貼字邊
  ⚠️ 但不要超過原水印視覺區域的 50%, 不要把整個邊吞掉

✅ 應該移除 (限上述 3 區域內的):
- 邊框色條 (整圈或單邊): 拆 4 條 bbox [0,0,1,0.05] / [0,0.95,1,1] / [0,0,0.05,1] / [0.95,0,1,1]
- 角落貼紙 (現貨速發/順豐包郵/寶藏單品推薦/嚴選/24h極速 — 在四角範圍內)
- 頂底 banner (金典嚴選/閒魚官方認證/坚持原装 等橫幅)
- 右下水印 (閒魚號/手機型號/抖音號)
- 文字促銷貼 (任何顏色, 在邊緣/角落範圍內)
- ★ **賣家自加複合 logo** (中文方框「吉石到 / XX 收藏 / XX 古玩」+ 上下兩條水平線 + 英文 italic 小字 — 通常在底部+左上同時出現. 整組必須框進去, 包水平線兩端!)

❌ 絕對不要動 (即使在邊緣也保留):
- 商品本體 (錢幣/瓷器/鏡頭/包包/首飾/盒子)
- 商品上的字 (PMG/GBCA 評級盒鑑定資訊「明-天啟通寶 美88 25.6mm」)
- 評級盒外框 (PMG/GBCA 透明塑膠盒)
- 商品紋飾/印章/朝代名 (永曆通寶/中華民國 等錢幣本體字)
- QR code 馬賽克 (賣家保護)
- emoji/貼紙 蓋住 QR code (賣家保護)
- 手指拿著商品 (家拍正常)
- ★ **背景書封/包裝紙/桌布上的字** — 如果在中心區域 (商品旁的家居背景), 那是拍照場景, 不是水印
- ★ 4.0.61: **拍攝場景的家居裝飾元素** — 桌邊/牆邊/角落的:
  - 花瓶 / 盆栽 / 花枝 (例如太湖石擺件桌邊掛的梅花樹枝)
  - 鳥籠 / 燭台 / 工藝品擺設
  - 裝飾畫 / 裝裱書畫 / 牆上掛飾
  - 仿古傢俱 (條案/茶几/花架)
  這些是「拍照場景的氛圍 props」, 不是賣家加的水印, 拍攝者刻意搭配出商品文人氛圍, 抹掉會破壞圖風格

【bbox 格式】 [x1, y1, x2, y2] 歸一化 0-1, 左上原點
【嚴格自檢】★ 寫完所有 bbox 後再問自己一遍:
  - 我有沒有漏掉**頂部** (y<0.15) 那塊水印?
  - 我有沒有漏掉**左上角** (x<0.2 且 y<0.2) 那個小 logo?
  - 同一個賣家 logo 是不是只列了 1 個 bbox 但圖上看到 2-3 個?
★ 每個 bbox 中心點 (x,y) 是否在 0.15-0.85 之間?
  - 是 → 中心區域 → **不要寫** (商品本體)
  - 否 → 邊緣/角落 → 可寫

★ 4.0.53: 每個 bbox 必須帶 confidence (0.0-1.0, 你對「這真的是水印」的信心):
  - 1.0 = 100% 確定是賣家加的浮水印 (例如閒魚 logo / 寶藏單品推薦 / 印章+時間戳)
  - 0.7-0.9 = 看起來像水印但不 100% 確定 (例如可能是商品本體上自帶的設計)
  - 0.5 = 邊界 case (拿不準是水印還是商品本體, admin 該抽查)
  - < 0.5 → 別寫進 watermarks (太不確定不該動)
邊界 case 寫出來 + 低 confidence 比漏抓更好 — admin 會抽查低分樣本.

純 JSON: {"watermarks":[{"type":"底部品牌logo","bbox":[0.3,0.88,0.7,0.99],"confidence":0.95},{"type":"左上同logo","bbox":[0.05,0.18,0.22,0.28],"confidence":0.85}]}
無水印 → {"watermarks":[]}"""

# ★ 4.0.39 VERIFY_PROMPT 改進 — 修「verify 看 LaMa 沒碰的區域當殘留」bias:
#   實際 case: LaMa 只蓋底部 logo, verify 卻看到「右上書封字」「中上包裝紙字」喊殘留 → rollback.
#   改進: 接受 LaMa 處理區域 hint, verify 只看那些 bbox 內有沒有殘留 (其他位置不算).
VERIFY_PROMPT = """這張圖剛經 LaMa 去水印處理. 我會列出 LaMa **實際蓋過的 bbox 區域**, 你判斷殘留:

★★★ 規則: 主要看「LaMa 處理過的 bbox 區域內」有沒有殘留. ★★★
  - bbox 範圍**外**的字/紋理 → 大部分**不算殘留** (那是商品本體 / 場景背景, LaMa 沒碰過)
  - 例如: 商品旁的書封字、包裝紙字、桌布字、商品本體紋飾 — 都不算
  - 即使你覺得整張圖看起來「還有字」, 只要不是下面例外就 clean=true

★★★ 4.0.60 + 4.0.62 — bbox 外特殊例外 (即使 LaMa 沒處理過也算殘留, clean=false): ★★★
  ❌ **「.com」結尾的網址** (taobao.com / xianyu.taobao.com / 1688.com / pinduoduo.com / mogu.com 等任何 URL)
  ❌ **對立平台名直接寫在圖上**: 淘寶 / taobao / 蝦皮 / shopee / 拼多多 / pinduoduo / 阿里巴巴 / 1688 / mogujie / 小紅書 / xiaohongshu / 京東 / jd / 天貓 / tmall
  ❌ **賣家自加紅色獨立浮印章** (浮在背景空白處, 不在商品本體紋飾上, 跟商品分離的方塊/圓章)
  ❌ 4.0.62: **賣家防盜/防偽中文水印** (整張任何位置): 「實物拍攝/盜圖必究/盜版必究/翻版必究/抄襲必究/版權所有/未經授權」 等防盜聲明
  ❌ 4.0.62: **賣家加的規格文字** (任何位置, 通常黑色或紅色, 浮在背景空白處): 「高XX寬XX厚XX」「直徑XX」「重XX斤/克/g/kg」「尺寸約 XX cm/CM」「小口尺寸約」「大口尺寸約」「總重量」 — 跟商品本體刻字不同 (本體刻字是浮雕/陽刻在金屬/木頭上, 規格水印是電腦字浮在背景)
  ❌ 4.0.62: **賣家中央描述/讚美文字** (跟 LOCATE_PROMPT 第 7 類對應): 「面料非常有光澤」「肩膀有點染色了」「精美的玉雕膠感」「老料新工」「天然好料」「極品」 等浮在商品上方/周圍的描述讚美句
  ❌ 4.0.62: **app/網站 UI 殘留** (整張看起來像 app 截圖 / 網站截圖): 狀態欄 (時間 + 信號 + 電量) / 搜尋欄 + 放大鏡 / 「订阅/收藏/评论」按鈕 / 「¥XXX 包邮」標籤 / 「Add to cart」/「立刻购买」按鈕 / 評論數/瀏覽數 / app 名稱 (豆包/百度百科/抖音/閒魚交易須知)
    特徵: 不是商品實拍, 整張背景是純色 UI 而非自然/桌面環境, 看起來像手機 screenshot
  ❌ 4.0.63: **促銷標/熱賣貼紙** (浮在商品上或周邊, 任何位置): 「HOT!」「NEW!」「SALE!」「特價」「限時」「秒殺」「爆款」「現貨」「熱賣」 等彩色橢圓形/圓形貼紙. 通常紅底白字 + 彩色 emoji 眼睛/星星. 不是商品本體圖案而是賣家後製貼上去的
  ❌ 4.0.63: **相機取景畫面 UI 殘留** (拿手機對準商品「拍攝中」的畫面截圖, 不是處理過的成品圖): 對焦框/網格線/變焦倍率「0.6 1x 1.8x 2x 3x 6x」/底部圓形拍攝按鈕/暫停按鈕 ⏸️/相機切換 icon
    特徵: 商品在中央但有相機 UI 元素疊加, 畫面是「正在拍」的狀態而非「拍好的圖」
  ❌ 4.0.63: **賣家標價標籤** (浮在商品旁的價格貼紙): 「¥XXX」「$XXX」「￥XXX,XXX」+ 小型綠色/黃色/紅色標貼紙 / 價格條碼 / 賣家手寫價格
  ❌ 4.0.65: **賣家裝飾 sticker** (整張任何位置, 浮在背景, 跟商品本體分離): 粉紅/紅色/彩色**小愛心** ♥ / 星星 ★ / 花朵 🌸 / 蝴蝶結 🎀 / 雪花 ❄ / 表情 emoji 等裝飾性 pixel-art 圖示 (通常 8-bit pixelated 風格, 邊緣鋸齒, 純色填充).
    特徵: 不是商品本體上印的, 是賣家後製加在背景空白處的裝飾, 通常出現在四角 + 頂底邊緣, **多個成對排列** (例如左上+右上+左下+右下 4 個成組, 或頂部 + 底部各 1 排).
    ★ 即使 LaMa 處理過後輪廓「半透明/淡化」但仍能看出愛心/星星形狀, 一律算殘留 (LaMa 對小 pixel-art sticker inpaint 弱, 容易留 ghost)
  ❌ 4.0.65: **賣家拼貼版型** (整張是 2-4 格商品拼貼 + 賣家加裝飾框/標題/愛心 sticker): 帶白色相框邊框 + 標題 (例如「Day 1/Day 2/Day 3」「hooray!」「to be a foodie」手寫字)/或量尺寸對比拼貼 (左圖商品+右圖+量尺) + 邊角愛心裝飾.
    特徵: 不是單張商品實拍, 是賣家拼出來的「精選賣家秀」風格, 帶大量裝飾 + 手寫字
  ❌ 4.0.69: **賣家拍攝影片/紀錄片截圖拼貼** (4-9 格不同 frame 拼成一張): 場景常見「陶藝師傅製作過程/工作室一景/職人手持商品」+ 影片字幕 (日文/中文小字, 例如「我叫XX」「晾干」「制作中」白色字幕在格子底部) + 愛心或邊框裝飾.
    特徵: 多格中含「人物臉/手/工作場景」, 不是純商品實拍, 每格底部有字幕條, 整體像影片 storyboard

  ★ 這 6 類即使在中心商品區域、即使 LaMa 沒框過, 看到一定 clean=false + 詳細描述位置.
  ★ 理由: K0_locate 不框中心區域 (避免破壞商品), 但 Yahoo TW 必抓的違規必須由 verify 把關. K0 沒能力清這些 (中央/整張) 但 verify 至少 catch 防止髒圖混入.

❌ bbox 內不乾淨 (clean=false) — bbox 範圍內仍可看出原水印的:
- 文字輪廓 (即使半透明、淡化、模糊, 還能看出是字 → 不放行)
- logo 形狀 (賣場標/平台 logo 的剪影、輪廓)
- 邊框痕跡 (彩色色條, 即使斷裂)
- 殘留可辨認的促銷貼紙形狀 (橢圓/方框輪廓)
- 「寶藏單品推薦/金典/閒魚/integrity·innovate」等字樣即使非常淡也算殘留

✅ bbox 內乾淨 (clean=true) + bbox 外沒上述例外:
- 純色塊/紋理不平整 (看不出是文字或 logo, 只是 LaMa 修復後的色差)
- 邊緣模糊 (沒有可辨認形狀)
- 完全清乾淨

⚠️ bbox **外**的這些東西**不算**殘留 (跟 4.0.60 例外不衝突):
- 商品本身的字 (PMG「中華民國」「壹圓」/ 評級盒鑑定資料 / 朝代名)
- 商品紋飾/印章 (錢幣朝代字/玉牌銘文)
- 相機型號浮水印 (HUAWEI/vivo/xiaomi 自動加的)
- QR code 馬賽克
- 場景背景的書封字、包裝紙字、桌布字 (LaMa 沒碰過, 不算)

★ 4.0.53: 必須帶 confidence (0.0-1.0, 你對「LaMa 確實清乾淨了」的信心):
  - 1.0 = 100% 乾淨 / 100% 確定還髒
  - 0.7-0.9 = 大致清乾淨 / 大致還有殘 (但有點不確定)
  - 0.5 = 邊界 case (例如殘留模糊, admin 該抽查)
  - < 0.5 = AI 自己也拿不準

純 JSON: {"clean": true/false, "remaining": "若 false 寫殘留是什麼具體文字/logo 形狀, 在哪個 bbox 內或在中心區域 (4.0.60 例外觸發)", "confidence": 0.95}"""


import threading


def _ascii_safe_path(orig_path, log_fn=None):
    """Windows + 中文資料夾 + torch.jit.load 的坑: fopen() 用 ANSI 代碼頁解碼字節, 中文字會 errno 42.
    對策:
      1. 已是 ASCII → 直接用
      2. Windows 8.3 短路徑可用 → 用短路徑
      3. 都不行 → 複製到 %TEMP%/lama_cache/ (一次性 196MB)
    """
    # 1. 已是 ASCII
    try:
        orig_path.encode('ascii')
        return orig_path
    except UnicodeEncodeError:
        pass

    # 2. Windows 短路徑 (8.3)
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
            GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            GetShortPathNameW.restype = wintypes.DWORD
            buf = ctypes.create_unicode_buffer(260)
            if GetShortPathNameW(orig_path, buf, 260) > 0:
                short = buf.value
                try:
                    short.encode('ascii')
                    if log_fn:
                        log_fn(f'  [LaMa weight] 用 8.3 短路徑: {short}')
                    return short
                except UnicodeEncodeError:
                    pass  # 8.3 被系統關了
        except Exception:
            pass

    # 3. 複製到 ASCII 目錄
    import tempfile, shutil
    cache_dir = os.path.join(tempfile.gettempdir(), 'lama_cache')
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, 'big-lama.pt')
    orig_size = os.path.getsize(orig_path)
    if not (os.path.isfile(cached) and os.path.getsize(cached) == orig_size):
        if log_fn:
            log_fn(f'  [LaMa weight] 中文路徑 + 8.3 關 → 複製到 {cache_dir} (一次性 {orig_size/1024/1024:.0f}MB)...')
        shutil.copy2(orig_path, cached)
        if log_fn:
            log_fn(f'  [LaMa weight] 複製完成')
    return cached


LAMA_POOL_SIZE = 8  # 預設 (GPU 8GB+); _detect_lama_config 會根據實際硬體調整
_lama_pool = []  # [(SimpleLama, Lock), ...]
_lama_pool_init_lock = threading.Lock()
_lama_device_used = None  # 初始化後記錄實際用的 device, 給 log 看


def _detect_lama_config(force_device='auto', force_pool_size=None):
    """偵測硬體, 返回 (device, pool_size).

    force_device: 'auto' | 'cuda' | 'cpu'
    force_pool_size: int (覆蓋自動計算) 或 None

    自動規則 (auto, 一個 LaMa ~525MB VRAM):
      24GB+  (4090 / RTX 6000 / A100)        → pool=24
      16GB+  (4080 / 4070 Ti Super 16GB)     → pool=16
      12GB+  (4070 Super / 3060 12GB / 3080 Ti) → pool=12
      8GB+   (3070 / 4060 Ti 8GB)            → pool=8
      6GB+   (1660 Super / 2060 / 3060 6GB)  → pool=4
      4GB+   (3050 / 1650)                   → pool=2
      2GB+   (750 Ti)                        → pool=1
      < 2GB  → pool=1 + 警告
      無 GPU / CUDA 不可用 → CPU, pool=2 (CPU 並發效益低)
    """
    device = 'cpu'
    pool_size = 2
    info_lines = []
    try:
        import torch
        cuda_ok = torch.cuda.is_available()
    except Exception as e:
        cuda_ok = False
        info_lines.append(f'  ⚠ torch 未裝或異常: {str(e)[:60]} → 走 CPU')

    if force_device == 'cpu':
        device = 'cpu'
        pool_size = force_pool_size or 2
        info_lines.append(f'  [LaMa device] 強制 CPU (pool={pool_size})')
    elif force_device == 'cuda':
        if not cuda_ok:
            device = 'cpu'
            pool_size = force_pool_size or 2
            info_lines.append(f'  [LaMa device] 強制 CUDA 但偵測不到 → 退回 CPU (pool={pool_size})')
        else:
            device = 'cuda'
            pool_size = force_pool_size or _auto_pool_size_for_gpu(info_lines)
    else:  # auto
        if cuda_ok:
            # ★ Blackwell (sm 10.0+) 預防性檢查: 看 torch 的 arch_list 有沒有支援這代
            #   - cu124 build: arch_list = [..., sm_90+PTX] → 對 sm_120 PTX JIT 跑 LaMa 會炸 (no kernel image)
            #   - cu128 build: arch_list = [..., sm_100, sm_120, ...] → 原生支援, 直接走 GPU
            #   原則: torch 真支援 (arch_list 含這個 sm) 才走 GPU; 否則 CPU 安全
            try:
                import torch
                cap = torch.cuda.get_device_capability(0)  # (major, minor)
                if cap[0] >= 10:
                    name = torch.cuda.get_device_name(0)
                    sm_str = f'sm_{cap[0]}{cap[1]}'  # 例: 'sm_120'
                    sm_compute = f'compute_{cap[0]}{cap[1]}'
                    archs = torch.cuda.get_arch_list()
                    # arch_list 元素例: 'sm_50' / 'sm_90' / 'compute_90' / 'sm_120'
                    has_native = any(sm_str in a or sm_compute in a for a in archs)
                    if has_native:
                        info_lines.append(
                            f'  [LaMa device] {name} (sm_{cap[0]}.{cap[1]}, Blackwell) — torch {torch.__version__} 原生支援 ✓'
                        )
                        # fall through 走 cuda
                    else:
                        device = 'cpu'
                        pool_size = force_pool_size or 2
                        info_lines.append(
                            f'  [LaMa device] 偵測到 {name} (sm_{cap[0]}.{cap[1]}, Blackwell)'
                        )
                        info_lines.append(
                            f'    torch {torch.__version__} arch_list={archs} 不含 {sm_str}'
                        )
                        info_lines.append(
                            f'    → 強制 CPU (pool={pool_size}). 升 torch 2.9+cu128 才能 GPU 加速 (刪 .venv 重跑 setup.bat)'
                        )
                        return device, pool_size, info_lines
            except Exception:
                pass  # 拿不到 capability 就走原本 cuda 路徑, 失敗時下面 _init_lama_pool 會 fallback
            device = 'cuda'
            pool_size = force_pool_size or _auto_pool_size_for_gpu(info_lines)
        else:
            device = 'cpu'
            pool_size = force_pool_size or 2
            info_lines.append(f'  [LaMa device] 自動偵測: 無 CUDA → CPU (pool=2, 速度約慢 10-20x)')

    return device, pool_size, info_lines


def _auto_pool_size_for_gpu(info_lines):
    """根據 GPU VRAM 算 pool size"""
    try:
        import torch
        props = torch.cuda.get_device_properties(0)
        vram_gb = props.total_memory / (1024 ** 3)
        name = props.name
        # 一個 LaMa instance 約 ~525MB VRAM. 算法: pool ≈ vram * 0.55 / 0.525 (留 45% 給 driver/cuBLAS)
        # 0.5 容差避免邊界 (6GB 卡實際 5.99 / 12GB 卡實際 11.99)
        # ★ 4.0.59: 升 pool size 榨更多 VRAM (4070 SUPER 12GB 之前 pool=12 只用 6.3GB,50% 浪費)
        # 邏輯: idle weights ~525MB/instance + inference peak ~300MB activation buffer
        #       LaMa lock-per-instance, 同時 active <= K0 並發 (受 adaptive cap)
        # 12GB pool=16: idle 8.4GB + 並發 active peak 4-5GB = 邊界 12GB (有 OOM auto-reduce 兜底)
        if vram_gb >= 22:    # 4090 24GB / RTX 6000 / A100 — idle 14.7GB + peak ≈ 18-19GB
            ps = 28
        elif vram_gb >= 15:  # 4080 16GB / 4070 Ti Super 16GB — idle 10.5GB + peak ≈ 13-14GB
            ps = 20
        elif vram_gb >= 11:  # 4070 SUPER 12GB / 3060 12GB / 3080 Ti 12GB — idle 8.4GB + peak ≈ 10.5-11GB
            ps = 16
        elif vram_gb >= 7.5: # 4070 12GB (有些是 11.6) / 3070 8GB / 4060 Ti 8GB — idle 5.25GB + peak ≈ 6.5-7GB
            ps = 10
        elif vram_gb >= 5.5: # 4060 / 3060 6GB / 1660 Super 6GB / 2060 6GB — idle 3.15GB + peak ≈ 4-4.5GB
            ps = 6
        elif vram_gb >= 3.5: # 3050 4GB / 1650 4GB — idle 1.6GB + peak ≈ 2.5-3GB
            ps = 3
        elif vram_gb >= 1.5: # 750 Ti 2GB / 1050 2GB
            ps = 1
        else:
            ps = 1
            info_lines.append(f'  ⚠ GPU VRAM {vram_gb:.1f}GB 太小, 強制 pool=1')
        est_used = ps * 0.525
        info_lines.append(f'  [LaMa device] GPU: {name} ({vram_gb:.1f}GB) → pool={ps} (預估佔用 {est_used:.1f}GB)')
        return ps
    except Exception as e:
        info_lines.append(f'  ⚠ 讀 GPU 屬性失敗: {str(e)[:50]} → pool=2')
        return 2


def _init_lama_pool(force_device='auto', force_pool_size=None, log_fn=None):
    """惰性初始化 N 個 LaMa instance (thread-safe). 自動偵測 device.

    log_fn: 給 init 過程的訊息一個地方寫
    """
    global _lama_pool, LAMA_POOL_SIZE, _lama_device_used
    with _lama_pool_init_lock:
        if _lama_pool:
            return

        device, pool_size, info = _detect_lama_config(force_device, force_pool_size)
        LAMA_POOL_SIZE = pool_size
        _lama_device_used = device

        if log_fn:
            for line in info:
                log_fn(line)
            log_fn(f'  [LaMa init] device={device}, pool_size={pool_size} (建立中...)')

        # 打包權重: 中樞根目錄如有 big-lama.pt, 直接餵 LAMA_MODEL 跳過 GitHub 下載
        bundled = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'big-lama.pt'
        )
        if os.path.isfile(bundled) and not os.environ.get('LAMA_MODEL'):
            # 中文路徑 → torch.jit.load fopen() errno 42, 必須轉 ASCII-safe 路徑
            safe = _ascii_safe_path(bundled, log_fn=log_fn)
            os.environ['LAMA_MODEL'] = safe
            if log_fn:
                size_mb = os.path.getsize(bundled) / 1024 / 1024
                log_fn(f'  [LaMa weight] 使用打包檔 big-lama.pt ({size_mb:.0f} MB)')
        elif not os.environ.get('LAMA_MODEL') and not os.environ.get('LAMA_MODEL_URL'):
            # 沒打包檔, 也沒人指定 URL → 走 kkgithub 鏡像 (對中國用戶 5-10 倍速)
            os.environ['LAMA_MODEL_URL'] = (
                'https://kkgithub.com/enesmsahin/simple-lama-inpainting/'
                'releases/download/v0.1.0/big-lama.pt'
            )
            if log_fn:
                log_fn('  [LaMa weight] 找不到打包檔, 走 kkgithub 鏡像下載')

        from simple_lama_inpainting import SimpleLama

        def _build_one(dev):
            """建一個 LaMa instance, 處理舊版無 device 參數兼容"""
            try:
                return SimpleLama(device=dev)
            except TypeError:
                return SimpleLama()

        # ★ 第一個 instance 試裝 — 若 cuda 失敗 (例如未知怪卡 / cu124 對 sm_120 PTX 不夠),
        #   清空 pool, 改走 CPU 重試. 預防性 sm_120 偵測沒涵蓋到的長尾情況.
        try:
            first = _build_one(device)
            _lama_pool.append((first, threading.Lock()))
        except Exception as e:
            err = str(e)[:120]
            if device == 'cuda':
                if log_fn:
                    log_fn(f'  ⚠ LaMa cuda 載入失敗: {err}')
                    log_fn(f'  → 自動退回 CPU 重試 (LaMa 仍可用, 速度慢 10-20x)')
                device = 'cpu'
                _lama_device_used = 'cpu'
                # CPU 模式 pool size 不該照 GPU 算法 (~525MB/instance)
                pool_size = max(2, min(LAMA_POOL_SIZE, 4))
                LAMA_POOL_SIZE = pool_size
                first = _build_one('cpu')
                _lama_pool.append((first, threading.Lock()))
            else:
                # 連 CPU 都炸 — 這就真不能跑了
                raise

        # 剩餘 instance: 已知第一個 OK, 但邊緣 GPU 載到第 N 個可能 OOM (pool 估太激進)
        # ★ 4.0.59: catch CUDA OOM, 停在當前 pool 大小 — 降級比退 CPU 好太多
        for _ in range(pool_size - 1):
            try:
                _lama_pool.append((_build_one(device), threading.Lock()))
            except Exception as e:
                err = str(e)[:120]
                # CUDA OOM (torch.cuda.OutOfMemoryError) 或顯存相關 RuntimeError
                if device == 'cuda' and ('OutOfMemoryError' in type(e).__name__
                                          or 'CUDA out of memory' in err
                                          or 'CUDA error: out of memory' in err):
                    actual = len(_lama_pool)
                    if log_fn:
                        log_fn(f'  ⚠ LaMa pool 載到第 {actual+1} 個 OOM (idle ~{actual*0.525:.1f}GB), '
                               f'停在 pool={actual} (auto 偵測太激進, 邊界調回)')
                    pool_size = actual
                    LAMA_POOL_SIZE = actual
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    break
                # 非 OOM 的其他 exception → propagate (照舊行為)
                raise

def _get_lama():
    """兼容舊 API: 回第一個 instance (不推薦在多 thread 用)"""
    if not _lama_pool:
        _init_lama_pool()
    return _lama_pool[0][0]

def _acquire_lama():
    """取一個空閒 LaMa + lock (thread-safe). 返回 (lama, lock); 用完必須 lock.release()"""
    if not _lama_pool:
        _init_lama_pool()
    # 嘗試找一個空閒的
    for lama, lock in _lama_pool:
        if lock.acquire(blocking=False):
            return lama, lock
    # 全部佔用, 等第一個
    lama, lock = _lama_pool[0]
    lock.acquire()
    return lama, lock


def _ai_call(img_b64, prompt, xtrace_header: str = ''):
    """通用 AI 呼叫, 回 parsed JSON 或 None. 失敗時通報 APIMonitor (連續 N 次 → PauseException)"""
    try:
        from checkpoint_manager import get_monitor
        _monitor = get_monitor()
    except Exception:
        _monitor = None
    body = {
        'model': MODEL,
        'messages': [{
            'role':'user',
            'content':[
                {'type':'text','text':prompt},
                {'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{img_b64}'}}
            ]
        }],
        'temperature': 0.0,
    }
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
            # ★ 不論 status_code, 一律試解析 body 拿 error 欄位 (中介 503 也有 error.type/code)
            try:
                jr = r.json()
                err = jr.get('error') or {}
                if err:
                    last_err_code = err.get('code'); last_err_type = err.get('type'); last_err_msg = err.get('message')
            except Exception:
                jr = None
            if r.status_code == 200 and jr and not err:
                content = jr['choices'][0]['message']['content']
                content = re.sub(r"```json\s*","",content); content = re.sub(r"```\s*$","",content.strip())
                m = re.search(r'\{[\s\S]*\}', content)
                if m:
                    if _monitor: _monitor.record_success()
                    return json.loads(m.group())
            if attempt < 1: time.sleep(1)
        except Exception as e:
            last_exc = e
            if attempt < 1: time.sleep(1)
    if _monitor:
        # ★ 4.0.31: record_fail_safe 自帶 is_network_blip 檢查, VPN 抖不誤觸發 cascade
        try:
            from processors.utils import record_fail_safe
            record_fail_safe(_monitor, status_code=last_status, error_code=last_err_code,
                             error_type=last_err_type, error_message=last_err_msg,
                             exception=last_exc)
        except ImportError:
            _monitor.record_fail(status_code=last_status, error_code=last_err_code,
                                 error_type=last_err_type, error_message=last_err_msg,
                                 exception=last_exc)
    return None


def _ai_locate(img_path, bc: str = '', img_idx: int = 0):
    """AI 定位水印 bbox, 失敗回 None
    ★ 4.0.53: 每個 bbox 帶 confidence (0-1), missing 則 default 1.0 (back-compat).
    """
    try:
        # ★ 壓縮到 1024px 上傳 (AI 看 bbox 不需高解析度, 上傳快 5x)
        from PIL import Image, ImageOps
        from io import BytesIO
        img = Image.open(img_path)
        # ★ 4.0.71: 應用 EXIF orientation (iPhone/Android 原圖預設 EXIF=6 右旋 90度,
        #   PIL 預設不 apply, 像素是「躺著的」, AI 看了會把「右下水印」當「左下」框錯位置.
        #   ImageOps.exif_transpose 對沒 EXIF 的圖 no-op, 安全.
        img = ImageOps.exif_transpose(img).convert('RGB')
        if max(img.size) > 1024:
            ratio = 1024 / max(img.size)
            img = img.resize((int(img.size[0]*ratio), int(img.size[1]*ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, 'JPEG', quality=85, optimize=True)
        img_b64 = base64.b64encode(buf.getvalue()).decode()
    except:
        return None
    try:
        from processors.feedback_collector import make_xtrace_single
        xtrace = make_xtrace_single('stage_k0_locate', bc or 'unknown', img_idx)
    except Exception:
        xtrace = ''
    parsed = _ai_call(img_b64, LOCATE_PROMPT, xtrace_header=xtrace)
    if not parsed:
        return None
    wms = parsed.get('watermarks', []) or []
    # 為每個 bbox 確保有 confidence (default 1.0 if missing)
    for w in wms:
        if 'confidence' not in w:
            w['confidence'] = 1.0
        else:
            try: w['confidence'] = float(w['confidence'])
            except Exception: w['confidence'] = 1.0
            w['confidence'] = max(0.0, min(1.0, w['confidence']))
    return wms


def _ai_verify_clean(img_bytes, bc: str = '', img_idx: int = 0, mask_bboxes: list = None):
    """LaMa 跑完後驗證: 回 (clean: bool, remaining: str, confidence: float [0-1])
       失敗回 (False, '', 0.0)

    ★ 4.0.22: return tuple 帶 remaining 文字 — admin Claude review 要看 verify AI 為什麼判髒
    ★ 4.0.39: 接受 mask_bboxes (LaMa 實際處理過的 bbox list), 構造 prompt 告訴 verify
       「只看這些區域內」 — 修 verify 把商品背景字當殘留誤判.
    ★ 4.0.53: return 多帶 confidence, 給 admin priority_review 用
       (低 confidence = AI 邊界 case, admin 該抽查)
    """
    try:
        img_b64 = base64.b64encode(img_bytes).decode()
    except:
        return False, '', 0.0
    try:
        from processors.feedback_collector import make_xtrace_single
        xtrace = make_xtrace_single('stage_k0_verify', bc or 'unknown', img_idx)
    except Exception:
        xtrace = ''
    # ★ 4.0.39: 把 LaMa 實際蓋過的 bbox 列在 prompt 開頭, 讓 verify 知道「只看這幾塊」
    prompt = VERIFY_PROMPT
    if mask_bboxes:
        bbox_lines = []
        for i, bb in enumerate(mask_bboxes, 1):
            if bb and len(bb) == 4:
                bbox_lines.append(f"  bbox{i}: [x1={bb[0]:.2f}, y1={bb[1]:.2f}, x2={bb[2]:.2f}, y2={bb[3]:.2f}]")
        if bbox_lines:
            prompt = ("LaMa 蓋過的 bbox 區域 (歸一化 0-1, 左上原點):\n"
                      + "\n".join(bbox_lines)
                      + "\n\n"
                      + VERIFY_PROMPT)
    parsed = _ai_call(img_b64, prompt, xtrace_header=xtrace)
    if parsed is None:
        return False, '', 0.0  # 驗證失敗保守當髒, confidence=0
    clean = bool(parsed.get('clean', False))
    remaining = str(parsed.get('remaining', ''))
    # ★ 4.0.53: parse confidence (default 1.0 backward compat)
    try:
        conf = float(parsed.get('confidence', 1.0))
        conf = max(0.0, min(1.0, conf))
    except Exception:
        conf = 1.0
    return clean, remaining, conf


def _try_dewatermark_one(img_path, bc: str = '', img_idx: int = 0, _meta_out: dict = None):
    """嘗試對單張圖去水印, 成功返回 (saved_path, area_ratio), 失敗返回 (None, reason).
    bc / img_idx: 給 X-Trace + feedback collector 用

    ★ 4.0.22: _meta_out (mutable dict) 用來累積診斷 metadata 給 admin Claude:
       - watermarks: AI 定位的 bbox 列表 (含 type 跟 bbox 座標)
       - watermark_area: 總面積比 0-1
       - skipped_center: 商品中心保護擋掉的 bbox 數
       - verify_remaining: K0_verify AI 判髒時的殘留描述
       不破壞 return signature (仍 (path/None, str))
    """
    if _meta_out is None:
        _meta_out = {}
    _meta_out.setdefault('watermarks', [])
    _meta_out.setdefault('watermark_area', 0.0)
    _meta_out.setdefault('skipped_center', 0)
    _meta_out.setdefault('verify_remaining', '')

    if not os.path.exists(img_path):
        return None, '檔案不存在'

    # 1. AI 定位 bbox
    watermarks = _ai_locate(img_path, bc=bc, img_idx=img_idx)
    if watermarks is None:
        return None, 'AI 定位失敗'
    _meta_out['watermarks'] = watermarks
    # ★ 4.0.53: 算 locate avg confidence 給 admin priority_review 排序
    confs = [w.get('confidence', 1.0) for w in watermarks if isinstance(w, dict)]
    _meta_out['locate_confidence'] = round(sum(confs) / len(confs), 3) if confs else 1.0
    if not watermarks:
        return None, '無水印 (無需救援)'

    # 2. 計算水印總面積比例
    total_area = 0.0
    for wm in watermarks:
        bbox = wm.get('bbox')
        if not bbox or len(bbox) != 4: continue
        x1, y1, x2, y2 = bbox
        if x2 < x1 or y2 < y1: continue
        total_area += (x2 - x1) * (y2 - y1)
    _meta_out['watermark_area'] = round(total_area, 4)
    if total_area > HARD_CAP_AREA:
        # 整張都是水印 (Disney 拼貼類), LaMa 會破壞商品本體
        return None, f'水印面積 {total_area*100:.0f}% > {HARD_CAP_AREA*100:.0f}% hard cap, 不救援 (避免破壞商品)'
    if total_area > MAX_AREA_RATIO:
        return None, f'水印面積 {total_area*100:.0f}% > {MAX_AREA_RATIO*100:.0f}%, 不救援'

    # 3. 載入圖, resize 到 800px (LaMa OOM 限制)
    # ★ 4.0.71: 應用 EXIF orientation 跟 _ai_locate 用同樣的座標系 (一致才對齊 bbox)
    from PIL import Image, ImageDraw, ImageOps
    try:
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
    except:
        return None, '圖檔讀失敗'
    if max(img.size) > 800:
        ratio = 800 / max(img.size)
        img = img.resize((int(img.size[0]*ratio), int(img.size[1]*ratio)), Image.LANCZOS)
    w, h = img.size

    # 4. 構造 mask (★ 4.0.39: padding=16: 對複雜 logo「方框 + 上下水平線 + 英文小字」吃 8px 不夠)
    # ★ 商品中心保護: bbox 中心點若在中心 60% 範圍 → 跳過 (避免 LaMa 把商品本體當水印填掉)
    CENTER_MIN, CENTER_MAX = 0.15, 0.85  # 中心 70% 為商品保護區
    mask = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(mask)
    skipped_center = 0
    applied_bboxes = []  # ★ 4.0.39: 收 LaMa 實際蓋過的歸一化 bbox, 給 verify 當 hint
    for wm in watermarks:
        bbox = wm.get('bbox')
        if not bbox or len(bbox) != 4: continue
        x1, y1, x2, y2 = bbox
        # 計算 bbox 中心點
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        # 中心點落在 [0.15, 0.85] 區域 = 商品本體, 跳過
        if CENTER_MIN <= cx <= CENTER_MAX and CENTER_MIN <= cy <= CENTER_MAX:
            skipped_center += 1
            continue
        # 也檢查 bbox 是否覆蓋過大區域 (> 50% 面積) — LaMa 會破壞商品
        if (x2-x1) * (y2-y1) > 0.50:
            skipped_center += 1
            continue
        px1 = max(0, int(x1*w) - MASK_PADDING)
        py1 = max(0, int(y1*h) - MASK_PADDING)
        px2 = min(w, int(x2*w) + MASK_PADDING)
        py2 = min(h, int(y2*h) + MASK_PADDING)
        draw.rectangle([(px1,py1),(px2,py2)], fill=255)
        # ★ 4.0.39: 把 padding 後的歸一化 bbox 收起來給 verify
        applied_bboxes.append([px1/w, py1/h, px2/w, py2/h])
    _meta_out['skipped_center'] = skipped_center
    # ★ 4.0.57: 修 AttributeError — draw (ImageDraw.Draw) 沒有 getbbox(),
    #   應該用 mask (Image) 的 getbbox(). 4.0.55 新同事撞 14 件 silent exception 就是這個.
    #   mask.getbbox() 全黑 → None, 有 pixel → 4-tuple. None = 全部 bbox 都被中心保護擋掉.
    if skipped_center > 0 and mask.getbbox() is None:
        # 全部 bbox 都被中心保護擋掉 → 沒水印可去
        return None, f'AI 框到商品本體 ({skipped_center} 個 bbox 在中心保護區), 跳過'

    # 5. LaMa inpaint (in-memory, 還沒寫盤) — 用 LaMa pool 並行
    try:
        lama, lama_lock = _acquire_lama()
        try:
            cleaned = lama(img, mask)
        finally:
            lama_lock.release()
    except Exception as e:
        return None, f'LaMa 失敗: {str(e)[:50]}'

    # 6. 驗證: AI 看 LaMa 結果還有沒有水印殘留 (小面積跳過 verify, 省 AI call)
    from io import BytesIO
    buf = BytesIO()
    cleaned.convert('RGB').save(buf, 'JPEG', quality=92, optimize=True)
    cleaned_bytes = buf.getvalue()
    if total_area >= SKIP_VERIFY_SMALL:
        # ★ 4.0.39: 把 LaMa 實際蓋過的 bbox 傳給 verify, 避免 verify 把 LaMa 沒碰的區域 (商品背景書封字/包裝紙字) 當殘留
        # ★ 4.0.53: verify 多回 confidence, 寫進 _meta_out 給 admin priority_review 排序用
        clean, remaining, verify_conf = _ai_verify_clean(cleaned_bytes, bc=bc, img_idx=img_idx, mask_bboxes=applied_bboxes)
        _meta_out['verify_remaining'] = remaining
        _meta_out['verify_confidence'] = verify_conf
        if not clean:
            return None, '驗證仍有殘留 (rollback)'

    # ★ 4.0.23: output_hash 用 cleaned_bytes 直接算 (跟 K0_verify input 同一份 bytes,
    #   byte-exact 對齊 API server 攔到的 hash → admin_review 直接命中 D:\images)
    #   不要寫盤後 read disk 算 — 雖然 binary write/read 該一樣, 但 cleaned_bytes 100% 保證
    try:
        from processors.feedback_collector import hash_bytes as _hash_bytes
        _meta_out['output_hash'] = _hash_bytes(cleaned_bytes)
    except Exception:
        pass

    # 7. 真乾淨, 寫盤覆蓋原圖
    # ★ 4.0.72: atomic write — 防 process kill / 斷電 / Ctrl+C 在 write 中間導致原圖損壞.
    #   原本 `open(img_path,'wb')` 立刻把原圖截斷成 size=0, 中間 kill = 原圖空白. 災難.
    #   改: 寫 .tmp 再 os.replace (atomic on Windows + Unix).
    try:
        tmp_path = img_path + '.lama.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(cleaned_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())  # 保證寫到 disk
            except Exception:
                pass
        os.replace(tmp_path, img_path)  # atomic rename
        return img_path, total_area
    except Exception as e:
        # 清掉 .tmp
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return None, f'寫盤失敗: {str(e)[:50]}'


def try_dewatermark_rejected(df, log_fn: Callable, progress_fn: Optional[Callable] = None, log_detail_fn: Optional[Callable] = None, ckpt=None, lama_device: str = 'auto', lama_pool_size=None) -> dict:
    log_detail = log_detail_fn if log_detail_fn else log_fn
    """
    對 V65 Stage E 標記 _reject_indices 的圖嘗試救援.
    救援成功 → 從 _reject_indices 移除該 index (圖會被保留)
    救援失敗 → _reject_indices 不變 (圖仍被丟棄)

    返回 stats
    """
    if '_reject_indices' not in df.columns:
        log_fn('[去水印救援] _reject_indices 欄不存在, 跳過')
        return {'total':0, 'saved':0, 'failed':0}

    has_reason = df['_filter_reason'].fillna('').str.strip().astype(bool) if '_filter_reason' in df.columns else None

    # 收集所有要救的圖 (idx, img_index, img_path)
    tasks = []
    for idx, row in df.iterrows():
        if has_reason is not None and has_reason.loc[idx]: continue
        rv = row.get('_reject_indices', '')
        if not rv or not isinstance(rv, str): continue
        try:
            reject_set = set(int(x) for x in rv.split(',') if x.strip())
        except: continue
        if not reject_set: continue
        img_field = str(row.get('圖片','') or '')
        paths = [p.strip() for p in img_field.split('|') if p.strip()]
        for img_i in reject_set:
            if 0 <= img_i < len(paths) and os.path.exists(paths[img_i]):
                tasks.append((idx, img_i, paths[img_i]))

    # ★ Checkpoint: 跳過已完成 (resume 時)
    if ckpt is not None:
        before = len(tasks)
        tasks = [t for t in tasks if not ckpt.is_done('k0_dewatermark', t[2])]
        skipped = before - len(tasks)
        if skipped > 0:
            log_fn(f'[去水印救援] ⏭ Resume 跳過 {skipped} 張已完成')

    log_fn(f'[去水印救援] 待救 {len(tasks)} 張 reject 圖 (LaMa + AI 定位)')
    if not tasks:
        return {'total':0, 'saved':0, 'failed':0}

    # ★ 預先載入 LaMa (warmup, 省首張 ~10s lazy load 卡頓) — 帶 device 偵測
    warmup_t0 = time.time()
    try:
        _init_lama_pool(force_device=lama_device, force_pool_size=lama_pool_size, log_fn=log_fn)
        log_fn(f'  [LaMa warmup] {time.time()-warmup_t0:.0f}s ({_lama_device_used} × {LAMA_POOL_SIZE} instance)')
    except Exception as e:
        log_fn(f'  ⚠ LaMa 載入失敗: {str(e)[:80]}')
        return {'total':len(tasks), 'saved':0, 'failed':len(tasks)}

    # 跑 救援 (並發 30, AI 定位是瓶頸; LaMa 本地 1-2s/張)
    saved_indices = {}  # {(idx, img_i): area_ratio or 'kept_no_watermark'}
    saved_details = []  # [(path, area_ratio)] LaMa 真正救起 (verify 通過, 1.jpg 已換)
    no_watermark_kept = []  # [(path)] K0 AI 看「無水印」直接保留 (信任第二意見)
    fail_reasons = {}
    fail_details = []  # [(path, reason)] - 真正失敗 (面積太大/verify rollback/LaMa 失敗)
    n_done = 0
    t0 = time.time()

    def _run(task):
        # ★ 4.0.56: 整個 _run 包 try/except 把 unhandled exception 轉成正常 return tuple,
        #   避免 line 895 caller except 無聲吞掉 (4.0.55 新同事跑批 9 件 exception 看不到 root cause).
        idx_orig, img_i_orig, path_orig = task
        try:
            return _run_impl(task)
        except Exception as _e:
            err_info = f'worker exception: {type(_e).__name__}: {str(_e)[:80]}'
            return idx_orig, img_i_orig, path_orig, False, err_info

    def _run_impl(task):
        idx, img_i, path = task
        # 撈商品條碼當 X-Trace bc
        try:
            bc = str(df.at[idx, '商品條碼']) if '商品條碼' in df.columns else f'IDX{idx}'
        except Exception:
            bc = f'IDX{idx}'
        # ★ 4.0.21: 在 LaMa 跑之前先算 input_hash + stage_e_hash
        #   (rescued case path 會被覆寫成新 jpg, LaMa 後再算就是 output_hash 了, 不是 input)
        #   - input_hash: LaMa 看到的原始 jpg bytes (LaMa workflow reference)
        #   - stage_e_hash: API server 攔截那份 (resize 1024+q85), admin 用這個找 D:\images
        input_hash_pre = ''
        stage_e_hash_pre = ''
        try:
            from processors.feedback_collector import hash_bytes, hash_stage_e_view
            try:
                with open(path, 'rb') as f:
                    input_hash_pre = hash_bytes(f.read())
            except Exception:
                pass
            try:
                stage_e_hash_pre = hash_stage_e_view(path)
            except Exception:
                pass
        except Exception:
            pass

        t_start = time.time()
        # ★ 4.0.22: 傳 mutable dict 進去, _try_dewatermark_one 會 update 帶診斷 metadata
        lama_meta = {}
        result, info = _try_dewatermark_one(path, bc=bc, img_idx=img_i, _meta_out=lama_meta)
        lama_ms = int((time.time() - t_start) * 1000)
        # 收 LaMa event 給 admin Claude review
        try:
            from processors.feedback_collector import get_collector, hash_bytes
            coll = get_collector()
            input_hash = input_hash_pre
            stage_e_hash = stage_e_hash_pre
            # ★ 4.0.23: output_hash 從 _meta_out 拿 (= sha256(cleaned_bytes), 跟 K0_verify
            #   API server 端攔到的 hash byte-exact 對齊). rollback case _meta_out 沒這欄 → 空字串.
            output_hash = lama_meta.get('output_hash', '') if result else ''
            # lama_out 分類
            if result is not None:
                lama_out = 'rescued'
            elif info == '無水印 (無需救援)':
                lama_out = 'no_watermark'
            elif info == 'AI 定位失敗':
                lama_out = 'ai_locate_fail'
            elif '驗證仍有殘留' in info:
                lama_out = 'rollback_residue'
            else:
                lama_out = 'not_attempted'
            coll.record_lama(
                bc=bc, idx=img_i,
                stage_e_dec='reject',  # 進到 LaMa 都是 Stage E reject 的
                lama_out=lama_out,
                lama_ms=lama_ms,
                input_hash=input_hash,
                output_hash=output_hash,
                stage_e_hash=stage_e_hash,
                # ★ 4.0.22 診斷 metadata
                watermarks=lama_meta.get('watermarks') or [],
                watermark_area=lama_meta.get('watermark_area', 0.0),
                skipped_center=lama_meta.get('skipped_center', 0),
                verify_remaining=lama_meta.get('verify_remaining', ''),
                # ★ 4.0.53 confidence
                locate_confidence=lama_meta.get('locate_confidence', 1.0),
                verify_confidence=lama_meta.get('verify_confidence', 1.0),
            )
        except Exception:
            pass  # feedback 是 best-effort, 失敗不影響跑批
        return idx, img_i, path, result is not None, info

    try:
        from checkpoint_manager import get_monitor, get_optimal_workers
        _mon_k0 = get_monitor()
        # ★ K0 = chat_short (AI locate 約 3-5s)
        actual_workers, w_info = get_optimal_workers(task_type='chat_short', warmup=False, max_cap=WORKERS_AI)
        # ★ 4.0.32: hub RTT-aware cap (K0 上次 ai_locate_fail 29 件就是 218ms × 38 並發 TLS 撐爆)
        try:
            from processors.utils import apply_hub_cap as _hcap
            _capped = _hcap(actual_workers)
            if _capped < actual_workers:
                log_fn(f'  [K0] 中介建議 {actual_workers} → hub RTT cap → {_capped}')
                actual_workers = _capped
        except Exception: pass
        # ★ 4.0.58: GPU pool floor — K0 並發不能低於 LaMa pool size (LaMa 是本地 GPU, 跟 HTTP RTT 無關)
        # 否則 pool 中部分 instance 永遠 idle, GPU memory 浪費 (e.g. 4070 SUPER 12GB pool=12, 並發 8 → 4 idle, 6.3GB VRAM 沒用)
        if actual_workers < LAMA_POOL_SIZE:
            log_fn(f'  [K0] 並發 {actual_workers} < LaMa pool {LAMA_POOL_SIZE}, 拉到 pool size (避免 GPU instance idle, RTT cap 只管 HTTP 不管本地 GPU)')
            actual_workers = LAMA_POOL_SIZE
        log_fn(f'  [K0] 動態並發: {actual_workers} (來源={w_info.get("source")}, my_share={w_info.get("my_rpm_share", "?")})')
    except Exception:
        _mon_k0 = None
        actual_workers = WORKERS_AI
    # ★ 4.0.36: AdaptiveCap (K0 chat + GPU 混合, chat 受 RTT 影響)
    # ★ 4.0.38: success_check — K0 _run 內部複雜, 區分 「API call fail」(該降 cap) 跟
    #   「業務正常但沒救起」(no_watermark/rollback/水印太大跳過, 跟 RTT 無關 — 不該降 cap).
    try:
        from processors.utils import get_or_create_adaptive_cap, make_adaptive_worker
        _max_k0 = max(actual_workers * 2, 48)
        _ac_k0 = get_or_create_adaptive_cap('k0', initial_cap=actual_workers, max_cap=_max_k0, min_cap=8)
        _ac_k0.set_log_fn(log_fn)
        log_fn(f'  [K0] adaptive cap 啟用: 起點={actual_workers}, max={_max_k0}, min=8')
        # _run return: (idx, img_i, path, result_is_not_none_bool, info_str)
        # success_check 邏輯: 只有「AI 定位失敗」才算 API call fail (chat 端問題, 該降 cap).
        # 其他情況 (LaMa rollback / no_watermark / 水印太大跳過 / 寫盤失敗) 不算 RTT 問題.
        def _k0_success(r):
            if not r or len(r) < 5: return True  # malformed → 不懲罰
            success_bool = r[3]
            if success_bool: return True  # rescued
            info = str(r[4] or '')
            # 「AI 定位失敗」(K0_locate chat call 失敗) 或 worker exception (4.0.56) 都算 fail, 該降 cap
            if 'AI 定位失敗' in info or 'worker exception' in info: return False
            return True  # 其他都是業務/本地問題, 不該降 cap
        _adaptive_run = make_adaptive_worker(_ac_k0, _run, success_check=_k0_success)
        _pool_max_k0 = _max_k0
    except Exception as _e:
        _ac_k0 = None
        _adaptive_run = _run
        _pool_max_k0 = actual_workers
        log_fn(f'  [K0] adaptive cap init 失敗 (退回靜態): {_e}')
    pool = ThreadPoolExecutor(max_workers=_pool_max_k0)
    futs = [pool.submit(_adaptive_run, t) for t in tasks]
    try:
        for f in as_completed(futs):
            if _mon_k0 and getattr(_mon_k0, 'user_stop', False):
                # 4.0.36: 喚醒 adaptive cap acquire 等待者, 否則 pool.shutdown(wait=True) deadlock
                if _ac_k0 is not None:
                    try: _ac_k0.shutdown()
                    except Exception: pass
                pool.shutdown(wait=False, cancel_futures=True)
                log_fn(f'  [K0] 收到用戶停止, 提早結束 (已跑 {n_done}/{len(tasks)}, in-flight {actual_workers} 個跑完)')
                break
            try:
                idx, img_i, path, ok, info = f.result()
                if ok:
                    # 真正 LaMa 救起 + verify 通過
                    saved_indices[(idx, img_i)] = info
                    saved_details.append((path, info))
                elif info == '無水印 (無需救援)':
                    # ★ 改回保守: K0 看不到不代表沒水印 (實測 3/4 誤判閒魚/MANGA/寶藏單品推薦)
                    # → 維持 reject (Stage E 已判該丟, K0 救不掉就丟)
                    no_watermark_kept.append(path)  # 保留紀錄方便 audit, 但不從 reject 移除
                    fail_reasons.setdefault(info, 0)
                    fail_reasons[info] += 1
                    fail_details.append((path, info))
                else:
                    # 真失敗 (面積太大 / verify 不過 / LaMa 失敗) → 維持 reject
                    fail_reasons.setdefault(info, 0)
                    fail_reasons[info] += 1
                    fail_details.append((path, info))
            except Exception as e:
                # ★ PauseException 不能吞, 傳到 pipeline 觸發 paused 流程
                if 'PauseException' in type(e).__name__: raise
            n_done += 1
            # ★ Checkpoint: 區分 mark_done / mark_failed
            #   - 成功 LaMa 救起 / 個別圖 (面積太大 / verify 殘留 / LaMa 失敗) → mark_done
            #   - API 失效 (AI 定位失敗 / AI 框到商品本體) → mark_failed (下次繼續會重試)
            if ckpt is not None:
                try:
                    # 判斷失敗類型
                    is_api_fail = False
                    if not ok and info:
                        # AI 定位失敗 = _ai_call return None = API 連續失敗
                        # 「AI 框到商品本體」也是 AI 定位異常 (可能 API 不穩)
                        # 4.0.56: worker exception (LaMa OOM / torch crash 等) 也下次重試
                        if 'AI 定位失敗' in info or 'AI 框到商品本體' in info or 'worker exception' in info:
                            is_api_fail = True
                    if is_api_fail:
                        ckpt.mark_failed('k0_dewatermark', path)  # 下次重試
                    else:
                        ckpt.mark_done('k0_dewatermark', path)  # 成功 或 個別圖
                    if n_done % 20 == 0:
                        ckpt.save(df=df)
                except Exception: pass
            if progress_fn:
                progress_fn('去水印救援', n_done, len(tasks))
            _step = max(1, len(tasks)//5)
            if n_done == 1 or n_done % _step == 0 or n_done == len(tasks):
                elapsed = time.time() - t0
                eta = (len(tasks) - n_done) * (elapsed / n_done) if n_done else 0
                n_lama = sum(1 for v in saved_indices.values() if v != 'kept_no_watermark')
                n_no_wm = len(no_watermark_kept)
                log_fn(f'  [去水印救援] {n_done}/{len(tasks)} | LaMa救起 {n_lama} | 無水印保留 {n_no_wm} | 耗時 {elapsed:.0f}s | ETA {eta:.0f}s')
    finally:
        pool.shutdown(wait=True)

    elapsed = time.time() - t0
    n_lama = sum(1 for v in saved_indices.values() if v != 'kept_no_watermark')
    n_no_wm = len(no_watermark_kept)
    n_real_fail = len(fail_details)
    log_fn(f'[去水印救援] 完成: LaMa救起 {n_lama} | K0看不到({n_no_wm}件 仍丟) | 真失敗 {n_real_fail} | 耗時 {elapsed:.0f}s')
    if fail_reasons:
        for r, n in sorted(fail_reasons.items(), key=lambda x:-x[1])[:5]:
            log_fn(f'    {r}: {n}')
    # ★ 詳情寫到詳細日誌檔 (GUI 不刷屏)
    if saved_details:
        log_detail(f'[去水印救援] LaMa 救起 (1.jpg 已換成乾淨版, verify 通過):')
        for path, area in saved_details:
            code = os.path.basename(os.path.dirname(path))
            fname = os.path.basename(path)
            area_str = f'{area*100:.0f}%' if isinstance(area, (int, float)) else str(area)
            log_detail(f'    ✅ {code}/{fname} (水印面積 {area_str})')
    if no_watermark_kept:
        log_detail(f'[去水印救援] K0 看「無水印」但維持 reject (K0 看不到≠沒水印, 實測誤判率高):')
        for path in no_watermark_kept:
            code = os.path.basename(os.path.dirname(path))
            fname = os.path.basename(path)
            log_detail(f'    ⚠ {code}/{fname}')
    if fail_details:
        log_detail(f'[去水印救援] 真失敗 (維持 reject, 圖會被丟):')
        for path, reason in fail_details:
            code = os.path.basename(os.path.dirname(path))
            fname = os.path.basename(path)
            log_detail(f'    ✗ {code}/{fname} → {reason}')

    # 更新 df: 從 _reject_indices 移除被救起的 index
    for (idx, saved_img_i), _ in saved_indices.items():
        rv = df.at[idx, '_reject_indices']
        if not isinstance(rv, str): continue
        try:
            curr = set(int(x) for x in rv.split(',') if x.strip())
        except:
            continue
        curr.discard(saved_img_i)
        df.at[idx, '_reject_indices'] = ','.join(str(x) for x in sorted(curr))

    return {
        'total': len(tasks),
        'saved': len(saved_indices),
        'failed': len(tasks) - len(saved_indices),
        'fail_reasons': fail_reasons,
        'elapsed': elapsed,
    }
