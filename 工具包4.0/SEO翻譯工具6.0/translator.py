# -*- coding: utf-8 -*-
"""
核心翻譯引擎：日文 → 繁體中文（含 SEO 標題優化）

功能：
- 句段切分 + 去重 + 本地 OpenCC（中→繁）
- 僅對含假名(日文)片段調用 API
- ThreadPoolExecutor + requests 連線池
- json_object 降級 & 純文本抽 JSON
- 片段級快取 cache_segments.jsonl
- 「商品簡述」固定映射
- 殘留假名二次清除
- Yahoo SEO 標題優化
"""
import json, os, re, sys, time, threading
from typing import Dict, List, Tuple, Callable, Optional
from email.utils import parsedate_to_datetime
import pandas as pd, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from opencc import OpenCC
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None


# ──────────────────── 全域設定 ────────────────────
class TranslatorConfig:
    def __init__(self):
        self.api_base = "https://api.example.com/v1"
        self.api_key = "<TEST_API_KEY>"
        self.api_keys: List[str] = []  # 多密鑰池（為空時退回 api_key）
        self.model = "gpt-5.4-mini"
        self.seo_model = "gpt-5.5"
        self.batch_size = 30  # 壓測 (15→30): 主副詞提取一次餵 30 個商品, 並發更密
        self.workers = 96  # 跟 V65 整體 64 並發對齊 (中介壓測 4 帳號 round-robin 可承載)
        self.timeout = 180
        self.max_retries = 2
        self.cache_version = "v2"
        self.debug = False
        self.enable_kana_cleanup = True
        self.enable_seo = True
        self.enable_translate = True  # 正文翻譯開關（閒魚導出可關閉）

    @property
    def keyword_batch_size(self) -> int:
        """關鍵詞提取批次"""
        return max(4, min(35, self.batch_size))

    @property
    def seo_batch_size(self) -> int:
        """SEO 優化批次"""
        return max(4, min(30, self.batch_size))

    @property
    def is_claude(self) -> bool:
        """判斷是否使用 Claude API（Anthropic Messages 格式）"""
        return "claude" in (self.model or "").lower() or "/claude/" in (self.api_base or "")

CFG = TranslatorConfig()

# ──────────────────── 全局停止信號 ────────────────────
_STOP_FN: Optional[Callable] = None

def _is_stopped() -> bool:
    """檢查是否收到停止信號。所有耗時操作前都應調用。"""
    return _STOP_FN is not None and _STOP_FN()

# ──────────────────── Session ────────────────────

# ── FailoverError：攜帶狀態碼，讓外層判斷是否該切線路 ──
class _ProgressSaved(Exception):
    """進度已保存，不輸出不完整結果。GUI 應捕獲此異常並提示用戶。"""
    pass

class FailoverError(Exception):
    """由 _chat_claude / _chat_openai 拋出，表示該線路重試耗盡但屬於可切線路的錯誤。"""
    def __init__(self, message: str, status_code: Optional[int] = None,
                 is_network_error: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.is_network_error = is_network_error

    def is_failover_eligible(self) -> bool:
        if self.is_network_error:
            return True
        if self.status_code in (429, 500, 502, 503, 504):
            return True
        return False


# ── KeyPool：線程安全的密鑰輪換池 ──
class KeyPool:
    """Round-robin 密鑰分配，429 時自動跳過冷卻中的 key。"""

    def __init__(self, keys: List[str]):
        self._keys = list(keys) if keys else []
        self._lock = threading.Lock()
        self._index = 0
        self._cooldowns: Dict[int, float] = {}  # key_index -> expiry

    @property
    def size(self) -> int:
        return len(self._keys)

    def next_key(self) -> str:
        """取下一個可用 key（round-robin，跳過冷卻中的）。"""
        if not self._keys:
            return ""
        now = time.time()
        with self._lock:
            # 嘗試所有 key，找到一個不在冷卻中的
            for _ in range(len(self._keys)):
                idx = self._index % len(self._keys)
                self._index += 1
                if self._cooldowns.get(idx, 0) <= now:
                    return self._keys[idx]
            # 全部冷卻中 → 返回等待時間最短的
            earliest_idx = min(self._cooldowns, key=self._cooldowns.get)
            return self._keys[earliest_idx]

    def mark_429(self, key: str, cooldown: float = 10.0):
        """標記 key 被限速，短暫冷卻。"""
        with self._lock:
            for i, k in enumerate(self._keys):
                if k == key:
                    self._cooldowns[i] = time.time() + cooldown
                    break

    def mark_ok(self, key: str):
        """清除 key 的冷卻。"""
        with self._lock:
            for i, k in enumerate(self._keys):
                if k == key:
                    self._cooldowns.pop(i, None)
                    break


_KEY_POOL: Optional[KeyPool] = None
_KEY_POOL_LOCK = threading.Lock()


def _get_key_pool() -> KeyPool:
    """取得或建立 KeyPool 單例。"""
    global _KEY_POOL
    with _KEY_POOL_LOCK:
        keys = CFG.api_keys if CFG.api_keys else ([CFG.api_key] if CFG.api_key else [])
        if _KEY_POOL is None or _KEY_POOL.size != len(keys):
            _KEY_POOL = KeyPool(keys)
        return _KEY_POOL


# ── RouteManager：線程安全的多線路管理器 ──
class RouteManager:
    """管理 aws/droid 兩條線路的 session 緩存、冷卻和配置快照。"""

    COOLDOWN_SECONDS = 180  # 坏线路冷却 3 分钟，避免反复踩坑
    _SLUG_RE = re.compile(r'/(aws|droid)/')
    _ALL_SLUGS = ["aws", "droid"]

    def __init__(self, api_base: str, api_key: str, is_claude: bool):
        self._lock = threading.Lock()
        self._primary_url = api_base
        self._api_key = api_key
        self.is_claude = is_claude  # 配置快照
        self._fingerprint = (api_base, api_key, is_claude)
        self._routes: List[str] = self._derive_routes(api_base)
        self._sessions: Dict[str, requests.Session] = {}
        self._cooldowns: Dict[str, float] = {}  # route -> expiry timestamp
        self._last_success_route: Optional[str] = None  # 最近成功線路，優先使用

    @classmethod
    def _derive_routes(cls, api_url: str) -> List[str]:
        """從 api_url 自動推導三條線路，以配置中的線路為 primary。"""
        m = cls._SLUG_RE.search(api_url)
        if not m:
            return [api_url]  # 退化為單線路模式
        current_slug = m.group(1)
        # 以配置中的線路為首，其餘按固定順序排後面
        other_slugs = [s for s in cls._ALL_SLUGS if s != current_slug]
        ordered = [current_slug] + other_slugs
        routes = []
        for slug in ordered:
            routes.append(cls._SLUG_RE.sub(f'/{slug}/', api_url))
        return routes

    @property
    def fingerprint(self):
        return self._fingerprint

    def get_available_routes(self) -> List[str]:
        """返回可用線路，僅在配置主線 cooldown 時才優先最近成功線路，否則配置主線優先。"""
        now = time.time()
        with self._lock:
            available = [r for r in self._routes if self._cooldowns.get(r, 0) <= now]
            if not available:
                available = list(self._routes)  # 全部冷卻 → best-effort
            # 只在配置主線被冷卻時，才把最近成功線路提前
            primary_in_cooldown = (self._routes and self._cooldowns.get(self._routes[0], 0) > now)
            if primary_in_cooldown and self._last_success_route and self._last_success_route in available:
                available.remove(self._last_success_route)
                available.insert(0, self._last_success_route)
            return available

    def get_session(self, api_base: str) -> requests.Session:
        """取得或建立指定線路的 session（短鎖，只保護 dict 讀寫）。"""
        with self._lock:
            if api_base in self._sessions:
                return self._sessions[api_base]
        # 建立 session 在鎖外面（不包住建立過程）
        session = requests.Session()
        if Retry:
            retries = Retry(total=1, backoff_factor=0.5,
                            status_forcelist=[500, 502, 503, 504],
                            allowed_methods=None)  # total=1: 減少內層重試，外層 failover 兜底
        else:
            retries = 0
        adapter = HTTPAdapter(pool_connections=60, pool_maxsize=60, max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        # 設置 headers
        if self.is_claude:
            session.headers.update({
                "x-api-key": self._api_key or "",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            })
        else:
            session.headers.update({
                "Authorization": f"Bearer {self._api_key}" if self._api_key else "",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        with self._lock:
            # 雙重檢查：其他線程可能已經建好了
            if api_base not in self._sessions:
                self._sessions[api_base] = session
            else:
                session.close()
            return self._sessions[api_base]

    def mark_failed(self, api_base: str):
        """把線路加入冷卻。"""
        with self._lock:
            self._cooldowns[api_base] = time.time() + self.COOLDOWN_SECONDS

    def mark_success(self, api_base: str):
        """清除線路冷卻（線路恢復），並記錄為最近成功線路。"""
        with self._lock:
            self._cooldowns.pop(api_base, None)
            self._last_success_route = api_base

    def close_all(self):
        """關閉所有緩存的 session。"""
        with self._lock:
            for s in self._sessions.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()
            self._cooldowns.clear()


# ── RouteManager 全局單例 + active count ──
_ROUTE_MGR: Optional[RouteManager] = None
_ROUTE_MGR_LOCK = threading.Lock()
_ROUTE_MGR_ACTIVE_COUNT = 0
_ROUTE_MGR_ACTIVE_LOCK = threading.Lock()


def _get_route_manager() -> RouteManager:
    """取得或建立 RouteManager。翻譯運行中（active_count > 0）跳過 fingerprint 檢測。"""
    global _ROUTE_MGR
    with _ROUTE_MGR_LOCK:
        new_fp = (CFG.api_base, CFG.api_key, CFG.is_claude)
        with _ROUTE_MGR_ACTIVE_LOCK:
            active = _ROUTE_MGR_ACTIVE_COUNT > 0
        if _ROUTE_MGR is not None:
            if active or _ROUTE_MGR.fingerprint == new_fp:
                return _ROUTE_MGR
            # fingerprint 變了且沒有活躍任務 → 重建
            _ROUTE_MGR.close_all()
        _ROUTE_MGR = RouteManager(CFG.api_base, CFG.api_key, CFG.is_claude)
        return _ROUTE_MGR


def _acquire_route_manager() -> RouteManager:
    """翻譯入口調用：取得 manager 並增加 active count。"""
    global _ROUTE_MGR_ACTIVE_COUNT
    mgr = _get_route_manager()
    with _ROUTE_MGR_ACTIVE_LOCK:
        _ROUTE_MGR_ACTIVE_COUNT += 1
    return mgr


def _release_route_manager():
    """翻譯結束（finally）調用：減少 active count。"""
    global _ROUTE_MGR_ACTIVE_COUNT
    with _ROUTE_MGR_ACTIVE_LOCK:
        _ROUTE_MGR_ACTIVE_COUNT = max(0, _ROUTE_MGR_ACTIVE_COUNT - 1)


def _extract_route_slug(api_base: str) -> str:
    """精確按路徑段提取 aws/droid/ultra，識別不到返回 'custom'。"""
    m = re.search(r'/(aws|droid|ultra)/', api_base)
    return m.group(1) if m else "custom"


def _parse_retry_after(response) -> Optional[float]:
    """解析 Retry-After header，兼容秒數和 HTTP-date 兩種格式。"""
    val = response.headers.get("Retry-After")
    if not val:
        return None
    try:
        seconds = float(val)
        return max(0.0, seconds)
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(val)
        diff = (dt - dt.now(dt.tzinfo)).total_seconds()
        return max(0.0, diff)
    except Exception:
        pass
    return None

cc = OpenCC('s2t')

# ──────────────────── 應用目錄（打包兼容）────────────────────
def _app_dir() -> str:
    """取得應用程式實際所在目錄（打包後用 exe 路徑，開發時用 __file__）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# ──────────────────── 自學習詞典 ────────────────────
def _learned_dict_path():
    return os.path.join(_app_dir(), "learned_dict.json")

def _load_learned_dict() -> Dict[str, str]:
    """載入自學習詞典（AI 翻譯過的日文→中/英映射）"""
    if os.path.exists(_learned_dict_path()):
        try:
            with open(_learned_dict_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_learned_dict(d: Dict[str, str]):
    """儲存自學習詞典"""
    try:
        with open(_learned_dict_path(), "w", encoding="utf-8") as f:
            json.dump(d, ensure_ascii=False, indent=2, fp=f)
    except Exception:
        pass

def _learn_from_kana_fix(before: str, after: str):
    """
    從假名清除的結果中提取新映射。
    比較 before/after，找出被替換的假名片段→中/英文，存入自學習詞典。
    下次遇到同樣的假名會直接替換，不需要再調 AI。
    """
    if not before or not after or before == after:
        return
    # 提取 before 中的假名片段
    kana_re = re.compile(r'[\u3040-\u30FF\uFF66-\uFF9D]+')
    kana_spans = [(m.start(), m.end(), m.group()) for m in kana_re.finditer(before)]
    if not kana_spans:
        return

    learned = _load_learned_dict()
    changed = False

    for start, end, kana_text in kana_spans:
        if len(kana_text) < 2:  # 單字假名不學（太短，容易誤匹配）
            continue
        if kana_text in learned or kana_text in KATAKANA_TO_LATIN or kana_text in JAPANESE_FIXED_MAP:
            continue  # 已知映射，跳過

        # 在 after 中找到對應位置的替換文字
        # 策略：before 中假名前後的非假名文字在 after 中也存在，用它們定位
        prefix = before[:start]
        suffix = before[end:]

        # 找 prefix 在 after 中的位置
        if prefix:
            p_idx = after.find(prefix)
            if p_idx < 0:
                continue
            after_start = p_idx + len(prefix)
        else:
            after_start = 0

        # 找 suffix 在 after 中的位置
        if suffix:
            s_idx = after.find(suffix, after_start)
            if s_idx < 0:
                continue
            after_end = s_idx
        else:
            after_end = len(after)

        replacement = after[after_start:after_end]

        # 驗證：替換文字不含假名、不為空、長度合理
        if (replacement and not kana_re.search(replacement)
            and len(replacement) <= len(kana_text) * 3
            and replacement != kana_text):
            learned[kana_text] = replacement
            changed = True

    if changed:
        _save_learned_dict(learned)

# 自學習詞典在 KATAKANA_TO_LATIN / JAPANESE_FIXED_MAP 之後載入（見第305行附近）

# ──────────────────── 簡中→台灣繁中 用語本地化 ────────────────────
# OpenCC 只做字級轉換，這裡補平台用語差異
CN_TO_TW_TERMS: Dict[str, str] = {
    # 鹹魚/淘寶常見用語 → 台灣用語
    "成色": "品相", "品相": "品相",
    "包邮": "含運", "包郵": "含運",
    "不包邮": "運費另計", "不包郵": "運費另計",
    "自提": "自取", "面交": "面交",
    "闲置": "閒置", "閒置": "閒置",
    "出闲置": "出清", "出閒置": "出清",
    "秒发": "即寄", "秒發": "即寄",
    "白菜价": "超低價", "白菜價": "超低價",
    "捡漏": "撿便宜", "撿漏": "撿便宜",
    "全新未拆": "全新未拆封",
    "九成新": "九成新", "九五新": "近全新", "几乎全新": "近全新",
    "幾乎全新": "近全新",
    "正品": "正品", "高仿": "仿品",
    "代购": "代購", "海淘": "海外購入",
    "断码": "斷碼", "斷碼": "斷碼",
    "清仓": "清倉", "清倉": "清倉",
    "尾货": "尾貨",
    "瑕疵品": "瑕疵品", "微瑕": "微瑕",
    "收纳": "收納", "收納": "收納",
    "手办": "公仔", "手辦": "公仔",
    "潮牌": "潮牌", "国潮": "國潮",
    "数码": "數位", "數碼": "數位",
    "充电": "充電", "充電": "充電",
    "内存": "記憶體", "內存": "記憶體",
    "运存": "運行記憶體",
    "屏幕": "螢幕",
    "外观": "外觀",
    "机身": "機身",
    "镜头": "鏡頭", "鏡頭": "鏡頭",
    "耳机": "耳機",
    "音箱": "音響", "音響": "音響",
    "鼠标": "滑鼠",
    "键盘": "鍵盤",
    "显卡": "顯示卡", "顯卡": "顯示卡",
    "硬盘": "硬碟",
    "优盘": "隨身碟", "U盘": "隨身碟",
    "笔记本": "筆電", "笔记本电脑": "筆記型電腦",
    "筆記本電腦": "筆記型電腦", "筆記本": "筆電",
    "平板": "平板",
    "样品": "樣品", "樣品": "樣品",
    "尺码": "尺碼",
    "均码": "均碼",
    "码数": "碼數",
    # 補充常見硬轉漏項
    "充电宝": "行動電源", "充電寶": "行動電源",
    "移动电源": "行動電源", "移動電源": "行動電源",
    "数据线": "傳輸線", "數據線": "傳輸線",
    "外放": "外放", "公放": "擴音",
    "视频": "影片", "視頻": "影片",
    "软件": "軟體", "軟件": "軟體",
    "网络": "網路", "網絡": "網路",
    "宽带": "寬頻",
    "打印机": "印表機", "打印機": "印表機",
    "U盤": "隨身碟",
    "激光": "雷射",
    "信息": "資訊", "訊息": "訊息",
}

def _apply_cn_tw_terms(s: str) -> str:
    """將簡中平台用語轉為台灣用語"""
    if not isinstance(s, str) or not s:
        return s
    for k in sorted(CN_TO_TW_TERMS.keys(), key=len, reverse=True):
        if k in s:
            s = s.replace(k, CN_TO_TW_TERMS[k])
    return s

# ──────────────────── 商品簡述映射 ────────────────────
FIXED_SUMMARY_MAP = [
    {"jp": ["やや傷や汚れあり目につく傷や汚れがある", "やや傷や汚れあり"], "zh": "略有刮痕或髒污"},
    {"jp": ["目立った傷や汚れなし細かな使用感・傷・汚れはあるが、目立たない", "目立った傷や汚れなし"], "zh": "無明顯刮痕或髒污"},
    {"jp": ["傷や汚れあり多くの人が見てわかるような傷や汚れがある", "傷や汚れあり"], "zh": "有刮痕或髒污"},
    {"jp": ["新品、未使用新品で購入し、一度も使用していない", "新品、未使用"], "zh": "全新、未使用"},
    {"jp": ["未使用に近い数回使用し、あまり使用感がない", "未使用に近い"], "zh": "近乎未使用"},
    {"jp": ["全体的に状態が悪い商品の全体に目立つ傷や汚れ、ダメージがある", "全体的に状態が悪い"], "zh": "商品有明顯的刮痕、髒污或損傷"},
]

_PUNCT_RE = re.compile(r"[ \t\r\n、。．\.，,：:！!？\?・•\|]+", re.UNICODE)

def _norm(s: str) -> str:
    if not isinstance(s, str):
        if pd.isna(s): return ""
        s = str(s)
    return _PUNCT_RE.sub("", s.strip())

# 從商品簡述抽取結構化屬性 (for SEO AI 上下文)
# Phase 23 咸鱼 14k 實測有 187 個屬性鍵, 這些是 SEO 相關性最高的幾個
SEO_ATTR_KEEP_KEYS = [
    '年代', '款式', '材質', '釉色工藝', '窯口', '品牌', '主題',
    '類別', '種類', '工藝', '顏色', '產地', '商品形態', '紫砂泥料',
    '木質材質', '適用性別', '圖案', '形狀',
]
SEO_ATTR_SKIP_VALUES = {'其他', '不詳', '年代不詳', '暫無', '未說明', 'other/其他'}
_ATTR_KV_RE = re.compile(r'([^\n：:]{1,8})[：:]([^\n]{1,60})')

def extract_seo_attrs(summary: str, title: str = '') -> str:
    """從商品簡述抽結構化屬性 + 從標題推斷類別附加 profile 提示 (Phase 29)

    summary: 商品簡述 (可為空)
    title: 商品標題 (若提供, 會加類別推斷 profile 作為 AI 提示)
    """
    parts = []
    seen = set()
    if isinstance(summary, str) and summary.strip():
        for m in _ATTR_KV_RE.finditer(summary):
            k = m.group(1).strip()
            v = m.group(2).strip()
            if k in SEO_ATTR_KEEP_KEYS and v and v not in SEO_ATTR_SKIP_VALUES and k not in seen:
                parts.append(f'{k}:{v}')
                seen.add(k)
    # Phase 29 整合: 若提供 title 且能推斷類別, 附加類別 profile
    # 這個提示讓 AI 知道該類 popular 池用什麼主詞/信任詞/長度
    try:
        hint = build_category_attr_hint(title or '', summary or '')
        if hint:
            parts.append(hint)
    except Exception:
        pass
    return ' | '.join(parts)


def apply_fixed_summary(value: str) -> str:
    raw = value if isinstance(value, str) else ("" if pd.isna(value) else str(value))
    normed = _norm(raw)
    for rule in FIXED_SUMMARY_MAP:
        keys = sorted([_norm(k) for k in rule["jp"]], key=len, reverse=True)
        for k in keys:
            if k and k in normed:
                return rule["zh"]
    try:
        return cc.convert(raw)
    except Exception:
        return raw

# ──────────────────── 片假名→英文 ────────────────────
KATAKANA_TO_LATIN: Dict[str, str] = {
    "テレックス": "TERREX", "クライマプルーフ": "Climaproof", "アノラック": "Anorak",
    "ゴアテックス": "GORE-TEX", "ゴアテクス": "GORE-TEX",
    "ルイヴィトン": "Louis Vuitton", "エルメス": "Hermes", "シャネル": "CHANEL",
    "グッチ": "GUCCI", "プラダ": "PRADA", "ディオール": "Dior",
    "フェンディ": "FENDI", "セリーヌ": "CELINE", "バレンシアガ": "Balenciaga",
    "サンローラン": "Saint Laurent", "ボッテガ": "Bottega",
    "クロムハーツ": "Chrome Hearts", "カルティエ": "Cartier",
    "ティファニー": "Tiffany & Co.", "スワロフスキー": "Swarovski",
    "モンクレール": "Moncler", "カナダグース": "Canada Goose",
    "ナイキ": "NIKE", "アディダス": "adidas", "ニューバランス": "New Balance",
    "ノースフェイス": "The North Face", "パタゴニア": "Patagonia",
    "アークテリクス": "Arc'teryx", "コロンビア": "Columbia", "コーチ": "Coach",
}

# ──────────────────── 英文→中文 後處理字典（模型漏翻時硬替換） ────────────────────
ENGLISH_TO_CHINESE: Dict[str, str] = {
    # 寶石
    "Rose Quartz": "粉晶", "rose quartz": "粉晶",
    "Clear Quartz": "白水晶", "clear quartz": "白水晶",
    "Smoky Quartz": "茶晶", "smoky quartz": "茶晶",
    "Strawberry Quartz": "草莓晶", "strawberry quartz": "草莓晶",
    "Iris Quartz": "彩虹水晶", "iris quartz": "彩虹水晶",
    "Rutilated Quartz": "髮晶", "rutilated quartz": "髮晶",
    "Pink Spinel": "粉尖晶石", "pink spinel": "粉尖晶石",
    "Amethyst": "紫水晶", "amethyst": "紫水晶",
    "Tiger Eye": "虎眼石", "tiger eye": "虎眼石", "Tiger's Eye": "虎眼石",
    "Moonstone": "月光石", "moonstone": "月光石",
    "Sunstone": "太陽石", "sunstone": "太陽石",
    "Labradorite": "拉長石", "labradorite": "拉長石",
    "Tourmaline": "碧璽", "tourmaline": "碧璽",
    "Aquamarine": "海藍寶", "aquamarine": "海藍寶",
    "Citrine": "黃水晶", "citrine": "黃水晶",
    "Garnet": "石榴石", "garnet": "石榴石",
    "Peridot": "橄欖石", "peridot": "橄欖石",
    "Opal": "蛋白石", "opal": "蛋白石",
    "Jade": "翡翠", "jade": "翡翠",
    "Agate": "瑪瑙", "agate": "瑪瑙",
    "Onyx": "黑曜石", "onyx": "黑曜石",
    "Lapis Lazuli": "青金石", "lapis lazuli": "青金石",
    "Malachite": "孔雀石", "malachite": "孔雀石",
    "Turquoise": "綠松石", "turquoise": "綠松石",
    "Coral": "珊瑚", "coral": "珊瑚",
    "Amber": "琥珀", "amber": "琥珀",
    "Fluorite": "螢石", "fluorite": "螢石",
    "Obsidian": "黑曜石", "obsidian": "黑曜石",
    "Aventurine": "東陵石", "aventurine": "東陵石",
    "Carnelian": "紅玉髓", "carnelian": "紅玉髓",
    "Jasper": "碧玉", "jasper": "碧玉",
    "Howlite": "白松石", "howlite": "白松石",
    "Rhodonite": "薔薇輝石", "rhodonite": "薔薇輝石",
    "Sodalite": "方鈉石", "sodalite": "方鈉石",
    "Tanzanite": "坦桑石", "tanzanite": "坦桑石",
    "Alexandrite": "亞歷山大石", "alexandrite": "亞歷山大石",
    "Topaz": "黃玉", "topaz": "黃玉",
    "Sapphire": "藍寶石", "sapphire": "藍寶石",
    "Ruby": "紅寶石", "ruby": "紅寶石",
    "Emerald": "祖母綠", "emerald": "祖母綠",
    "Diamond": "鑽石", "diamond": "鑽石",
    # 材質
    "Pearl": "珍珠", "pearl": "珍珠",
    "Crystal": "水晶", "crystal": "水晶",
    "Sterling Silver": "純銀", "sterling silver": "純銀",
    "Stainless Steel": "不鏽鋼", "stainless steel": "不鏽鋼",
    "Gold Plated": "鍍金", "gold plated": "鍍金",
    "Rose Gold": "玫瑰金", "rose gold": "玫瑰金",
    "White Gold": "白金", "white gold": "白金",
    "Platinum": "鉑金", "platinum": "鉑金",
    "Copper": "銅", "copper": "銅",
    "Brass": "黃銅", "brass": "黃銅",
    "Leather": "皮革", "leather": "皮革",
    "Silk": "絲綢", "silk": "絲綢",
    "Cotton": "棉", "cotton": "棉",
    "Ceramic": "陶瓷", "ceramic": "陶瓷",
    "Porcelain": "瓷器", "porcelain": "瓷器",
    "Wooden": "木質", "wooden": "木質",
    # 商品類型 / 配件
    "Bracelet": "手鏈", "bracelet": "手鏈",
    "Necklace": "項鍊", "necklace": "項鍊",
    "Pendant": "吊墜", "pendant": "吊墜",
    "Earrings": "耳環", "earrings": "耳環",
    "Earring": "耳環", "earring": "耳環",
    "Ring": "戒指", "ring": "戒指",
    "Bangle": "手鐲", "bangle": "手鐲",
    "Anklet": "腳鏈", "anklet": "腳鏈",
    "Brooch": "胸針", "brooch": "胸針",
    "Charm": "吊飾", "charm": "吊飾",
    "Beads": "珠子", "beads": "珠子",
    "Bead": "珠", "bead": "珠",
    "Chain": "鏈條", "chain": "鏈條",
    "Clasp": "扣環", "clasp": "扣環",
    "Tassel": "流蘇", "tassel": "流蘇",
    # 常見通用商品詞（品牌後面的詞也要翻）
    "Sneakers": "運動鞋", "sneakers": "運動鞋",
    "Shoes": "鞋", "shoes": "鞋",
    "Boots": "靴子", "boots": "靴子",
    "Sandals": "涼鞋", "sandals": "涼鞋",
    "Slippers": "拖鞋", "slippers": "拖鞋",
    "Handbag": "手提包", "handbag": "手提包",
    "Tote Bag": "托特包", "tote bag": "托特包",
    "Shoulder Bag": "肩背包", "shoulder bag": "肩背包",
    "Crossbody Bag": "斜背包", "crossbody bag": "斜背包",
    "Clutch Bag": "手拿包", "clutch bag": "手拿包",
    "Backpack": "後背包", "backpack": "後背包",
    "Wallet": "錢包", "wallet": "錢包",
    "Purse": "皮夾", "purse": "皮夾",
    "Pouch": "收納袋", "pouch": "收納袋",
    "Bag": "包", "bag": "包",
    "Watch": "手錶", "watch": "手錶",
    "Sunglasses": "太陽眼鏡", "sunglasses": "太陽眼鏡",
    "Scarf": "圍巾", "scarf": "圍巾",
    "Hat": "帽子", "hat": "帽子",
    "Cap": "帽子", "cap": "帽子",
    "Belt": "皮帶", "belt": "皮帶",
    "Gloves": "手套", "gloves": "手套",
    "Jacket": "外套", "jacket": "外套",
    "Coat": "大衣", "coat": "大衣",
    "Shirt": "襯衫", "shirt": "襯衫",
    "Dress": "洋裝", "dress": "洋裝",
    "Skirt": "裙子", "skirt": "裙子",
    "Pants": "褲子", "pants": "褲子",
    "Jeans": "牛仔褲", "jeans": "牛仔褲",
    "Sweater": "毛衣", "sweater": "毛衣",
    "Hoodie": "帽T", "hoodie": "帽T",
    "T-Shirt": "T恤", "t-shirt": "T恤",
    "Vest": "背心", "vest": "背心",
    "Cardigan": "針織外套", "cardigan": "針織外套",
    # 功能 / 概念
    "Crystal Healing": "水晶療癒", "crystal healing": "水晶療癒",
    "Chakra": "脈輪", "chakra": "脈輪",
    "Healing": "療癒", "healing": "療癒",
    "Meditation": "冥想", "meditation": "冥想",
    "Handmade": "手工", "handmade": "手工",
    "Hand Made": "手工", "hand made": "手工",
    "Vintage": "復古", "vintage": "復古",
    "Antique": "古董", "antique": "古董",
    "Natural": "天然", "natural": "天然",
    "Genuine": "正品", "genuine": "正品",
    "Adjustable": "可調節", "adjustable": "可調節",
    "Unisex": "男女通用", "unisex": "男女通用",
    "Limited Edition": "限量版", "limited edition": "限量版",
    # 顏色
    "Pink": "粉色", "pink": "粉色",
    "Blue": "藍色", "blue": "藍色",
    "Green": "綠色", "green": "綠色",
    "Red": "紅色", "red": "紅色",
    "Purple": "紫色", "purple": "紫色",
    "White": "白色", "white": "白色",
    "Black": "黑色", "black": "黑色",
    "Yellow": "黃色", "yellow": "黃色",
    "Orange": "橙色", "orange": "橙色",
    "Silver": "銀色", "silver": "銀色",
    "Gold": "金色", "gold": "金色",
    "Multi-color": "多色", "multi-color": "多色",
    "Rainbow": "彩虹色", "rainbow": "彩虹色",
}

# ──────────────────── 日文殘留句式清理（後處理兜底） ────────────────────
# 常見漏網的日文句式/詞尾，在翻譯後處理中硬替換
JAPANESE_RESIDUAL_PATTERNS: List[Tuple[str, str]] = [
    # ── 完整固定短句（長詞優先，放前面避免被短詞先截斷） ──
    ("ご了承ください", "請見諒"),
    ("ご確認ください", "請確認"),
    ("ご覧ください", "請查看"),
    ("お願いします", ""),
    ("お願い致します", ""),
    ("でございます", ""),
    # ── 變形/亂碼式日文殘句（必須在短詞之前，否則短詞先截斷導致完整匹配失敗） ──
    ("1度だけ使用し壺た", "僅使用一次"),
    ("1度だけ使用しまた", "僅使用一次"),
    ("1度だけ使用しました", "僅使用一次"),
    ("使用し壺た", "已使用"),
    ("使用しまた", "已使用"),
    ("出品し壺す", "出售"),
    ("出品しまず", "出售"),
    # ── 商品狀態描述（高確定性固定搭配） ──
    ("出品いたします", "出售"),
    ("出品します", "出售"),
    ("発送いたします", "寄出"),
    ("発送します", "寄出"),
    ("使用していました", "曾使用"),
    ("使用しています", "使用中"),
    ("使用しました", "已使用"),
    ("1度だけ使用", "僅使用一次"),
    ("数回使用", "使用數次"),
    ("未使用品になります", "為未使用品"),
    ("中古品になります", "為二手品"),
    ("未使用です", "未使用"),
    ("新品です", "全新"),
    ("中古品です", "二手品"),
    ("美品です", "品相良好"),
    # ── 常見句式殘留（只處理高確定性的完整搭配） ──
    ("ではありません", "並非"),
    ("ありません", "沒有"),
]

def _cleanup_japanese_residual(s: str) -> str:
    """清理翻譯後仍殘留的日文句式/詞尾。"""
    if not isinstance(s, str) or not s:
        return s
    for jp, zh in JAPANESE_RESIDUAL_PATTERNS:
        if jp in s:
            s = s.replace(jp, zh)
    return s

def _apply_english_to_chinese(s: str) -> str:
    """後處理：把模型漏翻的英文寶石名/材質名硬替換成中文，並清理替換後的重複詞。"""
    if not isinstance(s, str) or not s:
        return s
    for eng in sorted(ENGLISH_TO_CHINESE.keys(), key=len, reverse=True):
        if eng in s:
            s = s.replace(eng, ENGLISH_TO_CHINESE[eng])
    # 清理替換後的重複詞（例如 "虎眼石 虎眼石手珠" → "虎眼石手珠"）
    s = _dedup_adjacent_words(s)
    return s


def _dedup_repeated_phrases(s: str) -> str:
    """清理連續完全重複的多詞片段 (e.g. 'MARC JACOBS MARC JACOBS' → 'MARC JACOBS')。

    比 _dedup_adjacent_words 更激進: 處理「2-5 詞連續重複」的品牌名情況。
    迭代直到沒有重複, 因為移除後可能露出新的重複。
    """
    if not isinstance(s, str) or not s:
        return s
    prev = None
    while prev != s:
        prev = s
        words = s.split()
        if len(words) < 2:
            break
        # 從長到短偵測 (5 詞 → 1 詞)
        for n in range(min(5, len(words) // 2), 0, -1):
            new_words = words[:]
            i = 0
            changed = False
            while i + 2 * n <= len(new_words):
                seg1 = ' '.join(new_words[i:i + n])
                seg2 = ' '.join(new_words[i + n:i + 2 * n])
                # 大小寫不敏感比對, 必須含字母 (排除全是數字)
                if (seg1.lower() == seg2.lower()
                        and re.search(r'[a-zA-Z一-鿿]', seg1)
                        and len(seg1) >= 2):
                    # 砍掉重複的後半 (保留 seg1)
                    del new_words[i + n:i + 2 * n]
                    changed = True
                    # 不 advance i, 可能 i 位置又出現新的重複
                    continue
                i += 1
            if changed:
                s = ' '.join(new_words)
                break  # 重來最外層 while loop
    return s


def _normalize_excel_escapes(s: str) -> str:
    """把 Excel 換行 escape (_x000d_) 換成普通空格. 保留可讀性."""
    if not isinstance(s, str) or not s:
        return s
    # _x000d_ 是 \r 的 escape, 換成空格 (避免被 SEO 階段當特殊符號)
    s = s.replace('_x000d_', ' ').replace('_x000D_', ' ')
    # 收斂連續空格到 1 個
    s = re.sub(r' {2,}', ' ', s)
    return s


def _dedup_adjacent_words(s: str) -> str:
    """清理相鄰重複詞：如果前一個詞完整包含在後一個詞裡，去掉前者。"""
    parts = s.split()
    if len(parts) <= 1:
        return s
    result = []
    i = 0
    while i < len(parts):
        # 看當前詞是否被下一個詞包含
        if i + 1 < len(parts) and parts[i] in parts[i + 1]:
            i += 1  # 跳過當前短詞，保留下一個長詞
            continue
        # 看當前詞是否和上一個已保留的詞重複
        if result and parts[i] in result[-1]:
            i += 1
            continue
        # 看當前詞是否包含上一個已保留的詞（當前更長）
        if result and result[-1] in parts[i]:
            result[-1] = parts[i]  # 用更長的替換更短的
            i += 1
            continue
        result.append(parts[i])
        i += 1
    return " ".join(result)

# ──────────────────── 日文常見詞→中文 固定映射 ────────────────────
JAPANESE_FIXED_MAP: Dict[str, str] = {
    # 工藝/修復
    "金継ぎ": "金繼修復", "金繕い": "金繕修復", "金繕": "金繕修復",
    # 傳統工藝品
    "こけし": "木芥子人偶", "べこ": "牛擺飾", "赤べこ": "赤牛擺飾",
    "木目込み": "木目嵌", "木目込": "木目嵌",
    "だるま": "達磨", "まねき猫": "招財貓",
    # 容器/器皿
    "ぐい呑み": "小酒杯", "ぐい呑": "小酒杯", "ぐいのみ": "小酒杯",
    "なます皿": "膾皿", "向付": "小菜碟",
    "小物入れ": "小物收納", "蓋付き": "附蓋",
    "耳付き": "附耳", "足付き": "附足",
    # 曲物
    "曲げわっぱ": "曲木便當盒", "わっぱ": "曲木盒",
    # 其他常見詞
    "くらわんか": "鯨鉢", "値下げ": "降價",
    "まとめて": "整批", "まとめ": "整批",
    "仰向け": "仰臥", "滝登り": "鯉躍龍門",
    "あり": "", "なし": "無",  # 「銘あり」→「銘」,  only when standalone-ish
    "入り": "入", "付き": "附",
    # 素材
    "ブロンズ": "Bronze", "エッチング": "蝕刻版畫",
    "ライトアンバー": "淺琥珀色",
    # 茶道/器具
    "水差し": "水壺", "急須": "茶壺", "湯呑み": "茶杯", "湯飲み": "茶杯",
    "茶入": "茶入", "菓子器": "糕點器",
    # 常見後綴
    "型抜き": "模具成型", "首振り": "搖頭",
    "好き": "喜愛",
    # 源數據亂碼/截斷修正
    "安提g": "古董", "安提ー": "古董",
    "マグ杯": "馬克杯", "フリーカップ": "Free杯",
    # ── 殘留假名修正（從實際翻譯結果中收集） ──
    "つば九郎": "燕子隊吉祥物",
    "ロンハーマン": "Ron Herman",
    "ゑり正": "衿正",  # 京都和服店
    "さざなみ": "漣",  # 小波紋
    "目の眼": "目之眼",  # 日文雜誌名
    "ビブリア": "Biblia",  # 書名
    "作り帶": "造型帶",  # 和服帶
    "塩ビ管": "PVC管",  # 聚氯乙烯管
    "輪るピングドラム": "迴轉企鵝罐",  # 動畫作品名
    "ピングドラム": "企鵝罐",
}

# 啟動時載入自學習詞典，合併到固定映射（KATAKANA_TO_LATIN 已定義）
_learned = _load_learned_dict()
if _learned:
    for k, v in _learned.items():
        if k not in KATAKANA_TO_LATIN and k not in JAPANESE_FIXED_MAP:
            JAPANESE_FIXED_MAP[k] = v

def replace_japanese_fixed(s: str) -> str:
    """將常見日文詞替換為中文，在 API 翻譯前預處理"""
    if not isinstance(s, str) or not s:
        return s
    for k in sorted(JAPANESE_FIXED_MAP.keys(), key=len, reverse=True):
        if k in s and JAPANESE_FIXED_MAP[k]:  # 空值映射跳過（需要上下文判斷的）
            s = s.replace(k, JAPANESE_FIXED_MAP[k])
    return s

def load_katakana_map(path: str) -> Dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    ext = os.path.splitext(path)[1].lower()
    mp: Dict[str, str] = {}
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            for k, v in obj.items():
                k, v = str(k).strip(), str(v).strip()
                if k and v:
                    mp[k] = v
        return mp
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sep = "\t" if "\t" in line else ("," if "," in line else None)
            if sep is None:
                continue
            k, v = line.split(sep, 1)
            k, v = k.strip(), v.strip()
            if k and v:
                mp[k] = v
    return mp

def replace_katakana_to_latin(s: str) -> str:
    if not isinstance(s, str) or not s:
        return "" if not isinstance(s, str) and pd.isna(s) else (s if isinstance(s, str) else str(s))
    for k in sorted(KATAKANA_TO_LATIN.keys(), key=len, reverse=True):
        if k in s:
            s = s.replace(k, KATAKANA_TO_LATIN[k])
    return s

# ──────────────────── 文字判斷 ────────────────────
def is_latin_only(s: str) -> bool:
    return bool(re.fullmatch(r'[\s\-\.,_~:/\\\(\)\[\]\{\}\+\=\*&%\$#@!\'\\"0-9A-Za-z]+', s))

def has_kana(s: str) -> bool:
    # 排除 ・(U+30FB) 和 ー(U+30FC) — 分隔符/長音不算假名殘留
    cleaned = re.sub(r'[・ー]', '', s)
    return bool(re.search(r'[\u3040-\u30FF\uFF66-\uFF9D]', cleaned))

def has_cjk(s: str) -> bool:
    return bool(re.search(r'[\u4E00-\u9FFF]', s))

def looks_like_chinese(s: str) -> bool:
    return bool(re.search(r"[的了這那什么嗎嘛吧與与為为們们還还會会就把被給给在著着]", s))

def looks_like_japanese_kanji(s: str) -> bool:
    if re.search(r"[円￥々〆ヶヵ・〜※〒]", s):
        return True
    jp_kw = [
        "中古", "新品", "未使用", "付属", "付属品", "送料", "発送", "即購入",
        "値下げ", "専用", "状態", "動作", "確認", "返品", "交換", "保証",
        "受取", "連絡", "説明書", "箱", "写真", "可能", "不可", "希望", "限定", "正規"
    ]
    return any(k in s for k in jp_kw)

# ──────────────────── Yahoo 搜索算法模擬器（Vespa BM25F 核心） ────────────────────
import math

def _yahoo_tokenize(text: str) -> List[str]:
    """
    模擬 Yahoo 拍賣的斷字規則（Vespa linguist + CJK bigram）：
    - 空格分隔不同 token
    - 連續中文做 character-level n-gram（Yahoo 用 bigram overlapping）
    - 連續英數是一個 token（品牌/型號整體匹配）
    - 全形符號斷字
    """
    tokens = []
    for part in text.split():
        if part:
            tokens.append(part)
    return tokens

def _cjk_bigrams(text: str) -> List[str]:
    """
    生成 CJK bigram 列表。Yahoo Vespa 對中文用 overlapping bigram 索引：
    「古伊萬里盤」→ ['古伊', '伊萬', '萬里', '里盤']
    搜「伊萬」能命中「古伊萬里盤」就是因為 bigram 索引。
    """
    chars = [c for c in text if '\u4E00' <= c <= '\u9FFF']
    if len(chars) < 2:
        return chars
    return [chars[i] + chars[i+1] for i in range(len(chars) - 1)]

def _contains_query(title: str, query: str) -> bool:
    """
    檢查標題是否完整包含搜索詞（子字串匹配）。
    Yahoo 的核心：完整匹配 = 搜得到，不匹配 = 零曝光。
    """
    if not query or not title:
        return False
    return query in title

def _bm25_term_score(tf: int, doc_len: int, avg_dl: float = 20.0,
                     k1: float = 1.2, b: float = 0.75) -> float:
    """
    BM25 單詞項分數。Yahoo Vespa 用的就是 BM25 變體。
    tf: 詞頻（query 在 title 出現次數）
    doc_len: 文檔長度（標題字數）
    avg_dl: 平均文檔長度（Yahoo 拍賣標題約 20 字）
    k1: 詞頻飽和參數（1.2 = Vespa 默認）
    b: 長度正規化（0.75 = Vespa 默認）

    IDF 省略（因為我們只排名單品，不跨文檔比較）
    """
    if tf == 0:
        return 0.0
    norm = 1 - b + b * (doc_len / avg_dl)
    return tf / (tf + k1 * norm)

def _field_weight_score(title: str, primary_kw: str, secondary_kw: str) -> float:
    """
    BM25F 欄位加權。Yahoo Vespa 的 fieldMatch 特性：
    - title 權重 = 1.0（最高，我們的目標欄位）
    - description 權重 ≈ 0.3
    - seller_info 權重 ≈ 0.1
    這裡只算 title 欄位，但模擬 BM25F 的 TF 計算。
    """
    doc_len = len(title)
    score = 0.0
    if primary_kw:
        tf = title.count(primary_kw)
        score += _bm25_term_score(tf, doc_len) * 1.0  # title field weight
    if secondary_kw:
        tf = title.count(secondary_kw)
        score += _bm25_term_score(tf, doc_len) * 1.0
    return score

def _proximity_score(title: str, kw1: str, kw2: str) -> float:
    """
    計算兩個關鍵詞在標題中的距離分數。
    Yahoo Vespa 引擎用 nativeProximity 做排名。
    距離越近分數越高（1.0 = 相鄰，0.0 = 不在標題中）。
    Vespa 實際公式：proximity = 1 / (1 + min_gap)
    """
    if not kw1 or not kw2:
        return 0.5  # 只有一個詞時給中間分
    pos1 = title.find(kw1)
    pos2 = title.find(kw2)
    if pos1 < 0 or pos2 < 0:
        return 0.0
    # 計算兩詞之間的字元間距
    if pos1 < pos2:
        gap = pos2 - (pos1 + len(kw1))
    else:
        gap = pos1 - (pos2 + len(kw2))
    gap = max(0, gap)
    # Vespa nativeProximity: 相鄰=1.0, 隔1字=0.5, 隔3字=0.25
    return 1.0 / (1.0 + gap * 0.5)

def _position_decay(title: str, kw: str) -> float:
    """
    位置衰減：關鍵詞越靠前，權重越高。
    Yahoo Vespa 的 firstOccurrence 特性：標題前段的詞權重更大。
    公式：decay = 1 - (position / title_length) * 0.5
    """
    if not kw or kw not in title:
        return 0.0
    pos = title.find(kw)
    title_len = max(len(title), 1)
    return 1.0 - (pos / title_len) * 0.5

def _query_coverage(title: str, primary_kw: str, secondary_kw: str) -> float:
    """
    查詢覆蓋度：多少比例的搜索詞被標題匹配到。
    Vespa 的 queryCompleteness + fieldCompleteness 特性。
    100% 覆蓋 = 最高排名，部分覆蓋 = 降權。
    """
    total = 0
    matched = 0
    if primary_kw:
        total += 1
        if primary_kw in title:
            matched += 1
    if secondary_kw:
        total += 1
        if secondary_kw in title:
            matched += 1
    return matched / max(total, 1)

def yahoo_relevancy_score(title: str, primary_kw: str, secondary_kw: str) -> dict:
    """
    Yahoo 搜索 relevancy 評分 (v2 — 基於我們挖的真實 Phase 18/20/21/25/32/36 數據)

    重新設計依據 (vs 舊版):
    - Phase 32: rel = 30M + buy_count → 主詞精確匹配是「被搜到」唯一條件 (40 分)
    - Phase 36: TOP 1 主詞平均第 14 字 → 位置不主導 (位置權重 15→5)
    - Phase 25 修正: popular 池有「SEO 堆積型 50+ 字」+ 「正常成熟賣家 20-25 字」兩種,
      古董類正常賣家 24 字也能 popular (如三鼎實業正德通寶 51 件銷量), 不必拉到 50+
    - Phase 18+20: 廢詞 (美術/置物/茶道) 0 瀏覽 → 必扣分 (-20)
    - 副詞 substring 太嚴 → 改 char-level 部分匹配 (15 分)
    - proximity 沒實測證據 → 移除
    - BM25F 模糊 → 移除, 用 char 命中比例代替

    新權重 (滿 100):
      40 主詞完整在標題 (Phase 32 — 被搜到唯一條件)
      15 副詞 char-level 比例匹配 (取代精確 substring)
      10 主詞防撞池 (古董類非通用 2 字裸用)
      10 主詞長度合理 (≥2 字, 不能縮成 1 字)
      10 標題長度 20-45 字 (V16 實測 sort=rel TOP 10 區間)
      10 主詞在前半段 (Phase 36 — 不主導但有用)
      5  無重複詞
    扣分:
      -20 含 Phase 18+20 廢詞 (美術/工蕓/置物/茶道/帶留)
      -15 含日文假名
      -15 含簡體字
      -10 含咸魚套話 (議價/撿漏/感興趣)
    """
    issues = []
    details = {}

    if not title: title = ''
    if not primary_kw: primary_kw = ''
    if not secondary_kw: secondary_kw = ''

    # ── 1. 主詞完整在標題 (40 分) — Phase 32 被搜到的唯一條件 ──
    pk_match = primary_kw and primary_kw in title
    pk_pts = 40 if pk_match else 0
    details["pk_match"] = pk_match
    if primary_kw and not pk_match:
        issues.append(f"主詞「{primary_kw}」未在標題 → 買家搜不到 (-40)")

    # ── 2. 副詞 char-level 比例匹配 (15 分) — token 拆解後在就行 ──
    if secondary_kw:
        # 排除標點符號, 看實質字元在標題的比例
        sk_chars = [c for c in secondary_kw if c.strip() and c not in '，。/、 ']
        if sk_chars:
            in_count = sum(1 for c in sk_chars if c in title)
            sk_rate = in_count / len(sk_chars)
            sk_pts = 15 * sk_rate
        else:
            sk_pts = 15
        details["sk_char_rate"] = round(sk_rate if sk_chars else 1.0, 2)
        if sk_chars and sk_rate < 0.5:
            issues.append(f"副詞「{secondary_kw}」char 匹配 {sk_rate*100:.0f}% 過低")
    else:
        sk_pts = 15  # 無副詞不扣

    # ── 3. 主詞防撞池 (10 分) — V27 sort=popular TOP 30 古董率重驗 (2026-04-27) ──
    # 真撞池 (古董率 ≤ 10%, 必加前綴): 加入「掛件/頭飾/吊墜/花器/印章/擺件/茶杯/罐」
    # 中等 (10-30%): 盤/杯/擺飾/佛像/香爐/手鐲/銀幣/花瓶/瑪瑙珠 — 仍建議加前綴更穩
    # 不撞池 (古董率 ≥ 50%): 銅器 73%/刨刃 63%/銀元 60%/瓷壺 53%/茶碗 40% — 移除
    risky_2char = {'瓶','碗','盤','杯','罐','擺件','擺飾','掛件','頭飾',
                   '佛像','茶杯','香爐','手鐲','戒指','吊墜',
                   '銀幣','花瓶','花器','印章'}
    if primary_kw in risky_2char:
        anti_pts = 0
        issues.append(f"主詞「{primary_kw}」是通用 2 字會撞現代池 → 應加古/老/朝代前綴")
    else:
        anti_pts = 10
    details["anti_clash"] = anti_pts

    # ── 4. 主詞長度合理 (10 分) — 防 [619] 主詞縮成 1 字 ──
    pk_len = len(primary_kw) if primary_kw else 0
    if pk_len >= 2:
        pk_len_pts = 10
    elif pk_len == 1:
        pk_len_pts = 0
        issues.append(f"主詞只 1 字「{primary_kw}」 → 過短無 SEO 價值")
    else:
        pk_len_pts = 0
    details["pk_len"] = pk_len

    # ── 5. 標題長度 20-45 字 (10 分) — 2026-04-27 V16 重驗
    # 在 sort=rel (買家真排名) 下: 復古胸針 TOP 10 = 48 字, 古董飾品 = 51 字,
    #   水晶手鏈 = 33 字, 復古耳環 = 36 字, 洋裝 = 38 字, 和田玉 = 44 字
    # 沒有「越短越好」也沒「越長越好」, 25-45 是常見區間
    # 但 V14 證實: popular 池 ≠ rel 池, 之前「50+ 是 SEO 堆積型」結論基於 popular 不可靠
    tlen = len(title)
    if 20 <= tlen <= 45:
        len_pts = 10  # 黃金區間 (V16 實測 TOP 10 平均落點)
    elif 16 <= tlen < 20 or 45 < tlen <= 55:
        len_pts = 7
    elif 12 <= tlen < 16 or 55 < tlen <= 65:
        len_pts = 4
    elif tlen < 12:
        len_pts = 0
        issues.append(f"標題過短 {tlen} 字, 流量信號不足")
    else:  # >65
        len_pts = 2
        issues.append(f"標題 {tlen} 字過長, 關鍵詞密度被稀釋")
    details["length"] = tlen

    # ── 6. 主詞在前半段 (10 分) — Phase 36 TOP 1 平均第 14 字 ──
    if primary_kw and pk_match:
        idx = title.find(primary_kw)
        rel_pos = idx / max(len(title), 1)
        if rel_pos <= 0.3:
            pos_pts = 10  # 前 30%
        elif rel_pos <= 0.5:
            pos_pts = 7
        elif rel_pos <= 0.7:
            pos_pts = 5
        else:
            pos_pts = 2
        details["pk_position"] = round(rel_pos, 2)
    else:
        pos_pts = 0

    # ── 7. 無重複詞 (5 分) ──
    words = title.split()
    has_dup = len(words) != len(set(words)) and len(words) > 2
    dup_pts = 5 if not has_dup else 0
    if has_dup:
        dupes = [w for w in set(words) if words.count(w) > 1]
        issues.append(f"重複詞 {dupes}")

    # ── 扣分 1: 真實廢詞 (-20) — V25 + 6704 件實跑校正 ──
    # 「鎮陶」單獨是廢詞但「景德鎮陶瓷」「鎮陶瓷」是合法品名 → 加排除模式
    # 「術品」單獨是廢詞但「藝術品」是合法 → 排除「藝術品」
    waste_words_check = ['美術品','工蕓','蕓品','古美術','藝品','器裝','品置']
    found_waste = [w for w in waste_words_check if w in title]
    # 「術品」: 排除「藝術品」「美術品」(美術品本身就在列表)
    if '術品' in title and '藝術品' not in title and '美術品' not in title:
        found_waste.append('術品')
    # 「鎮陶」: 排除「景德鎮陶」「鎮陶瓷」(這是合法品名)
    if '鎮陶' in title and '景德鎮陶' not in title and '鎮陶瓷' not in title:
        found_waste.append('鎮陶')
    waste_penalty = -20 if found_waste else 0
    if found_waste:
        issues.append(f"V25 實測 0 流量廢詞: {found_waste} (-20)")

    # ── 扣分 2: 假名 (-15) ──
    kana_penalty = -15 if has_kana(title) else 0
    if has_kana(title):
        issues.append("含日文假名 (-15)")

    # ── 扣分 3: 簡體字 (-15) ──
    # 用 opencc s2t 對比, 不同則含簡體
    try:
        from opencc import OpenCC
        if not hasattr(yahoo_relevancy_score, '_cc'):
            yahoo_relevancy_score._cc = OpenCC('s2t')
        cc = yahoo_relevancy_score._cc
        trad = cc.convert(title)
        # 排除常見異體字 + 6704 件實跑誤殺白名單 (繁簡共用 + 異體字)
        SIMP_WHITELIST = set('回家具咸托里台杯斗厘峰彩床几制范向于扎雕松只折扇双里准并历')
        diff_chars = set()
        for o, t in zip(title, trad):
            if o != t and o not in SIMP_WHITELIST:
                diff_chars.add(o)
        if diff_chars:
            simp_penalty = -15
            issues.append(f"含簡體字 {list(diff_chars)[:5]} (-15)")
        else:
            simp_penalty = 0
    except Exception:
        simp_penalty = 0

    # ── 扣分 4: 咸魚套話 (-10) ──
    junk_phrases = ['議價','撿漏','大開門','感興趣','私聊','一線下鄉','#興趣','#撿漏']
    found_junk = [j for j in junk_phrases if j in title]
    junk_penalty = -10 if found_junk else 0
    if found_junk:
        issues.append(f"咸魚套話 {found_junk} (-10)")

    # ── 加總 ──
    raw_score = (pk_pts + sk_pts + anti_pts + pk_len_pts + len_pts + pos_pts + dup_pts
                 + waste_penalty + kana_penalty + simp_penalty + junk_penalty)
    final_score = max(0, min(100, raw_score))

    details["breakdown"] = {
        "pk_match": pk_pts,
        "sk_char": round(sk_pts, 1),
        "anti_clash": anti_pts,
        "pk_len": pk_len_pts,
        "length": len_pts,
        "pk_position": pos_pts,
        "no_dup": dup_pts,
        "waste_penalty": waste_penalty,
        "kana_penalty": kana_penalty,
        "simp_penalty": simp_penalty,
        "junk_penalty": junk_penalty,
    }

    return {
        "score": final_score,
        "primary_match": pk_match,
        "secondary_match": secondary_kw and (sk_pts >= 7.5),  # >=50% char match
        "has_kana": has_kana(title),
        "has_duplicate": has_dup,
        "length": tlen,
        "details": details,
        "issues": issues,
    }

# ─── 簡→繁高頻字對映表 (實測 Yahoo 單向合併的字) ───
# 來源: 141,961 樣本 + 35 組同義詞實測
# 僅列「實測有差異」的字 — 寫繁體能吃雙邊流量
SIMP_TO_TRAD_MAP = {
    # 強制反向修正: V1 實測「和田玉」367k 是「和闐玉」107k 的 3.4 倍, 統一用「和田玉」
    "和闐玉": "和田玉", "和闐": "和田",
    # 商品類型
    "手表": "手錶", "项链": "項鍊", "项鏈": "項鍊", "項鏈": "項鍊",
    "耳飾": "耳環", "耳饰": "耳環", "耳环": "耳環",
    "手鐲": "手鐲", "腕鐲": "手鐲",  # 實測「手鐲」命中腕鐲搜索
    "眼鏡": "眼鏡", "墨鏡": "眼鏡",  # 實測「眼鏡」命中墨鏡/太陽眼鏡
    "戒子": "戒指", "戒指": "戒指",  # 「戒指」命中戒子
    "髮飾": "髮簪", "发簪": "髮簪", "髮簪": "髮簪",
    # 基礎簡轉繁
    "复古": "復古", "古董": "古董", "中古": "中古",
    "服裝": "服裝", "服装": "服裝",
    "手机": "手機", "电脑": "電腦", "笔电": "筆電",
    "发": "髮", "铁": "鐵", "针": "針", "环": "環",
    "书": "書", "车": "車", "电": "電",
    # 中樞 替換詞.xlsx 台灣用語校正
    "鏈": "鍊", "閑": "閒", "裸靴": "踝靴",
    "連衣": "連身", "連體": "連身",
    # V40 規格單位 — 中國式 → 台灣式 (跟 替換詞.xlsx 對齊, 防 AI 寫回中國式)
    "厘米": "cm", "釐米": "cm",
    "毫米": "mm", "毫升": "ml",
    "公分": "cm",  # 統一用 cm (對手實測 cm 也佔 50%)
    "公克": "g",
    # 注意: 「克」單字不轉 (避免誤動克拉/克難等專名), 由 prompt 規則 + regex 處理
}

# ─── 破壞 Yahoo 分詞的符號（實測搜索返回 0 件）───
BAD_SEPARATORS = ['・', '．', '——', '—', '／']
# 注意：2026-04-27 重新驗證: Yahoo 已升級分詞, 連寫/中劃線/空格 結果完全相同 (4759 件)
# 「中劃線保留連續子串」優勢已不存在, 不再特別保留 (但符號移除避免破壞無妨)

# ─── 中樞整合: 咸鱼/淘寶套話 + Yahoo 違規關鍵詞清理 ───
# 這些詞出現在標題會浪費字數或觸發 Yahoo 屏蔽, 純機械清理不涉 SEO 判斷
import json as _json_cleanup
from pathlib import Path as _Path_cleanup
CLEANUP_PHRASES = set()
CLEANUP_BANNED = set()
CLEANUP_CAT_MAP = {}
CAT_PROFILES = {}  # Phase 29: 63 類 SEO 知識庫
_cleanup_err = None
try:
    _here = _Path_cleanup(__file__).resolve().parent
    _cleanup_path = _here / 'cleanup_resources.json'
    if _cleanup_path.exists():
        _cleanup = _json_cleanup.loads(_cleanup_path.read_text(encoding='utf-8'))
        CLEANUP_PHRASES = set(_cleanup.get('delete_all_phrases', []))
        CLEANUP_BANNED = set(_cleanup.get('banned_words', []))
        CLEANUP_CAT_MAP = _cleanup.get('cat_mapping', {})
    # 載入 Phase 29/30/35 282-3107 類 profile
    _profile_path = _here / 'category_profiles.json'
    if _profile_path.exists():
        CAT_PROFILES = _json_cleanup.loads(_profile_path.read_text(encoding='utf-8'))
    # 載入 T5 類別推斷索引 (word → top 3 cats with weights)
    _idx_path = _here / 'cat_inference_index.json'
    if _idx_path.exists():
        globals()['CAT_INFERENCE_INDEX'] = _json_cleanup.loads(_idx_path.read_text(encoding='utf-8'))
    else:
        globals()['CAT_INFERENCE_INDEX'] = {}
except Exception as _e:
    _cleanup_err = str(_e)


# ─── Phase 29: 從標題推斷 Yahoo 二級分類 ───
def infer_category_from_title(title: str, summary: str = '') -> dict:
    """從標題+簡述推斷類別 (T5 升級版)

    算法: 使用 CAT_INFERENCE_INDEX (word→cats with weights)
    1. Tokenize 標題+簡述
    2. 每個 token 查 index → 得到候選 cats 加權分
    3. 累積分最高的 cat 勝出 (偏好深度 3 級 > 2 級 > 頂層)
    4. 最小 score 門檻放寬到 1.5 (單詞精確命中 leaf 就可推斷)
    """
    if not CAT_PROFILES or not title:
        return None
    idx = globals().get('CAT_INFERENCE_INDEX', {})
    if not idx:
        # fallback 舊邏輯
        text = f'{title} {summary}'
        best_path, best_score = None, 0
        for path, prof in CAT_PROFILES.items():
            dks = prof.get('detect_keywords', [])
            score = sum(1 for kw in dks if kw in text)
            for w in prof.get('primary_candidates', [])[:5]:
                if w in text: score += 0.5
            if score > best_score: best_score, best_path = score, path
        if best_path and best_score >= 2:
            return CAT_PROFILES[best_path]
        return None

    text = f'{title} {summary}'
    # 掃描 2-5 字 substring 查 index
    path_scores = {}
    seen_tokens = set()
    for length in [5, 4, 3, 2]:
        for i in range(len(text) - length + 1):
            token = text[i:i+length]
            if token in seen_tokens: continue
            if token in idx:
                seen_tokens.add(token)
                for path, score in idx[token]:
                    # 深度加權: 3 級 > 2 級 > 頂層
                    depth = path.count('>') + path.count(' > ')
                    depth_bonus = 1.0 + depth * 0.2
                    # 長 token 加權 (更具體)
                    token_bonus = 1.0 + (length - 2) * 0.3
                    path_scores[path] = path_scores.get(path, 0) + score * depth_bonus * token_bonus
    if not path_scores:
        return None
    # 最高分
    best_path = max(path_scores, key=path_scores.get)
    best_score = path_scores[best_path]
    if best_score >= 1.5:
        return CAT_PROFILES.get(best_path)
    return None


def build_category_attr_hint(title: str, summary: str = '') -> str:
    """若能推斷類別, 返回一段 attr 字串給 AI 作主詞候選指引"""
    prof = infer_category_from_title(title, summary)
    if not prof:
        return ''
    cands = prof.get('primary_candidates', [])[:5]
    trust = prof.get('trust_words', [])[:2]
    target = prof.get('target_length', 0)
    path = prof.get('path', '')
    parts = []
    if path: parts.append(f'類別:{path}')
    if cands: parts.append(f'該類主詞候選:{"/".join(cands)}')
    if trust: parts.append(f'該類信任詞:{"/".join(trust)}')
    if target: parts.append(f'該類平均長度:{target}字')
    return ' | '.join(parts)


def _strip_junk_phrases(text: str) -> str:
    """刪除咸鱼套話 + Yahoo 違規詞 (純機械, 非 SEO 判斷)"""
    if not text or not isinstance(text, str): return text
    result = text
    # 先刪長 phrase (避免短 phrase 破壞長 phrase)
    for phrase in sorted(CLEANUP_PHRASES, key=len, reverse=True):
        if phrase and len(phrase) >= 3 and phrase in result:
            result = result.replace(phrase, ' ')
    # 刪違規詞
    for w in CLEANUP_BANNED:
        if w and len(w) >= 2 and w in result:
            result = result.replace(w, '')
    import re as _re
    return _re.sub(r'\s+', ' ', result).strip()


def lookup_yahoo_category(taobao_cat_id):
    """用咸鱼 cat_id 查對應 Yahoo 分類"""
    if not taobao_cat_id: return None
    return CLEANUP_CAT_MAP.get(str(taobao_cat_id).strip())

def _normalize_to_traditional(text: str) -> str:
    """只轉換實測驗證過的字，不做全面簡繁轉換以免誤傷"""
    if not text: return text
    result = text
    for simp, trad in SIMP_TO_TRAD_MAP.items():
        if simp != trad and simp in result:
            result = result.replace(simp, trad)
    return result


def _strip_bad_separators(text: str) -> str:
    """移除會破壞 Yahoo 分詞的符號"""
    if not text: return text
    result = text
    for sep in BAD_SEPARATORS:
        result = result.replace(sep, ' ')
    # 壓縮多餘空白
    import re as _re
    result = _re.sub(r'\s+', ' ', result).strip()
    return result


# ─── Phase 28+實測 通用詞防撞池清單 (AI 偷懶時強制補前綴) ───
# 這些詞裸用會撞現代商品池, 古董類該用「古X/老X/朝代X」複合詞
RISKY_GENERIC_PRIMARY = {
    '瓶','碗','盤','杯','罐','擺件','擺飾','掛件','頭飾',
    '粉彩','佛像','陶瓷','首飾','木雕','茶壺','銅器','花瓶','花器',
    '銀幣','銀元','刨刃','刨刀','茶杯','茶碗','香爐','手鐲','戒指',
    '吊墜','印章','花錢','錢幣','水壺','筆筒','硯臺','煙嘴',
}

# 載入 anti_clash_map + buyer_search_keywords
ANTI_CLASH_MAP = {}
BUYER_KEYWORDS = set()
try:
    _acm_path = _Path_cleanup(__file__).resolve().parent / 'anti_clash_map.json'
    if _acm_path.exists():
        ANTI_CLASH_MAP = _json_cleanup.loads(_acm_path.read_text(encoding='utf-8'))
    _bsk_path = _Path_cleanup(__file__).resolve().parent / 'buyer_search_keywords.json'
    if _bsk_path.exists():
        _bsk = _json_cleanup.loads(_bsk_path.read_text(encoding='utf-8'))
        BUYER_KEYWORDS = set(_bsk.get('all_keywords', []))
except Exception:
    pass


def _apply_anti_pool_clash(primary_kw, secondary_kw, title_context):
    """若主詞是通用通用詞, 按上下文加前綴 (實測防撞池)

    title_context: 原標題 + 商品簡述合併字串, 用來偵測朝代/產地/材質
    """
    if not primary_kw or primary_kw not in RISKY_GENERIC_PRIMARY:
        return primary_kw
    ctx = str(title_context)
    # 優先順序: 朝代 > 產地 > 材質 > 通用「古」
    import re as _re
    # 產地優先 (日本/法國/德國商品 用「日本花器」(25k) 而非「清花器」(4))
    # — 2026-04-27 修: 71190 件實測「清花器」224 件全是日本商品被誤套清代前綴
    for p in ['日本','法國','德國','義大利','韓國','美國','英國']:
        if p in ctx:
            # 對日本商品直接「日本+品類」(實測「日本花器」25k vs 古花器 844)
            if primary_kw in {'銀幣','銀元','錢幣','刨刀','刨刃','花器','花瓶','茶杯','茶碗','香爐'}:
                return f'{p}{primary_kw}'
            return f'{p}古{primary_kw}'
    # 朝代關鍵字偵測 (只用兩字明確朝代名, 移除單字「清/明/宋/元」避免「清水/清晰」誤觸發)
    for dyn in ['大正','昭和','明治','宋代','明代','清代','民國','元代','唐代','光緒','乾隆','雍正','康熙','同治','嘉慶','道光','咸豐']:
        if dyn in ctx:
            return f'{dyn}{primary_kw}'
    # 材質偵測
    if primary_kw in {'手鐲','吊墜','戒指'}:
        for m in ['和田玉','翡翠','瑪瑙','水晶','琉璃','壽山石','銀','金']:
            if m in ctx:
                return f'{m}{primary_kw}'
    # 無其他線索: 通用「古」或「老」
    if primary_kw in {'銀幣','銀元','錢幣','花錢'}:
        return f'古{primary_kw}'
    if primary_kw in {'刨刃','刨刀'}:
        return f'老{primary_kw}'
    # ★ Phase 30 擴展: 若主詞在 ANTI_CLASH_MAP, 從 BUYER_KEYWORDS 裡找「含該詞的複合詞」
    if primary_kw in ANTI_CLASH_MAP:
        # 從 BUYER 詞庫找複合詞
        for bk in BUYER_KEYWORDS:
            if len(bk) >= 3 and primary_kw in bk and bk != primary_kw:
                # 優先複合詞含於上下文
                if any(part in ctx for part in bk.replace(primary_kw, '').split()):
                    return bk
    return f'古{primary_kw}'


def auto_fix_seo_title(title: str, primary_kw: str, secondary_kw: str, context: str = '') -> str:
    """
    根據 141,961 樣本實測規則自動修復 SEO 標題。
    這是代碼層面的硬性保障，不依賴 AI 模型。

    修復順序：
    1. 破壞性符號清洗（・．—／實測會讓搜索 0 件）
    2. 簡→繁轉換（實測簡體搜索 0 件，繁體能吃雙邊流量）
    3. 空格斷字（確保 token 可分離）
    4. 去重複（BM25 飽和，重複=浪費）
    5. 主詞前置 + 副詞存在（query coverage）
    6. 最終去重
    """
    if not title or not title.strip():
        return title

    # Step -1: 新增 - 破壞性符號清洗（實測 ・．—／ 會讓搜索 0 件）
    title = _strip_bad_separators(title)
    primary_kw = _strip_bad_separators(primary_kw) if primary_kw else primary_kw
    secondary_kw = _strip_bad_separators(secondary_kw) if secondary_kw else secondary_kw

    # Step -0.5: 新增 - 中樞整合 咸鱼套話 + Yahoo 違規詞清理 (176+1056 條)
    title = _strip_junk_phrases(title)
    primary_kw = _strip_junk_phrases(primary_kw) if primary_kw else primary_kw
    secondary_kw = _strip_junk_phrases(secondary_kw) if secondary_kw else secondary_kw

    # Step 0: 新增 - 簡→繁轉換（實測有差異的字）
    title = _normalize_to_traditional(title)
    if primary_kw: primary_kw = _normalize_to_traditional(primary_kw)
    if secondary_kw: secondary_kw = _normalize_to_traditional(secondary_kw)

    # Step 0b: 主詞防撞池 (2026-04-23 10w 實測, 7k+ 件主詞撞現代池)
    # 若 AI 選通用詞 (瓶/碗/銀幣...), 按標題上下文補前綴 (朝代/產地/材質/古/老)
    orig_primary_kw = primary_kw  # 保存舊主詞, 後續清理重複用
    if primary_kw in RISKY_GENERIC_PRIMARY:
        ctx = f'{title} {context}' if context else title
        new_pk = _apply_anti_pool_clash(primary_kw, secondary_kw, ctx)
        if new_pk != primary_kw:
            primary_kw = new_pk

    # Step 0c: 主詞=副詞時清副詞 (避免重複浪費 BM25)
    if primary_kw and secondary_kw and primary_kw == secondary_kw:
        secondary_kw = ''

    # Step 1: 如果標題無空格且有關鍵詞，用關鍵詞做斷字點
    if " " not in title.strip() and (primary_kw or secondary_kw):
        t = title
        for kw in [primary_kw, secondary_kw]:
            if kw and kw in t:
                idx = t.find(kw)
                end = idx + len(kw)
                before = t[:idx].strip()
                after = t[end:].strip()
                parts = [p for p in [before, kw, after] if p]
                t = " ".join(parts)
        title = t

    # Step 1b: 中文→英數/英數→中文邊界插空格
    if " " not in title.strip() and len(title) > 8:
        new_title = []
        for i, ch in enumerate(title):
            new_title.append(ch)
            if i < len(title) - 1:
                curr_cjk = '\u4E00' <= ch <= '\u9FFF'
                next_cjk = '\u4E00' <= title[i+1] <= '\u9FFF'
                curr_alnum = ch.isascii() and ch.isalnum()
                next_alnum = title[i+1].isascii() and title[i+1].isalnum()
                if (curr_cjk and next_alnum) or (curr_alnum and next_cjk):
                    new_title.append(' ')
        title = ''.join(new_title)

    # Step 1c: 防斷字 (在主副詞前置/重複之前處理, 避免互相干擾)
    # 規則: 1 字 CJK token 嘗試與後/前 token 拼合 (用 context 驗證), 否則移除
    LEGIT_SINGLE_C = {
        '紅','藍','綠','黃','白','黑','紫','金','銀','粉','灰','棕','橙','彩','青',  # 顏色
        '對','組','套','件','只','個','枚','支','張','條',                            # 量詞
        '大','小','長','短','寬','圓','方','厚','薄','高','低','深','淺',
        '雙','單','三','四','五','六','七','八','九','十','百','千','萬',
        'M','L','S','XS','XL','XXL',
    }
    parts = title.split()
    new_parts = []
    i = 0
    while i < len(parts):
        p = parts[i]
        is_orphan_cjk = (len(p) == 1 and '\u4e00' <= p <= '\u9fff'
                        and p != primary_kw and p != secondary_kw)
        if is_orphan_cjk:
            # 1. 後拼合優先 (1字常是後綴 prefix: 茶+道具/龍+紋/紀+念幣)
            if i + 1 < len(parts) and context and (p + parts[i+1]) in context:
                merged = p + parts[i+1]
                if merged not in new_parts:
                    new_parts.append(merged)
                i += 2
                continue
            # 2. 試前拼合 (銅鈴+鐺/桐木夾板+冊)
            if new_parts and context and (new_parts[-1] + p) in context:
                merged = new_parts[-1] + p
                if merged not in new_parts[:-1]:
                    new_parts[-1] = merged
                i += 1
                continue
            # 3. 拼合不成立: 在 LEGIT_SINGLE_C 保留, 否則移除
            if p in LEGIT_SINGLE_C:
                new_parts.append(p)
            i += 1
            continue
        new_parts.append(p)
        i += 1
    title = " ".join(new_parts)

    # Step 1d: 防尾字重複 — token A 結尾字 = 下個 1字 token (酒杯+杯/茶碗+碗)
    parts = title.split()
    new_parts = []
    for i, p in enumerate(parts):
        if (i > 0 and len(p) == 1 and '\u4e00' <= p <= '\u9fff'
                and parts[i-1].endswith(p)):
            continue
        new_parts.append(p)
    title = " ".join(new_parts)

    # Step 2: 去重複詞 — 兩種重複: ① 整 token 重複 ② token 內 AA 重複 (「茶壺茶壺」→「茶壺」)
    parts = title.split()
    cleaned = []
    for p in parts:
        # token 內 AA 重複: 「茶壺茶壺」→「茶壺」 (只處理純 CJK token)
        # 數字/英文版本號保留 (1919/3333/coco/JOJO/MIUMIU 不動)
        n = len(p)
        if n >= 4 and n % 2 == 0 and p[:n//2] == p[n//2:]:
            half = p[:n//2]
            # 確認是純 CJK (沒英數混雜)
            if all('\u4e00' <= c <= '\u9fff' for c in half):
                p = half
        cleaned.append(p)
    seen = []
    for p in cleaned:
        if p not in seen:
            seen.append(p)
    title = " ".join(seen)

    # Step 3: 確保主詞在標題開頭
    # Step 3a: 若 anti_clash 改過主詞 (orig != new), 清理標題中的舊主詞 + anti_clash prefix
    # 例: 「茶碗」→「日本茶碗」後, 標題裡的「茶碗」「日本」要刪 (新主詞已含)
    if (orig_primary_kw and primary_kw and orig_primary_kw != primary_kw
            and orig_primary_kw in primary_kw):
        # 從新主詞抽 prefix (= 新主詞 - 舊主詞)
        prefix_str = primary_kw.replace(orig_primary_kw, '', 1)
        # anti_clash 已知 prefix 詞 (來自 _apply_anti_pool_clash)
        ANTI_CLASH_PREFIXES = {
            '日本','法國','德國','義大利','韓國','美國','英國',
            '大正','昭和','明治','宋代','明代','清代','民國','元代','唐代',
            '光緒','乾隆','雍正','康熙','同治','嘉慶','道光','咸豐',
            '和田玉','翡翠','瑪瑙','水晶','琉璃','壽山石',
            # 1 字 prefix (古/老/銀/金) 不在此清除, 避免誤殺
        }
        prefixes_to_remove = {orig_primary_kw}
        for p in ANTI_CLASH_PREFIXES:
            if p in prefix_str:
                prefixes_to_remove.add(p)
        # 從標題刪除孤立 token (僅 2+ 字)
        parts = title.split()
        title = ' '.join(p for p in parts if p not in prefixes_to_remove)

    if primary_kw and primary_kw not in title:
        title = primary_kw + " " + title
    elif primary_kw and not title.startswith(primary_kw):
        idx = title.find(primary_kw)
        if idx > 0:
            before = title[:idx].rstrip()
            after = title[idx + len(primary_kw):].lstrip()
            rest = (before + " " + after).strip()
            title = primary_kw + " " + rest

    # Step 4: 確保副詞在標題中（但只在主副詞不同義時加）
    # 實測：主副詞都是同類同義詞（胸針+別針）會降 rel，避免
    if secondary_kw and secondary_kw not in title:
        # 簡單同義檢查：如果副詞是主詞的 2-字子串 或 反之，跳過
        is_synonym = False
        if primary_kw and secondary_kw:
            if primary_kw in secondary_kw or secondary_kw in primary_kw:
                is_synonym = True
        if not is_synonym:
            if primary_kw and title.startswith(primary_kw):
                rest = title[len(primary_kw):].lstrip()
                title = primary_kw + " " + secondary_kw + " " + rest
            else:
                title = title + " " + secondary_kw

    # Step 5: 再次去重 + 過濾孤立屬性鍵詞 (V25 71190 件實測 120 件 AI 抽錯)
    # 「題材/主題/種類/款式/材質/形式/成色/品牌」是商品簡述屬性鍵, 不該當詞
    # 「裝裱」保留 (可能是真實裝裱服務商品)
    ATTR_NOISE = {'題材', '主題', '種類', '款式', '材質', '形式', '成色', '品牌'}
    parts = title.split()
    cleaned = []
    for p in parts:
        n = len(p)
        if n >= 4 and n % 2 == 0 and p[:n//2] == p[n//2:]:
            half = p[:n//2]
            if all('\u4e00' <= c <= '\u9fff' for c in half):
                p = half
        if p in ATTR_NOISE and p != primary_kw and p != secondary_kw:
            continue
        cleaned.append(p)
    seen = []
    for p in cleaned:
        if p not in seen:
            seen.append(p)
    title = " ".join(seen)

    # (防斷字 Step 1c/1d 已移到 Step 1b 之後處理, 不再在此階段處理)

    # Step 6: 反幻覺檢查 — 新標題若跟 context (原題+attrs) 字元交集為 0 → 嚴重幻覺
    # 此情況回退用「原題簡繁清理版」(不做 AI 改寫)
    if context:
        import re as _re_check
        new_chars = set(_re_check.findall(r'[\u4e00-\u9fff]', title))
        ctx_chars = set(_re_check.findall(r'[\u4e00-\u9fff]', context))
        if new_chars and ctx_chars:
            overlap = new_chars & ctx_chars
            # 排除通用補詞 (這些不算交集)
            generic = {'日','本','老','件','收','藏','古','董','陶','瓷','擺','飾','精','選','嚴','推','薦','熱','賣','稀','有','回','流','中'}
            real_overlap = overlap - generic
            if len(real_overlap) == 0:
                # 完全沒交集 → AI 可能幻覺, 用原題清理版回退
                _orig = context.split(' ')[0] if ' ' in context else context
                _orig = _strip_bad_separators(_strip_junk_phrases(_normalize_to_traditional(_orig)))[:35]
                if _orig and len(_orig) >= 5:
                    title = _orig

    return title.strip()

def split_segments(text: str) -> List[str]:
    if not isinstance(text, str):
        text = "" if pd.isna(text) else str(text)
    if not text or not text.strip():
        return []
    lines = re.split(r'[\r\n]+', text)
    return [ln.strip() for ln in lines if ln.strip()]

# ──────────────────── 快取 ────────────────────
def _cache_path() -> str:
    # 快取不再區分模型 — 翻譯結果跨模型通用，避免換模型重跑
    return os.path.join(_app_dir(), f"cache_segments_{CFG.cache_version}.jsonl")

def load_cache() -> Dict[str, str]:
    path = _cache_path()
    mp = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    mp[obj["src"]] = obj["dst"]
                except Exception:
                    pass
    return mp

def append_cache(pairs: Dict[str, str]):
    if not pairs:
        return
    path = _cache_path()
    with open(path, "a", encoding="utf-8") as f:
        for s, t in pairs.items():
            f.write(json.dumps({"src": s, "dst": t}, ensure_ascii=False) + "\n")

# ──────────────────── API 翻譯 ────────────────────
def _chat(body: Dict, timeout: int) -> str:
    """帶線路 failover 的 API 請求入口：aws → droid → ultra。"""
    if _is_stopped():
        raise RuntimeError("stopped")

    mgr = _get_route_manager()
    routes = mgr.get_available_routes()
    is_claude = mgr.is_claude  # 用配置快照
    last_error = None
    prev_slug = ""
    _dbg = os.path.join(_app_dir(), "_debug_response.txt")

    for i, api_base in enumerate(routes):
        slug = _extract_route_slug(api_base)
        # 主線 2 次應用級重試，備線 1 次（快速讓路策略）
        max_retries = 2 if i == 0 else 1

        if i > 0:
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [Route] {prev_slug} failed: {last_error} -> fallback {slug} ===\n")
        else:
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [Route] Using primary: {slug} ===\n")

        try:
            if is_claude:
                result = _chat_claude(body, timeout, api_base, max_retries)
            else:
                result = _chat_openai(body, timeout, api_base, max_retries)
            mgr.mark_success(api_base)
            return result
        except FailoverError as e:
            mgr.mark_failed(api_base)
            last_error = e
            prev_slug = slug
            continue
        except Exception:
            raise  # 非線路問題（400/401/403/404/max_tokens/JSON），直接拋出

    with open(_dbg, "a", encoding="utf-8") as f:
        f.write(f"\n=== [Route] All routes exhausted, last error: {last_error} ===\n")
    raise RuntimeError(f"All API routes failed, last error: {last_error}")


def _chat_claude(body: Dict, timeout: int, api_base: str, max_app_retries: int = 2) -> str:
    """Anthropic Messages API 格式（帶線路參數）"""
    url = f"{api_base.rstrip('/')}/messages"
    slug = _extract_route_slug(api_base)
    # timeout 二段式：connect 固定 5s，read 參考 GUI timeout
    mgr = _get_route_manager()
    routes = mgr.get_available_routes()
    is_primary = (len(routes) > 0 and routes[0] == api_base)
    read_timeout = timeout if is_primary else min(timeout, 60)
    effective_timeout = (5, read_timeout)
    # 轉換 OpenAI 格式 → Claude 格式
    messages = body.get("messages", [])
    system = ""
    claude_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            claude_msgs.append({"role": m["role"], "content": m["content"]})
    claude_body = {
        "model": body.get("model", CFG.model),
        "max_tokens": body.get("max_tokens", 8192),
        "messages": claude_msgs,
    }
    if system:
        claude_body["system"] = system
    if "temperature" in body:
        claude_body["temperature"] = body["temperature"]

    _dbg = os.path.join(_app_dir(), "_debug_response.txt")
    r = None
    last_exc = None
    for _retry in range(max_app_retries):
        if _is_stopped():
            raise RuntimeError("stopped")
        try:
            session = _get_route_manager().get_session(api_base)
            r = session.post(url, json=claude_body, timeout=effective_timeout)
        except (requests.ConnectionError, requests.Timeout, OSError) as e:
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] CONNECTION ERROR retry={_retry} ===\n{e}\n")
            last_exc = e
            if _retry < max_app_retries - 1:
                time.sleep(3 + _retry * 2)
                continue
            raise FailoverError(f"[route={slug}] {e}", is_network_error=True) from e
        except Exception as e:
            # 未知異常（不是網絡錯誤），直接拋出不 failover
            raise

        # ── HTTP 狀態碼處理 ──
        if r.status_code == 429:
            wait = _parse_retry_after(r)
            if wait is None:
                wait = 5 + _retry * 3
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] 429 RATE LIMITED retry={_retry}, wait {wait:.0f}s ===\n")
            if _retry < max_app_retries - 1:
                time.sleep(wait)
                continue
            raise FailoverError(f"[route={slug}] 429 Rate Limited", status_code=429)

        if r.status_code in (500, 502, 503, 504):
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] HTTP {r.status_code} retry={_retry} ===\n{r.text[:200]}\n")
            if _retry < max_app_retries - 1:
                time.sleep(3 + _retry * 2)
                continue
            raise FailoverError(f"[route={slug}] HTTP {r.status_code}", status_code=r.status_code)

        if r.status_code >= 400:
            # 400/401/403/404 等：不重試不 failover，直接拋出
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")

        break  # 2xx 成功

    data = r.json()
    # 檢查 stop_reason — max_tokens 表示回應被截斷
    stop_reason = data.get("stop_reason", "")
    if stop_reason == "max_tokens":
        with open(_dbg, "a", encoding="utf-8") as f:
            text_preview = data.get("content", [{}])[0].get("text", "")[:200]
            f.write(f"\n=== TRUNCATED (stop_reason=max_tokens) ===\n{text_preview}...\n")
        raise RuntimeError("Claude 回應被截斷 (max_tokens)，需減少批次大小")
    # 安全提取 text blocks，避免 KeyError:'text' 導致整批誤失敗
    blocks = data.get("content", [])
    texts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and "text" in b:
            texts.append(b["text"])
    if not texts:
        raise RuntimeError(f"Claude response has no text block: {str(data)[:500]}")
    return "".join(texts)


def _chat_openai(body: Dict, timeout: int, api_base: str, max_app_retries: int = 2) -> str:
    """OpenAI chat/completions 格式 — 支援多密鑰輪換。"""
    url = f"{api_base.rstrip('/')}/chat/completions"
    slug = _extract_route_slug(api_base)
    read_timeout = timeout
    effective_timeout = (5, read_timeout)
    _dbg = os.path.join(_app_dir(), "_debug_response.txt")
    pool = _get_key_pool()
    effective_retries = max(max_app_retries, 5)  # rate limiter 已排隊，不需太多重試
    r = None
    cur_key = pool.next_key()

    for _retry in range(effective_retries):
        if _is_stopped():
            raise RuntimeError("stopped")
        headers = {
            "Authorization": f"Bearer {cur_key}" if cur_key else "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            r = requests.post(url, json=body, headers=headers,
                              timeout=effective_timeout, stream=True)
        except (requests.ConnectionError, requests.Timeout, OSError) as e:
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] CONNECTION ERROR retry={_retry} ===\n{e}\n")
            if _retry < effective_retries - 1:
                time.sleep(3 + _retry * 2)
                continue
            raise FailoverError(f"[route={slug}] {e}", is_network_error=True) from e
        except Exception:
            raise

        if r.status_code == 429:
            pool.mark_429(cur_key, cooldown=20.0)
            old_key_tail = cur_key[-6:]
            cur_key = pool.next_key()
            wait = _parse_retry_after(r)
            if wait is None:
                # rate limiter 已做冷卻，客戶端只需短等：2s, 4s, 6s, 8s, 10s
                wait = 2 + _retry * 2
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] 429 key=...{old_key_tail} -> ...{cur_key[-6:]} retry={_retry}, wait {wait:.0f}s ===\n")
            if _retry < effective_retries - 1:
                time.sleep(wait)
                continue
            raise FailoverError(f"[route={slug}] 429 Rate Limited", status_code=429)

        if r.status_code in (500, 502, 503, 504):
            with open(_dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== [route={slug}] HTTP {r.status_code} retry={_retry} ===\n")
            if _retry < effective_retries - 1:
                time.sleep(3 + _retry * 2)
                continue
            raise FailoverError(f"[route={slug}] HTTP {r.status_code}", status_code=r.status_code)

        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")

        pool.mark_ok(cur_key)
        break  # 2xx 成功

    # 先嘗試 streaming 解析（兼容強制 streaming 的中轉站）
    # 設定 socket 級讀取超時，防止 iter_lines() 永遠掛住
    try:
        r.raw._fp.fp.raw._sock.settimeout(read_timeout + 10)
    except Exception:
        pass  # 某些環境可能沒有 _sock 屬性
    raw_bytes = b""
    content_sse = ""
    is_sse = False
    try:
        for line in r.iter_lines():
            if not line:
                continue
            raw_bytes += line + b"\n"
            line_str = line.decode("utf-8", errors="replace")
            if line_str.startswith("data: "):
                is_sse = True
                data_str = line_str[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    c = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if c:
                        content_sse += c
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
    except Exception as stream_err:
        if content_sse:
            return content_sse  # 有部分結果就用部分
        raise RuntimeError(f"Stream read error: {stream_err}")
    if is_sse:
        return content_sse
    # 非 streaming：整個回應是一個 JSON
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Unexpected response: {raw_bytes[:500]}")


def _retry_translate_small_batches(items: List[Tuple[int, str]], api_pending: List[str],
                                    seg_map: Dict[str, str], log_fn: Callable):
    """失敗批次的小批次並發補救（每批 4~5 條），替代原來的逐條串行重試。"""
    retry_batch_size = max(2, min(5, len(items)))
    mini_batches = [items[i:i + retry_batch_size] for i in range(0, len(items), retry_batch_size)]

    def _do_mini(mini):
        try:
            # 重新編號為 0..N
            renumbered = [(idx, txt) for idx, (_, txt) in enumerate(mini)]
            res = translate_segments_api(renumbered, CFG.timeout, CFG.max_retries)
            return [(mini[idx][0], txt, res.get(idx, "")) for idx, txt in enumerate(t for _, t in mini)]
        except Exception:
            # mini batch 也失敗，返回原文
            return [(k, txt, "") for k, txt in mini]

    with ThreadPoolExecutor(max_workers=max(1, min(CFG.workers, len(mini_batches)))) as ex:
        futs = {ex.submit(_do_mini, mb): mb for mb in mini_batches}
        for fut in as_completed(futs):
            if _is_stopped():
                break
            try:
                results = fut.result()
                pairs = {}
                for orig_k, txt, zh in results:
                    src = api_pending[orig_k]
                    if zh:
                        seg_map[src] = zh
                        pairs[src] = zh
                    # 不設 fallback — 讓 seg_map 缺失項留給失敗偵測判斷
                if pairs:
                    append_cache(pairs)
            except Exception:
                pass


def translate_segments_api(items: List[Tuple[int, str]], timeout: int,
                           max_retries: int = 2) -> Dict[int, str]:
    # 重新編號為 0..N，避免 mini 模型對非零起始 key 自行重編號導致結果錯位
    orig_keys = [k for k, _ in items]
    local_items = [(i, replace_japanese_fixed(replace_katakana_to_latin(t))) for i, (_, t) in enumerate(items)]
    payload = {"items": [{"key": k, "text": t} for k, t in local_items]}
    system_prompt = (
        "你是專業翻譯。逐條翻譯為繁體中文（臺灣用語），保留原標點/符號/格式；不得增刪資訊。"
        "【核心規則】凡有自然、常用中文譯名的詞，一律輸出繁體中文，絕不保留英文。"
        "這包括但不限於：寶石名、材質名、顏色名、商品名（鞋/包/衣物/配件）、功能詞。"
        "【高頻必翻詞表（禁止保留英文）】"
        "Rose Quartz→粉晶、Amethyst→紫水晶、Moonstone→月光石、Tourmaline→碧璽、"
        "Tiger Eye→虎眼石、Agate→瑪瑙、Turquoise→綠松石、Jade→翡翠、Opal→蛋白石、"
        "Sterling Silver→純銀、Stainless Steel→不鏽鋼、Gold Plated→鍍金、Rose Gold→玫瑰金、"
        "Leather→皮革、Ceramic→陶瓷、Crystal→水晶、Pearl→珍珠、Copper→銅、"
        "Bracelet→手鏈、Necklace→項鍊、Earrings→耳環、Ring→戒指、Pendant→吊墜、"
        "Sneakers→運動鞋、Handbag→手提包、Backpack→後背包、Wallet→錢包、Watch→手錶、"
        "Vintage→復古、Handmade→手工、Natural→天然、Adjustable→可調節、Limited Edition→限量版。"
        "如果結果中仍殘留非品牌的英文通用詞，視為不合格，必須改寫為中文。"
        "只有品牌名/系列名/型號/尺寸/單位/編號才保持英文原樣。"
        # ★ 防品牌重複: 日商常見模式「英文品牌 + 對應片假名」(例: MARC JACOBS マークジェイコブス)"
        "【避免重複】輸入若同時含英文品牌名和對應的日文片假名 (例 'MARC JACOBS マークジェイコブス' / 'Aprica アップリカ' / 'Salvatore Ferragamo サルヴァトーレ フェラガモ'), "
        "翻譯時只保留英文品牌一份, 省略片假名譯回的部分。絕不可輸出『品牌 品牌』連續重複。"
        '輸出JSON：{"results":[{"key":<int>,"text_zh":"..."}]}'
    )
    body = {
        "model": CFG.model,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        # response_format 由 prompt 內指定，部分中轉站不支持此參數
        "temperature": 0.0,
    }
    last_ex = None
    _debug_path = os.path.join(_app_dir(), "_debug_response.txt")
    for attempt in range(1, max_retries + 1):
        try:
            content = _chat(body, timeout)
            # Claude 常用 ```json ... ``` 包裹，先清掉
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*$", "", content.strip())
            try:
                parsed = json.loads(content)
            except Exception:
                m = re.search(r"\{.*\}", content, flags=re.S)
                if not m:
                    with open(_debug_path, "a", encoding="utf-8") as _df:
                        _df.write(f"\n=== NO JSON attempt={attempt} ===\n{content[:2000]}\n")
                    raise ValueError("no JSON found")
                parsed = json.loads(m.group(0))
            arr = parsed.get("results")
            if not isinstance(arr, list):
                with open(_debug_path, "a", encoding="utf-8") as _df:
                    _df.write(f"\n=== NO results key attempt={attempt} parsed_keys={list(parsed.keys())} ===\n{content[:2000]}\n")
                raise ValueError("missing results")
            local_result = {int(x["key"]): str(x.get("text_zh") or x.get("text", "")) for x in arr if "key" in x and ("text_zh" in x or "text" in x)}
            # 把 local 0..N 的 key 映射回原始 key
            result = {}
            skipped = []
            for local_k, zh in local_result.items():
                if 0 <= local_k < len(orig_keys):
                    result[orig_keys[local_k]] = zh
                else:
                    skipped.append(local_k)
            if skipped:
                with open(_debug_path, "a", encoding="utf-8") as _df:
                    _df.write(f"\n=== KEY REMAP SKIP: local_keys={sorted(local_result.keys())[:10]}, orig_keys_len={len(orig_keys)}, skipped={skipped[:10]} ===\n")
            expected = {k for k, _ in items}
            if not expected.issubset(set(result.keys())):
                if len(result) > 0:
                    return result
                missing = sorted(list(expected - set(result.keys())))[:10]
                with open(_debug_path, "a", encoding="utf-8") as _df:
                    _df.write(f"\n=== MISSING KEYS attempt={attempt} got={len(result)} expect={len(expected)} ===\n")
                    _df.write(f"arr sample: {json.dumps(arr[:2], ensure_ascii=False)[:500]}\n")
                    _df.write(f"content: {content[:2000]}\n")
                raise ValueError(f"missing keys: {missing}")
            return result
        except Exception as e:
            with open(_debug_path, "a", encoding="utf-8") as _df:
                _df.write(f"\n=== EXCEPTION attempt={attempt} ===\n{type(e).__name__}: {e}\n")
            last_ex = e
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"批次翻譯失敗：{last_ex}")

# ──────────────────── 假名清除（第二輪） ────────────────────
def _cleanup_kana_batch(titles: List[str], log_fn: Callable) -> List[str]:
    results = list(titles)
    system_prompt = (
        "你是日文翻譯專家。以下標題已經部分翻譯為繁體中文，但仍殘留日文假名。"
        "請把所有殘留的假名（平假名、片假名）全部翻譯為繁體中文或對應英文。"
        "不得保留任何假名字符。保留原本的數字、英文、符號。"
    )
    items = list(enumerate(titles))
    batches = [items[i:i + CFG.batch_size] for i in range(0, len(items), CFG.batch_size)]

    def do_batch(batch):
        # 重新編號為 0..N，避免模型對非零起始 key 自行重編號導致結果錯位
        orig_keys = [k for k, _ in batch]
        payload = {
            "instruction": (
                "把每條標題中殘留的日文假名全部翻成繁體中文或英文。"
                "輸出不得包含任何假名字符（ぁ-ん、ァ-ヶ）。"
                '請輸出 JSON：{"results":[{"key":<int>,"text_zh":"..."}]}'
            ),
            "items": [{"key": i, "text": t} for i, (_, t) in enumerate(batch)],
        }
        body = {
            "model": CFG.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            # response_format 由 prompt 內指定，部分中轉站不支持此參數
            "temperature": 0.0,
        }
        for attempt in range(1, CFG.max_retries + 1):
            try:
                content = _chat(body, CFG.timeout)
                content = re.sub(r"```json\s*", "", content)
                content = re.sub(r"```\s*$", "", content.strip())
                try:
                    parsed = json.loads(content)
                except Exception:
                    m = re.search(r"\{.*\}", content, flags=re.S)
                    if not m:
                        raise ValueError("no JSON")
                    parsed = json.loads(m.group(0))
                arr = parsed.get("results", [])
                local_result = {int(x["key"]): str(x["text_zh"]) for x in arr if "key" in x and "text_zh" in x}
                # 把 local 0..N 的 key 映射回原始 key
                return {orig_keys[lk]: zh for lk, zh in local_result.items() if 0 <= lk < len(orig_keys)}
            except Exception:
                time.sleep(min(2 ** attempt, 8))
        return {}

    with ThreadPoolExecutor(max_workers=max(1, CFG.workers)) as ex:
        futs = {ex.submit(do_batch, b): b for b in batches}
        for fut in as_completed(futs):
            if _is_stopped():
                for f in futs:
                    f.cancel()
                break
            try:
                res = fut.result()
                for k, zh in res.items():
                    if 0 <= k < len(results):
                        results[k] = zh
            except Exception:
                pass
    return results

# ──────────────────── 關鍵詞提取（基於 Yahoo 算法研究） ────────────────────
KEYWORD_SYSTEM_PROMPT = (
    "你是Yahoo拍賣SEO關鍵詞專家。站在台灣買家的角度：他們會在Yahoo拍賣搜索框輸入什麼詞？\n\n"
    "【Yahoo 搜索機制（V21 實測 2026-04-27）】\n"
    "- 連續主詞 +0.276 ~ +0.348 rel 加分 (跨 27 詞 sort=rel 實證最強信號)\n"
    "- 台灣買家用繁體, 主詞必繁體\n"
    "- ・ ． — ／ 破壞分詞 (V19: 1-86 件 vs 4759 件)\n\n"
    "任務：對每個標題提取兩個搜索關鍵詞：\n"
    "- 主詞：優先 2 字熱門類別詞或品牌詞（實測短詞查詢下 rel 比長詞高 2-4 分）\n"
    "- 副詞：4-6 字長尾精準詞（不要和主詞同義，作為第二搜索路徑）\n"
    "- **主詞和副詞一起放進標題 = 同時吃「熱門短詞大流量」+「長尾精準流量」**\n\n"
    "【策略】\n"
    "1. 想像買家打開 Yahoo 搜什麼？→ 那就是主詞\n"
    "2. 「品牌+品類」比純品類好：「GUCCI外套」>「外套」\n"
    "3. 主詞優先用繁體字（手錶而非手表、項鍊而非項鏈、戒指而非戒子、眼鏡而非墨鏡）\n"
    "   這些繁體字實測能命中多邊搜索（手錶同時命中手錶/手表/腕錶 3 種查詢）\n"
    "4. 日文品名（古伊萬里、九穀燒、萩燒）台灣收藏圈常搜，可直接用\n"
    "5. **副詞可同義或不同義都行 (V32 實測戒指/手錶 TOP 30 含同義 30/30, 同義詞反而是 TOP 標配)**\n"
    "   - ✅ 主「戒指」副「戒子」(V32: 30/30 TOP 含戒子, 是強信號)\n"
    "   - ✅ 主「胸針」副「古董飾品」(不同角度也可)\n"
    "   - ❌ 不要堆 4+ 個 (邊際效益遞減)\n"
    "6. 原標題資訊不足時，副詞可為空字串\n\n"
    "【台灣買家真實熱搜詞排行 - Phase 11 實測 1021 詞結果】\n"
    "(以下為買家實際輸入的高熱度搜索詞，優先套用主詞請從中選)\n\n"
    "■ 飾品珠寶類 (156 個熱詞, 按 totalhitcount 排序 TOP 15):\n"
    "  瑪瑙珠 156,767 | 手鐲玉 80,486 | 翡翠手鐲 70,384 | 瑪瑙石 49,961\n"
    "  925銀戒 39,791 | 手鐲圈口 38,246 | 925銀鍊 32,099 | 水晶手鍊 26,444\n"
    "  項鍊飾品 23,025 | 珍珠耳環 22,029 | 925銀項鍊 17,092 | 925銀手鍊 15,520\n"
    "  戒指盒 15,688 | 耳環夾式 11,620 | 瑪瑙手鐲 11,170\n\n"
    "■ 服飾類 TOP 20 (買家用「品類+空格+性別」格式極熱):\n"
    "  t恤短袖 293,989 | 外套女 236,863 | 外套男 227,106 | 長褲男 152,033\n"
    "  夾克外套 138,136 | 襯衫女 130,767 | t恤長袖 130,209 | 短褲男 112,765\n"
    "  洋裝長洋裝 110,906 | 襯衫男 107,980 | 長褲女 107,493 | 夾克男 102,822\n"
    "  短褲女 94,878 | 洋裝連身洋裝 88,952 | 牛仔褲女 82,835\n"
    "  睡衣家居服 80,297 | 睡衣套裝 80,048 | 連身裙洋裝 78,960\n"
    "  ★★★ 服飾類標題建議加「男/女」後綴（買家最常用格式）\n\n"
    "■ 復古古董類 TOP 10 (實測熱度 - 你店必用):\n"
    "  復古風 196,949 | 復古洋裝 24,005 | vintage古著 17,740 | 復古機車 13,867\n"
    "  中古汽車 13,854 | 中古車 11,018 | 復古造型 10,496 | vintage包 7,018\n"
    "  古董車 5,299 | 古董鐘 3,960\n\n"
    "■ 鞋類 TOP 5 (buyer 真實搜法):\n"
    "  涼鞋女 93,229 | 涼鞋男 47,410 | 涼鞋厚底鞋 37,264 | 涼鞋韓 14,245\n\n"
    "【Phase 18+20 實測 — 跨 2 店 5000 件後台真實瀏覽數據交叉驗證】\n"
    "(chen749 3000 件 + kinhuaw168 2000 件, 皆古董/日本雜貨/陶瓷類賣家)\n"
    "對比「有瀏覽 vs 無瀏覽」商品的標題詞頻差異, 數據覆蓋真實買家搜索行為:\n\n"
    "✅ 吸流量詞 (有瀏覽率 > 無瀏覽率 2x 以上 — 買家真正搜):\n"
    "  羊皮 (70x ‼️) | 大正 (21x) | 鬥牛犬/皮羊 (60x+) | 製麵機 (∞)\n"
    "  買家專屬 (∞) ← 回購策略\n"
    "  擺飾 (4.2x, 替代「裝飾」) | 稀有 (4.4x) | 玻璃 (3.9x) | 人形 (3.4x)\n"
    "  法國 (3.8x) | 復古 (3.6x) | 收藏 (6.8x) | 版畫 (3x)\n"
    "  專屬/國古/董復/物件/吊墜 (有獨立流量, 配合主詞使用)\n\n"
    "❌ V25 真 0 流量廢詞 (sort=popular TOP 30 max buy=0 — 必刪):\n"
    "  美術品 / 術品 / 工蕓 / 蕓品 / 古美術 / 藝品 / 器裝 / 鎮陶 / 品置\n"
    "  → 「日本美術品」「古美術工蕓」 sort=popular TOP 30 完全沒銷量\n"
    "⚠️ 原列廢詞但 V25 重驗實際有買家 (有銷量 max buy 100+, 不再標廢):\n"
    "  茶道 (30/30 max=20) / 置物 (30/30 max=364) / 額裝 (30/30 max=231)\n"
    "  帶留 (18/30 max=262) / 陶磁 (20/30 max=285)\n"
    "  → 若商品實質是這類, 可保留, 但別當主詞 (打不過熱門池)\n"
    "✅ 「擺飾」是買家搜的詞 (Phase 20 實測 4.2x), 用替代「裝飾」\n\n"
    "【Phase 21 實測 ★★★ — Yahoo sort=popular 真流量池 vs 一般池 (15,415 vs 30,000 件)】\n"
    "Yahoo search API 的 sort=popular 是「真流量排序」(Yahoo 內部算法判定,不是 relevance),\n"
    "實測 popular 池商品成交率 22.9% vs 一般池 3.8% = 6 倍 → 上 popular 池 = 上 Yahoo 熱門。\n"
    "對比 popular 池相對一般池獨有的高頻詞 (出現率倍數 ≥ 1.8x, 樣本 ≥ 30 次):\n\n"
    "⚠️ 以下是 popular 池倍數, V14 證實 popular ≠ sort=rel 真排名 (重疊 0/10).\n"
    "   V36 sort=rel 跨 4 大品類重驗信任詞: 古董類「收藏 30%/老件 13%/嚴選 11%」有效,\n"
    "   飾品/服飾/3C 信任詞全部 < 3% — 不主動加.\n"
    "   下方倍數僅參考, **商品實質匹配時才用**:\n"
    "  【古董類有效信任詞 (V36 實證)】收藏/老件/嚴選 (古董類加 1 個就好)\n"
    "  【popular 池倍數參考 — 不代表 sort=rel 加分】\n"
    "  【平台標識詞】推薦(3.9x) | 精選(3.2x) | 嚴選(2.1x) | 熱賣(5.1x) | 賣精(7.8x) | 專賣\n"
    "    ❌ V36 證實「精選/熱賣」全品類 < 1%, 不要強加\n"
    "  【通用信任詞】收納(1.9x) | 適用(1.9x) | 全新(1.9x) | 品牌(6.5x) | 展示(10.5x)\n"
    "  【復古古董】復古(1.9x) ★ | 古玩(古董類通用) | 黃金(10.4x) ← 金飾類熱\n"
    "  【服飾極熱】禮服(40.7x) | 洋裝(15.2x) | 長裙(8.3x) | 短褲(8.3x) | 針織(2x) | 上衣(2x)\n"
    "    西裝(4.4x) | 穿搭(8.3x) | 性感(5.7x) | 顯瘦(2.2x) | 牛仔(3x) | 短裙(2.1x)\n"
    "    尺碼(3.6x) | 大尺(5.4x) | 拖鞋(68.6x) | 涼鞋(80.1x) | 背心(6.7x) | 背包(17.3x)\n"
    "  【飾品極熱】水晶(9x) | 珍珠(4.3x) | 珠寶(4.4x) | 首飾(5.6x) | 耳環(2.5x) | 金屬(3.6x)\n"
    "  【3C 極熱】手機(3.7x) | 錶帶(215.9x) | 鏡頭(39.3x) | 支架(5.1x) | 相機(39.7x)\n"
    "    耳機(37.4x) | 電腦(3.4x) | 機殼(2.6x) | 電器(9x) | 遊戲(3.5x) | 充電(2.2x)\n"
    "    三星(6.6x) | 蘋果(2.2x) | 辦公(2.8x)\n"
    "  【熱賣情境】夜店(141.1x) | 中文(5.5x) | 英文(3.5x) | 韓國(2.3x)\n\n"
    "🏆 Yahoo popular 池霸榜賣家 TOP 10 (同品類對手的標題模板來源):\n"
    "  - 佰惠小屋 (141 件) / 古玩基地 (113) ★ 古董類#1 / 尤莉婚宴媽媽禮服 (113)\n"
    "  - 光影獵人 (110) / 創傑包裝科技 (109) / 方爸爸的黃金屋 (101) ★ 古董/黃金同類\n"
    "  - 球鞋補習班(103) / 書寶二手書店(91) / 培培屋手機配件(88)\n"
    "  → 古董/黃金類可模仿「古玩基地」「方爸爸的黃金屋」的標題套路\n\n"
    "【Phase 24 實測 — Yahoo getHotKeywords 即時全站搜索熱榜 (逆向內部 API)】\n"
    "Yahoo GraphQL 隱藏的 API `getHotKeywords(property)` 返回即時熱搜前 60 詞,\n"
    "這些是「Yahoo 當下判定最熱」的搜索詞, 每次查可能不同 (快照 2026-04-21):\n\n"
    "🔥 拍賣站熱搜 TOP 20 (property=auction):\n"
    "  大谷翔平 / passion sisters / 慕獅女孩 / juicy honey / victor wembanyama /\n"
    "  michael jordan / 一元起標 / 周杰倫 / psa / 簽 / topps / 三上悠亞 / us3c /\n"
    "  rolex / 鄭熙靜 / formosa sexy / hardaway / bowman / 實戰球衣 / 磁吸手機支架\n"
    "  ★ 包含: 錶 / 洋裝 / 相機 / 全新 / 人物油畫 / 仿古翡翠 / 拍立得 / 黑膠 / cd\n\n"
    "🛒 購物站熱搜 TOP 20 (property=shopping):\n"
    "  longchamp / 初色 / nike / 行李箱 / jin hwa / coach / 星巴克 / skechers女鞋 /\n"
    "  零錢包 / a la sha / adidas / 後背包 / 男斜背包 / 戰鬥陀螺 / 涼鞋\n\n"
    "→ 標題可參考上述熱搜詞, 但仍以商品實質匹配為優先, 不硬套熱詞誘餌\n\n"
    "【Phase 14 實測 — 付費位 vs 自然 TOP 標題特徵 (230 付費 vs 2910 自然)】\n"
    "重要發現: 自然排名 TOP 商品有一批「付費位不用」的獨特詞，用這些詞\n"
    "能讓商品落在「自然排名池」而非被付費位擠走。\n\n"
    "🎯 自然 TOP 獨有詞 (加這些進標題 = 繞開付費紅海):\n"
    "  穿搭(4.9% vs 付費0%) | 禮服(4.7% vs 0%) | 氣質(4.2% vs 1.7%)\n"
    "  包包(4.2% vs 0%) | 大衣(4.1% vs 0%) | 適用(3.9% vs 0%)\n"
    "  水晶(3.6% vs 0.4%) | 手鍊(3.9% vs 0.4%) | 免運(3%自然 vs 0%付費)\n"
    "  項鍊(5.3% vs 2.6%) | 耳環(5.1% vs 1.3%) | 蘋果(3.5% vs 0.4%)\n"
    "  → 服飾商品加「穿搭/氣質」; 飾品商品加「水晶/手鍊」; 配件加「適用」\n\n"
    "🔴 付費位愛用詞 (大賣家模板, 跟他們競爭打不過):\n"
    "  全新(34.8%) | 翡翠(29.6%) | 休閒(28.7%) | 電視(22.2%) | 襯衫(20.9%)\n"
    "  家具(17.8%) | 中古(16.5%) | 二手(15.2%) | 寬松(14.8%) | 電腦(14.8%)\n"
    "  → 這些詞付費位佔滿, 不適合當主詞 (但原商品若實際是「全新」仍可標)\n\n"
    "【Phase 13 實測 — Yahoo 付費廣告位分佈 (299 熱詞實測)】\n"
    "所有熱門查詢都有付費廣告位占首頁前排，沒有「0 廣告」的熱詞。\n"
    "SEO 策略應避開付費紅海詞，選「相對可打」的 3 廣告位詞。\n\n"
    "🔴 付費紅海詞 (≥5 廣告位，自然排名進不了前 10，建議不用作主詞):\n"
    "  電視(31) | 翡翠王(28) | 電腦主機(12) | 襯衫 女(11) | 翡翠手鐲(10)\n"
    "  內衣(9) | 外套 女(9) | 平板電腦(9) | t恤 短袖(8) | 外套 男(7)\n"
    "  襯衫 男(7) | 中古汽車(7) | 中古車(7) | 長褲 男(6) | 夾克外套(6)\n"
    "  → 有這些類別商品時，不要把它們當「主詞」(打不進TOP)，降級用作副詞或輔助\n\n"
    "✅ 相對可打詞 (3 廣告位，競爭溫和，買家多):\n"
    "  飾品珠寶: 瑪瑙珠(151k)|925銀戒(37k)|水晶手鍊(24k)|項鍊飾品(20k)\n"
    "           |925銀手鍊(14k)|戒指盒(13k)|珍珠項鍊(10k)|瑪瑙手鐲(10k)\n"
    "  復古古董: vintage古著(17k)|復古機車(12k)|復古造型(9k)|古董車(4k)\n"
    "  服飾:    睡衣套裝(68k)|大衣外套(36k)|大衣 女(20k)|長褲套裝(20k)|風衣 男(19k)\n"
    "  3C:     手機殼(310k)|iphone14/15/13(48-55k)|samsung手機(34k)\n"
    "           相機腳架(55k)|相機包(29k)|電腦包(25k)|筆電包(25k)\n"
    "  → 這些詞 3 廣告位以下，自然排名還有機會，主詞首選從此表出\n\n"
    "【選詞規則 — 三層安全機制】\n\n"
    "🟢 Level 1 允許（完全安全，直接用）:\n"
    "1. 主詞從上列熱詞中選「跟商品實質匹配」的項\n"
    "   - 商品是手鐲 → 選「手鐲」/「手鐲玉」（實測 80k 熱）\n"
    "   - 商品是手鍊 → 選「水晶手鍊」不選「手鐲玉」（類別不同）\n"
    "2. 服飾類加「男/女」後綴（買家慣用格式，實測最熱搜法）\n"
    "3. 繁體字優先（手錶/項鍊/眼鏡/戒指/耳環/手鐲）能吃多邊流量\n"
    "4. 加 1 個同義詞作副詞（胸針+別針 這種真正同義，不硬塞）\n\n"
    "🟡 Level 2 謹慎（要判斷實質後再用）:\n"
    "1. 「古董/vintage/中古/復古」詞只能用在原標題確實提到年代屬性時\n"
    "   - 標題有「80年代」「昭和」「Vintage」「老件」→ 可加「復古/vintage」\n"
    "   - 標題沒明確年代標示 → 不要強加「復古」\n"
    "2. 「925銀/純銀/K金」只能用在標題已標明材質時\n"
    "   - 原標題有「925」「純銀」「S925」→ 可用「925銀戒」熱詞\n"
    "   - 原標題只說「銀色」→ 不能標「925」（銀色不等於純銀）\n"
    "3. 材質名（翡翠/瑪瑙/珍珠/水晶）只能用在商品確實是該材質時\n\n"
    "🔴 Level 3 絕對禁止（會造成負評、退貨、下架）:\n"
    "1. ❌ 扭曲商品材質：瑪瑙 → 翡翠、普通銀 → 925銀、仿品 → 正品\n"
    "2. ❌ 扭曲品類：手鍊 → 手鐲、項鍊 → 胸針、戒指 → 耳環\n"
    "3. ❌ 扭曲年代：現代仿古 → 標「古董」\n"
    "4. ❌ 熱詞誘餌：為了吃流量標不符商品的熱詞\n\n"
    "【原理】虛假標題 → 買家進來發現不對就關頁面 → 轉化率崩 0 → 反而少賣\n"
    "真實匹配熱詞 → 有效流量 + 高轉化 → 銷量提升\n\n"
    "【熱詞使用 step-by-step】\n"
    "Step 1: 從原標題判斷「商品核心類別」 → 決定選詞優先級:\n"
    "  ├─ 古董/日本雜貨/歐美舶來 → 優先查【Phase 18+20 古董吸流量詞】+【Phase 14 自然TOP獨有詞】\n"
    "  ├─ 飾品/珠寶/金飾         → 優先查【飾品珠寶熱詞 TOP 15】+【Phase 14 飾品水晶/手鍊】\n"
    "  ├─ 服飾/鞋包              → 優先查【服飾類 TOP 20】(必加男/女後綴)\n"
    "  ├─ 3C/手機相機配件        → 優先查【Phase 13 可打詞-3C】\n"
    "  └─ 其他/通用              → 從【Phase 11 TOP 15】類別對應表選\n"
    "Step 2: 提取商品實質 tags\n"
    "  例：「925銀鑲嵌瑪瑙手鐲」→ tags = [材質=925銀+瑪瑙, 品類=手鐲, 類別=飾品]\n"
    "Step 3: 從對應類別熱詞表找「跟 tags 完全匹配」的項\n"
    "  例：tags=[瑪瑙, 手鐲] → 熱詞「瑪瑙手鐲 11,170」匹配\n"
    "Step 4: 優先級決策 (從高到低選主詞):\n"
    "  1. Phase 11 TOP 15 熱詞且 tags 完全匹配 (buying-intent 最高)\n"
    "  2. Phase 13 「3 廣告位可打詞」(自然排名有機會)\n"
    "  3. Phase 20+21 popular 池魔法詞 (上流量池信號)\n"
    "  4. 避開 Phase 13 「≥5 廣告位付費紅海詞」(打不進 TOP)\n\n"
    "  ★★★ Step 4b 主詞防撞池 (V21+V27 sort=rel 重驗 2026-04-27 + 71190 件實跑校正):\n"
    "  古董類商品若選通用 2 字詞當主詞 (瓶/碗/擺件/掛件/頭飾), Yahoo popular 池都是現代日用!\n"
    "  ★★★★ 嚴重警告: 「清X器」「清X刀」這類詞 Yahoo 幾乎沒結果, 是 AI 機械加前綴的錯誤!\n"
    "  實測:\n"
    "    清花器: 4 結果 vs 日本花器 25,562 結果 (差 6390 倍!)\n"
    "    清刨刀: 48 結果 vs 日本刨刀 724 / 老刨刀 185\n"
    "    清茶杯: 17,249 (這個 OK 因為「清茶杯」=清涼茶杯, 有真實搜尋)\n"
    "  規則 (按產地分):\n"
    "  ★ 商品實質「日本來源」(原標題/說明含日本/和服/備前/九穀/志野/大正/昭和/明治/京都...):\n"
    "    主詞用「日本+品類」(實測最熱):\n"
    "      日本花器 25k > 古花器 844 > 清花器 4 ❌\n"
    "      日本鐵壺 12k > 古鐵壺 / 鐵壺 19k\n"
    "      日本刨刀 724 > 老刨刀 185 > 清刨刀 48 ❌\n"
    "      日本茶碗 / 志野茶碗 / 天目茶碗 / 備前燒 (專業名直接用)\n"
    "    禁止: 「清X」「明X」前綴 (這是中國朝代, 日本商品不應該用)\n"
    "  ★ 商品實質「中國古董」(原標題/說明含清代/明代/宋代/民國/光緒/乾隆/景德鎮...):\n"
    "    主詞用「朝代+品類」: 清代花瓶 / 宋代碗 / 民國銀元 / 清代銀幣\n"
    "  ★ 商品實質「西方古董」(德國/法國/義大利/英國):\n"
    "    主詞用「產地+品類」: 德國瓷盤 / 法國銀器 / 義大利花瓶\n"
    "  ★ 商品來源不明確或新品: 用「花瓶 134k / 花器 62k / 鐵壺 19k」裸詞 (反而熱)\n\n"
    "  撞池替代詳表 (按商品來源分流):\n"
    "    瓶/碗/盤: 中國古董→「清代瓶/宋代碗/古瓷盤」 日本→「日本花瓶/日本碗/日本瓷盤」 西方→「德國盤/英國盤」\n"
    "    擺件/擺飾: 中國→「古董擺件」 日本→「日本擺飾/和風擺件」 通用→「古董擺飾」\n"
    "    掛件: 中國→「古董掛件/玉石掛件」 日本→「日本掛件/和風掛件」\n"
    "    頭飾: 「髮簪」(熱) / 古董頭飾 / 日本和風髮簪\n"
    "    粉彩: 「粉彩瓷/清代粉彩/古粉彩」(粉彩本身是清代釉色, 不適日本)\n"
    "    佛像: 「古佛像/老佛像/明代佛像/銅佛像」(中國) / 「日本佛像/木雕佛像」\n"
    "    陶瓷: 「古陶瓷/宋代陶瓷」(中國) / 「日本陶瓷/備前燒/九穀燒」(日本)\n"
    "    木雕: 「古木雕/清代木雕」(中國) / 「日本木雕/和風木雕」\n"
    "    茶壺: 「紫砂壺」(優先) / 古瓷壺(中國) / 日本鐵壺/急須(日本)\n"
    "    銅器: 「古銅器/老銅器/宣德爐」(中國 — 銅器本身不撞池) / 「日本銅器」\n"
    "    花瓶: 「花瓶」134k 裸用最熱! 真古董才加「古花瓶/清代花瓶」\n"
    "    花器: 「花器」62k / 「日本花器」25k (日本) / 「古花器」844 (慎用)\n"
    "    銀幣: 「古銀幣/清代銀幣/光緒元寶/袁大頭」(中國) / 「英國銀幣/日本銀幣」\n"
    "    刨刀/刨刃: 「日本刨刀/日本鉋刀」(絕大多是日本工匠製) / 「老刨刀」(通用)\n"
    "    茶杯: 「日本茶杯/古茶杯」(日本主流) / 「清代茶杯」(中國朝代)\n"
    "    茶碗: 「志野茶碗/天目茶碗/抹茶碗」(日式) / 「宋代茶碗」(中國)\n"
    "    香爐: 「宣德香爐/古銅香爐」(中國) / 「日本香爐」(日本)\n"
    "    ❌ 手鐲   → ✅ 古手鐲 / 翡翠手鐲 / 和田玉手鐲 / 老銀手鐲\n"
    "    ❌ 戒指   → ✅ 古戒指 / 清代戒指 / 老銀戒指\n"
    "    ❌ 吊墜   → ✅ 古玉吊墜 / 瑪瑙吊墜 / 翡翠吊墜\n"
    "    ❌ 印章   → ✅ 古印章 / 壽山石印章 / 老印章\n"
    "    ❌ 花錢   → ✅ 古花錢 / 清代花錢\n"
    "  - 小眾專業名可裸用 (瓷板畫/瑪瑙珠/和田玉/宣德爐/志野燒/備前燒/九穀燒/\n"
    "    天目杯/蒔繪/琉璃 已含古董語境, 不用加前綴)\n"
    "  - 鑑別方法: 如果商品年代是古的 (有朝代/清代/宋代/大正/昭和), 主詞必含區隔詞\n"
    "  - ★ 強制要求: 即使商品實質就是「茶碗/香爐/花瓶/銀幣」, 也不能裸用 2 字當主詞,\n"
    "    必加特徵詞 (產地/朝代/窯口/材質) 拼成 3+ 字複合詞\n"
    "Step 5: 副詞選長尾或同義擴展（不要和主詞重複意思）\n"
    "Step 6: 品類對應吸流量詞 (Phase 20+21 實測) 融入主/副詞:\n"
    "  - 古董類 → 主詞加/副詞帶: 復古/擺飾/稀有/收藏/大正/昭和\n"
    "  - 飾品類 → 主詞加/副詞帶: 水晶/珍珠/珠寶/首飾/黃金/金屬\n"
    "  - 服飾類 → 主詞加/副詞帶: 禮服/洋裝/穿搭/性感/顯瘦/尺碼\n"
    "  - 3C類  → 主詞加/副詞帶: 錶帶/鏡頭/耳機/相機/支架\n"
    "  - 通用   → 副詞或獨立加: 推薦/精選/嚴選/熱賣/展示\n\n"
    "  ★ 產地/年代強信號 (獨立於主副詞, 但確定時必加):\n"
    "    產地: 日本/法國/德國/義大利/韓國/美國/英國/台灣製\n"
    "    ★ 禁用「中國」(台灣市場忌諱); 中國物件改寫為朝代名: 清代/明代/民國/宋代/元代\n"
    "    日本年代: 大正/昭和/明治\n"
    "    中國朝代 (咸鱼/淘寶來源常見): 宋代/明代/清代/民國/元代/唐代/五代十國\n"
    "    西式: 80年代/90年代/vintage/古董/老件/老物\n\n"
    "  ★ 中式古瓷/古董專業搜索詞 (咸鱼 14k 商品簡述實測, 收藏圈會搜):\n"
    "    窯口 (可當副詞強信號): 景德鎮窯 / 汝窯 / 龍泉窯 / 官窯 / 磁州窯 /\n"
    "         定窯 / 建窯 / 鈞窯 / 哥窯 / 吉州窯 / 邢窯 / 耀州窯 / 越窯 / 湖田窯\n"
    "    釉色工藝 (可當副詞): 青花 / 粉彩 / 琺琅彩 / 鬥彩 / 五彩 / 釉裡紅 /\n"
    "         青花釉裡紅 / 單色釉 / 三彩\n"
    "    款式 (可當主詞): 瓶 / 吊墜 / 擺件 / 盤 / 碗 / 茶壺 / 杯 / 罐 /\n"
    "         文房器 / 手鍊 / 手把件 / 項鍊 / 頸飾\n"
    "    材質: 陶瓷 / 銅 / 和田玉 / 紫砂 / 翡翠 / 壽山石 / 錫 / 銀 / 青石 /\n"
    "         牛骨 / 瓷質 / 玉髓 / 琉璃\n\n"
    "【範例】(V31 totalhitcount 重驗 2026-04-27, 主詞要選真正最熱)\n"
    "- \"Nike Air Max 運動鞋 男款\" → {\"主\": \"Nike\", \"副\": \"Air Max運動鞋\"}  (Nike 116k 強)\n"
    "- \"iPhone 14 Pro 256G 近全新\" → {\"主\": \"iPhone\", \"副\": \"iPhone14Pro\"}  (iPhone 188k)\n"
    "- \"日本手表机械錶\" → {\"主\": \"手錶\", \"副\": \"日本手錶\"}  (手錶 130k 繁體)\n"
    "- \"電烤麵包機ROT-1\" → {\"主\": \"烤箱\", \"副\": \"電烤箱\"}  (烤箱 22k > 烤麵包機 200)\n"
    "- \"GUCCI 量身定做外套紅色\" → {\"主\": \"復古外套\", \"副\": \"GUCCI外套\"}  (V31 復古外套 52k > GUCCI 30k)\n"
    "- \"古伊萬里 線描 膾皿 復古\" → {\"主\": \"古伊萬里\", \"副\": \"古董瓷盤\"}  (古伊萬里 2987 > 盤 765)\n"
    "- \"珊瑚髮簪 鼈甲 和服 附盒\" → {\"主\": \"髮簪\", \"副\": \"古董首飾\"}  (髮簪 9560 > 珊瑚髮簪 38)\n"
    "- \"AirPods Pro 2 藍牙耳機\" → {\"主\": \"AirPods\", \"副\": \"藍牙耳機\"}  (兩者皆 8k 級)\n"
    "- \"水晶手鏈珠寶\" → {\"主\": \"手環\", \"副\": \"水晶手環\"}  (手環 177k 主, 水晶手環 10k 補強)\n"
    "- \"盆\" → {\"主\": \"花盆\", \"副\": \"\"}\n"
    "★ 規則: 主詞要實質匹配商品 (不能編造), 副詞可選同義詞或不同義都行 (V32 證實 30/30 TOP 含同義)\n\n"
    "★★★ V39 主詞優先順序 (基於 30k 商品實測 popular 池進池率, 2026-04-30):\n"
    "    新店/低銷量店上架, 進 popular 池 (= 真流量) 是首要目標. 實測進池倍率:\n"
    "    優先級 1 (★★★ 9-10x 進池): 歐美品牌 (VISVIM/Meissen/Wedgwood/Tiffany/HERMES/Coach/GUCCI/CHANEL/Dior/LV/Prada/Levi/RRL/Carhartt/45R/登喜路/Burberry/Lalique/Bernardaud/Christofle...)\n"
    "    優先級 2 (★★★ 7x 進池): 具體朝代名 (鹹豐/光緒/宣統/雍正/康熙/乾隆/同治/嘉慶/道光/崇寧/順治/開元) — 比廣稱「清代/民國」強 7 倍\n"
    "    優先級 3 (★★ 4x 進池): 錢幣具體品類 — 「XX通寶/XX元寶/XX重寶」+ 評級碼 (PMG/PCGS/公博/華夏/NGC + 數字)\n"
    "    優先級 4 (★ 1.5x 進池): 釉色+品類 (青花瓷盤/粉彩花瓶/琺琅...)\n"
    "    優先級 5 (★ 1.5x 進池): 日本陶瓷專名 (備前燒/九穀燒/常滑燒/薩摩燒/清水燒/志野燒...)\n"
    "    優先級 6 (基準): 熱門短詞 (Phase 9 實測短詞 BM25F 高 +2-4 rel) — **僅商品無上述具體識別碼時用**\n\n"
    "    ❌ 通用詞主詞拖累 (進池倍率 0.28x — 進池組僅 2.7% vs 不進池組 9.8%):\n"
    "       裸用「翡翠/瑪瑙/和田玉/蜜蠟/珍珠/水晶/手鐲/戒指/耳環/胸針/鏡頭/手錶/碗/盤/壺」當主詞\n"
    "       → 一定要加區隔詞 (具體朝代/品牌/釉色/材質特徵)\n\n"
    "    決策樹:\n"
    "      原標題含具體朝代名 (鹹豐/光緒/雍正)? → 用該朝代當主詞首詞 (★★★)\n"
    "      原標題含歐美品牌 (VISVIM/Meissen)?  → 用該品牌當主詞首詞 (★★★)\n"
    "      原標題含釉色+品類 (青花瓷盤)?      → 釉色+品類整串 (★★)\n"
    "      皆無 → 用熱門 2 字短詞 (基準, BM25F 高)\n\n"
    "★★★ 強制硬規則 — 主詞副詞最少 2 字 (V37 實測 37250 件 410 件孤立 1 字 token 全部來自 AI 給 1 字副詞):\n"
    "    ❌ 主詞「玉」「珠」「銅」「老」「古」「件」「對」(Yahoo BM25F 不會獨立索引這些單字)\n"
    "    ❌ 副詞「老」「銅」「玉」「珠」「子」「件」「對」(無搜索價值, 純廢字)\n"
    "    ✅ 主詞「古玉」「瑪瑙珠」「銅器」「老件」「古玩」(2 字以上才能命中查詢)\n"
    "    ✅ 副詞「老件」「銅器」「古玉」「珠寶」「對杯」「飾品」(2 字以上才有 rel 加分)\n"
    "    → 即使原標題資訊極少, 也不能輸出 1 字 token, 寧可給空字串\n"
    "    → 1 字常見副詞陷阱: 「老件」原標題剩「老」字殘留, 必須拼合成「老件/古老」或丟棄\n\n"
    "回覆格式：純JSON對象，key是序號(字串)，value是{\"主\":\"...\",\"副\":\"...\"}。\n"
    "只回覆JSON，不要其他文字。"
)

def _extract_seo_keywords(titles: List[str], log_fn: Callable,
                           progress_fn: Optional[Callable] = None,
                           attrs: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    """批量提取每個標題的主副搜索關鍵詞。返回 (主詞列表, 副詞列表)

    attrs: 選填, 每題對應的「商品屬性串」(e.g. '年代:宋 | 窯口:景德鎮窯 | 材質:陶瓷')
           若給了, AI 能看到標題外的結構化屬性, 主詞副詞判斷更準
    """
    primary = [""] * len(titles)
    secondary = [""] * len(titles)
    non_empty = [(i, t) for i, t in enumerate(titles) if t.strip()]
    if not non_empty:
        return primary, secondary

    log_fn(f"[關鍵詞提取] 開始提取 {len(non_empty)} 條標題的搜索關鍵詞...")
    kw_bs = CFG.keyword_batch_size
    batches = [non_empty[i:i + kw_bs] for i in range(0, len(non_empty), kw_bs)]

    def do_kw_batch(batch):
        # batch = [(orig_idx, title), ...], 發給API用連續編號 1..N
        lines = []
        for i, (orig_idx, t) in enumerate(batch):
            line = f"{i + 1}. {t}"
            if attrs and orig_idx < len(attrs) and attrs[orig_idx]:
                line += f"  [屬性: {attrs[orig_idx]}]"
            lines.append(line)
        user_msg = f"以下是{len(batch)}個商品標題(含結構化屬性), 請提取搜索關鍵詞:\n\n" + "\n".join(lines)
        body = {
            "model": CFG.seo_model or CFG.model,
            "messages": [
                {"role": "system", "content": KEYWORD_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            # response_format 由 prompt 內指定，部分中轉站不支持此參數
            "temperature": 0.1,
        }
        for attempt in range(1, CFG.max_retries + 1):
            try:
                content = _chat(body, CFG.timeout)
                content = re.sub(r"```json\s*", "", content)
                content = re.sub(r"```\s*$", "", content.strip())
                try:
                    parsed = json.loads(content)
                except Exception:
                    m = re.search(r'\{.*\}', content, re.DOTALL)
                    if not m:
                        raise ValueError("no JSON found")
                    parsed = json.loads(m.group())
                out = {}
                for key, value in parsed.items():
                    try:
                        idx = int(key) - 1  # API回覆的1-based → 0-based batch位置
                        if 0 <= idx < len(batch):
                            orig_idx = batch[idx][0]  # 對應回原始titles的索引
                            if isinstance(value, dict):
                                p = str(value.get("主", "")).strip()
                                s = str(value.get("副", "")).strip()
                                out[orig_idx] = (p, s)
                            elif isinstance(value, str):
                                out[orig_idx] = (value.strip(), "")
                    except (ValueError, TypeError):
                        continue
                return out
            except Exception:
                time.sleep(min(2 ** attempt, 8))
        return {}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, CFG.workers)) as ex:
        futs = {ex.submit(do_kw_batch, b): b for b in batches}
        for fut in as_completed(futs):
            if _is_stopped():
                for f in futs:
                    f.cancel()
                log_fn("[關鍵詞提取] 收到停止信號，中止中...")
                break
            try:
                res = fut.result()
                for idx, (p, s) in res.items():
                    if p:
                        primary[idx] = p
                    if s:
                        secondary[idx] = s
            except Exception:
                pass
            done += 1
            if progress_fn:
                progress_fn(done, len(batches), "關鍵詞提取")

    extracted = sum(1 for r in primary if r)
    # 多輪重試失敗項目（帶退避等待）
    MAX_KW_RETRIES = 3
    for retry_round in range(1, MAX_KW_RETRIES + 1):
        missing = [(i, t) for i, t in non_empty if not primary[i]]
        if not missing or (_is_stopped()):
            break
        total_non_empty = len(non_empty)
        fail_rate = len(missing) / total_non_empty if total_non_empty else 0

        if fail_rate > 0.5:
            wait_secs = min(30 * retry_round, 90)
            log_fn(f"[關鍵詞提取 重試{retry_round}/{MAX_KW_RETRIES}] {len(missing)}/{total_non_empty} 條未提取到關鍵詞 ({fail_rate:.0%})，等待 {wait_secs}s...")
            for _w in range(wait_secs):
                if _is_stopped():
                    break
                time.sleep(1)
            if _is_stopped():
                break
        else:
            log_fn(f"[關鍵詞提取 重試{retry_round}/{MAX_KW_RETRIES}] {len(missing)} 條未提取到關鍵詞，重試中...")

        retry_batch_size = max(4, CFG.keyword_batch_size // 2)
        retry_batches = [missing[i:i + retry_batch_size] for i in range(0, len(missing), retry_batch_size)]
        with ThreadPoolExecutor(max_workers=max(1, CFG.workers)) as ex:
            futs = {ex.submit(do_kw_batch, b): b for b in retry_batches}
            for fut in as_completed(futs):
                try:
                    res = fut.result()
                    for idx, (p, s) in res.items():
                        if p:
                            primary[idx] = p
                        if s:
                            secondary[idx] = s
                except Exception:
                    pass
        new_extracted = sum(1 for r in primary if r)
        fixed_this_round = new_extracted - extracted
        log_fn(f"[關鍵詞提取 重試{retry_round}] 新增 {fixed_this_round} 條，累計 {new_extracted}/{total_non_empty}")
        extracted = new_extracted

        if fixed_this_round == 0 and fail_rate > 0.8:
            log_fn(f"[關鍵詞提取] API 持續無回應，停止重試")
            break

    final_extracted = sum(1 for r in primary if r)
    skipped = len(non_empty) - final_extracted
    if skipped > 0:
        log_fn(f"[關鍵詞提取] 完成 {final_extracted}/{len(non_empty)} 條 ({skipped} 條標題過短/無內容，跳過)")
    else:
        log_fn(f"[關鍵詞提取] 完成 {final_extracted}/{len(non_empty)} 條，全部成功")
    return primary, secondary


# ──────────────────── SEO 標題優化 ────────────────────
# 基於 141,961 樣本 (10 品類 × 30 關鍵詞 × 5 頁) 實測 + 6 輪 Yahoo 內部 API 逆向
# 數據來源: phase_data/phase4_summary.json
SEO_SYSTEM_PROMPT = (
    "你是Yahoo拍賣SEO標題優化專家。\n\n"
    "【Yahoo Vespa 引擎排名公式 (逆向工程核心 — 知其所以然)】\n"
    "final_rank = nativeRank(title) × 0.6 + freshness × 0.25 + sellerQuality × 0.15\n"
    "nativeRank(title) = bm25f(tf,dl) × queryCoverage × proximity × firstOccurrence\n\n"
    "【5 因子解析 — 你寫的每個字都要符合這 5 條】\n"
    "1. bm25f: 詞頻飽和效應, 同詞 2 次 ≈ 1 次 → 禁止重複\n"
    "2. queryCoverage: 買家搜索詞被標題覆蓋的比例, 100% > 部分 > 0\n"
    "3. proximity: 主副詞字符距離, 相鄰=1.0 / 隔5字=0.5 / 隔20字=0.1\n"
    "4. firstOccurrence: 主詞越靠前權重越高, 開頭=1.0 / 中間=0.7 / 末尾=0.5\n"
    "5. 斷字: 空格切 token + 中文 bigram, 「古伊萬里盤」=1 token+4 bigram\n"
    "★ 完整匹配規則: 搜「古伊萬里」能匹配「古伊萬里盤」(子字串), 但搜「伊萬里盤」不匹配「古伊萬里 盤」(被空格切斷)\n\n"
    "以下 9 條硬規則皆由 141,961 樣本實測證據導出, 直接執行:\n\n"
    "【規則 1 - 只用繁體中文】\n"
    "- 台灣買家用繁體, 標題必須繁體\n"
    "- 自動簡轉繁：手表→手錶、項鏈→項鍊、复古→復古、戒子→戒指、耳飾→耳環\n"
    "- 日文假名（ぁ-ん、ァ-ヶ）一律翻成中文\n\n"
    "【規則 2 - 繁體字優先詞】實測能吃雙/三邊流量\n"
    "以下繁體寫法實測可被多種搜索命中：\n"
    "  手錶 → 被「手錶/手表/腕錶」命中\n"
    "  項鍊 → 被「項鍊/項鏈」命中\n"
    "  手鐲 → 被「手鐲/腕鐲」命中\n"
    "  眼鏡 → 被「眼鏡/墨鏡/太陽眼鏡」命中\n"
    "  戒指 → 被「戒指/戒子」命中\n"
    "  耳環 → 被「耳環/耳飾」命中\n"
    "- 如主詞是這類詞，務必用繁體寫法\n\n"
    "【規則 3 - 標題必須兼含「熱門短詞 + 精準長詞」】\n"
    "實測跨查詢 rel 量化差異（Phase 9 — 同商品不同查詢比較）：\n"
    "  同一 AirPods 商品: 搜「AirPods」rel=106.43 vs 搜「藍牙耳機」rel=103.10（差 3.33）\n"
    "  同一電烤箱:      搜「烤箱」rel=105.70 vs 搜「烤麵包機」rel=101.13（差 4.57）\n"
    "  同一水晶手環:    搜「手環」rel=103.87 vs 搜「水晶手環」rel=101.43（差 2.43）\n"
    "結論：短詞／品牌詞查詢下 rel 比長詞查詢高 2~4 分，差距巨大\n"
    "策略：\n"
    "1. 標題必含「買家會搜的 2 字熱類」或「國際品牌名」\n"
    "   - 有 iPhone → 寫「iPhone」不只「手機殼」\n"
    "   - 有手環 → 必寫「手環」，再加「水晶手環」長尾\n"
    "   - 有 AirPods → 寫「AirPods」不只「藍牙耳機」\n"
    "2. 同時保留長尾詞作副搜索路徑（短詞熱但競爭大，長尾精準但池小）\n"
    "3. 核心關鍵詞連續不拆：「復古胸針」整串 82 件 vs「復古 胸針」5,261 件（67 倍差）\n"
    "4. 標題中兼含兩者 = 吃兩個查詢池 = 總流量最大\n\n"
    "【規則 3b - 規格單位用台灣式 (V40 實測 30k 件 34 件殘留中國式單位)】\n"
    "✅ 必用台灣式: cm / mm / ml / g / kg / 吋\n"
    "❌ 禁用中國式: 厘米 / 釐米 / 毫米 / 毫升 / 公克 / 公分 (轉成 cm)\n"
    "  例: 「6 厘米」 → 「6 cm」 / 「240 毫升」 → 「240 ml」 / 「200 克」 → 「200 g」\n"
    "  例: 「1.88 公斤」 → 「1.88 kg」 / 「12 寸」 → 「12 吋」\n"
    "  注意: 「克拉/克難/巴克/兆克」等專名保留中文, 不轉\n\n"
    "【規則 4 - 符號禁忌】\n"
    "- 禁用：・ ． — ／ （會破壞 Yahoo 分詞，實測搜「復古・胸針」= 0 筆）\n"
    "- 允許：半形空格 (中劃線優勢已失效, 2026-04-27 重新實測連寫/空格/中劃線結果相同)\n"
    "- 不同概念用空格分隔；同一概念連寫不加空格\n\n"
    "【規則 5 - 同義詞 1-2 個用空格分隔】不要堆 4+\n"
    "- 2026-04-27 V20 重驗 sort=rel TOP 1: 「胸針 別針 胸花」TOP1 標題就用 3 同義詞 + 空格分隔, BM25 多 term 加分\n"
    "- 但別堆到 4-5 個 (邊際效益遞減 + 擠掉其他資訊)\n"
    "- 胸針 → 加「別針」(主+1 同義) 或「別針 胸花」(主+2 同義) 都可\n"
    "- 同義詞之間用空格, 不要連寫\n\n"
    "【規則 6 - 英文處理】\n"
    "- 品牌/型號保留英文：GUCCI / CHANEL / iPhone15Pro / AirPods\n"
    "- 通用詞必須中文：red→紅色、silver→銀、strap→錶帶\n"
    "- 大小寫不敏感（GUCCI = gucci 同樣結果）\n\n"
    "【規則 7 - 同一詞不重複 2 次以上】\n"
    "- BM25 飽和：同詞出現 2 次 ≈ 1 次，重複浪費字數\n"
    "- 例外：主詞在開頭可再出現 1 次加強 coverage\n\n"
    "【規則 8 — 廢詞清單 (V25 sort=popular TOP 30 實測校正, 2026-04-27)】\n"
    "❌ 真 0 流量廢詞 (V25 TOP 30 max buy = 0 — 必刪):\n"
    "    美術品 / 術品 / 品美 / 工蕓 / 蕓品 / 古美術 / 藝品 / 器裝 / 鎮陶\n"
    "    → 「日本美術品」「古美術工蕓」組合 sort=popular TOP 30 完全沒銷量, 必刪\n"
    "⚠️ 原 Phase 18+20 標廢但 V25 重驗實際有買家 — **不再扣分, 但也別當主詞**:\n"
    "    茶道 (30/30 有銷量 max=20) / 置物 (30/30 max=364) / 額裝 (30/30 max=231)\n"
    "    帶留 (18/30 max=262) / 陶磁 (20/30 max=285) / 磁器 (13/30 max=20)\n"
    "    → chen749 自家用這些詞流量 0, 但不是詞本身廢, 是 chen749 整體 SEO 弱\n"
    "    → 若商品本質是這類 (帶留/茶道具/置物擺飾), 可保留但別堆砌\n"
    "✅ 推薦替代: 用「擺飾」替代「裝飾」(實測「擺飾」是買家搜的詞)\n\n"
    "【規則 9 — Yahoo 允許分類 SEO 決策樹 (Phase 28 逆向 popular 池 + XDZHGL allowed_categories)】\n"
    "★ 重要: 只針對**允許上架**的 14 個頂層分類做 SEO, 以下 8 個禁止分類商品不處理:\n"
    "  ❌ 禁止: 美容保養/電腦平板/汽機車/電玩遊戲/美食特產/嬰幼兒/寵物/成人專區\n"
    "  (若商品屬這些類, 標題簡單翻繁 + 去垃圾即可, 不要套熱詞策略)\n\n"
    "Step 1: 識別商品屬允許分類 → Step 2: 用該分類 popular 池實測主詞池\n\n"
    "├─ 【古董、藝術與礦石】★ 主力\n"
    "│  主詞池: 古玩 / 古董 / 古瓷 / 古瓷器 / 老件 / 老貨 / 老物件 / 高古瓷 / 古玉\n"
    "│  子類特化:\n"
    "│    水晶(78件) → 主詞: 水晶/天然水晶/黃水晶/紫水晶/白水晶/能量水晶\n"
    "│    玉石(35)   → 和田玉/玉石/翡翠/青玉/碧玉/古玉/羊脂玉\n"
    "│    佛像(32)   → 古佛像/老佛像/明代佛像/清代佛像/銅佛像/木雕佛像\n"
    "│    印石(26)   → 壽山石/田黃/青田石/雞血石/印章石\n"
    "│    風水開運(26) → 風水/開運/貔貅/招財/葫蘆\n"
    "│    木雕(22)   → 古木雕/老木雕/清代木雕/黃楊木雕/紅木雕\n"
    "│    書畫(19)   → 書法/國畫/水墨/字畫/古字畫/書畫作品\n"
    "│    香品(15)   → 沉香/檀香/線香/香道/老沉香\n"
    "│    紫砂壺(14) → 紫砂壺/老紫砂/段泥/朱泥/紫泥/宜興紫砂\n"
    "│    陶器琺瑯漆器(14) → 陶器/琺瑯/漆器/景泰藍/掐絲琺瑯\n"
    "│  ★ 禁用「中國」; 用朝代: 宋代/明代/清代/民國/元代/唐代\n"
    "│  ★ 日本迴流用: 日本/九穀燒/赤繪/染付/京燒/和服/大正/昭和/明治\n"
    "│  ★ 禁用通用 2 字撞現代池: 瓶/碗/盤/擺件/佛像/杯 必加古/老/朝代前綴\n"
    "│\n"
    "├─ 【女裝與服飾配件】★\n"
    "│  主詞池: 洋裝 / 連身裙 / 短袖上衣 / 長袖上衣 / 顯瘦 / 百搭 / 正韓\n"
    "│  子類特化:\n"
    "│    女裝上衣(102) → 上衣女/短袖上衣/長袖上衣/T恤女/襯衫女\n"
    "│    品牌服飾(71) → 品牌+品類 (GUCCI上衣/CHANEL外套)\n"
    "│    內衣/睡衣(61) → 內衣/睡衣套裝/睡衣家居服\n"
    "│    褲子(39) → 長褲女/短褲女/牛仔褲女/連身褲\n"
    "│    大尺碼(23) → 大尺碼/加大碼/XL/XXL\n"
    "│    牛仔(23) → 牛仔褲/牛仔外套/丹寧\n"
    "│    外套(19) → 外套女/夾克女/大衣女/風衣\n"
    "│    裙子(19) → 短裙/長裙/連身裙/包臀裙/A字裙\n"
    "│    襪子(16) → 絲襪/褲襪/短襪/船襪\n"
    "│    帽子(15) → 棒球帽/漁夫帽/毛帽\n"
    "│\n"
    "├─ 【男性精品與服飾】★\n"
    "│  子類特化:\n"
    "│    男裝(188) → 襯衫男/t恤男/外套男/褲子男/毛衣男\n"
    "│    配件(48) → 皮帶/領帶/領結/袖扣\n"
    "│    包包/皮夾(47) → 男包/斜背包/公事包/男皮夾\n"
    "│    男鞋(37) → 皮鞋/休閒鞋/運動鞋男\n"
    "│\n"
    "├─ 【女包精品與女鞋】★ (大量代購商品)\n"
    "│  子類特化:\n"
    "│    流行時尚包款(145) → 斜背包/手提包/後背包/腋下包/托特包\n"
    "│    名牌精品包(69)   → 品牌+包 (Coach手提包/LV斜背包/CHANEL包)\n"
    "│                      ★ 代購商品標題必加「附購證」「代購」(佔池 TOP)\n"
    "│    名牌精品皮夾(65) → 品牌+皮夾 (Coach皮夾/LV皮夾/Dior皮夾)\n"
    "│    靴子(36) → 短靴/長靴/馬丁靴\n"
    "│    涼鞋/拖鞋(25) → 涼鞋女/拖鞋女/羅馬涼鞋\n"
    "│    皮夾(12) / 化妝包(9) / 跟鞋(5)\n"
    "│\n"
    "├─ 【手錶與飾品配件】★\n"
    "│  子類特化:\n"
    "│    手錶(77)          → 手錶/機械錶/石英錶/女錶/男錶 + 品牌\n"
    "│    珠寶/鑽石(51)     → 鑽石/珠寶/18K金/PT950\n"
    "│    女性流行飾品(50)  → 項鍊/耳環/手鍊/戒指 (必繁體)\n"
    "│    品牌配件(26)      → 品牌+配件 (HERMES絲巾/GUCCI皮帶)\n"
    "│    男性飾品(12)      → 男項鍊/男戒指/男手鍊\n"
    "│    打火機(10) / 鑰匙圈(6)\n"
    "│\n"
    "├─ 【運動、戶外與休閒】★\n"
    "│  子類特化:\n"
    "│    男/女運動鞋(150) → Nike/Adidas+鞋款\n"
    "│    運動用品(85)     → 瑜珈墊/啞鈴/跑步/健身\n"
    "│    男/女運動服(80)  → 運動服/運動褲/運動內衣\n"
    "│    樂器(35)         → 吉他/鋼琴/烏克麗麗/口琴\n"
    "│    戶外(7)          → 登山/露營/帳篷\n"
    "│\n"
    "├─ 【偶像、球員卡與郵幣】★\n"
    "│  子類特化:\n"
    "│    明星偶像(78)       → 明星名+周邊 (BTS/SEVENTEEN/AKB)\n"
    "│    球員卡(52)         → 球員名卡/NBA/MLB/中華職棒/遊戲王/寶可夢\n"
    "│    郵票(18)           → 紀念郵票/年代+郵票\n"
    "│    商標收藏(17)       → 品牌+收藏 (麥當勞/可口可樂/7-11)\n"
    "│    錢幣古錢幣(14)     → 古錢/清代錢幣/民國錢幣/通寶\n"
    "│    鈔票紙鈔(9) / 名人簽名(7)\n"
    "│\n"
    "├─ 【玩具、模型與公仔】★\n"
    "│  子類特化:\n"
    "│    動漫週邊(60)  → 動漫名+週邊/鬼滅/火影/海賊王\n"
    "│    絨毛玩偶(54)  → 玩偶/娃娃/熊/卡通玩偶\n"
    "│    GK模型(8) / Cosplay(3)\n"
    "│\n"
    "├─ 【居家、家具與園藝】★ (限定子類允許)\n"
    "│  子類特化:\n"
    "│    寢具/家飾(47)  → 床包/被套/枕頭/抱枕\n"
    "│    收納用品(38)   → 收納箱/收納袋/收納盒/衣架\n"
    "│    廚房用品(34)   → 鍋具/餐具/餐盤/刀具\n"
    "│    傢俱/床墊(7)\n"
    "│\n"
    "├─ 【圖書/影音/文具】★\n"
    "│  子類特化:\n"
    "│    音樂與影片(378)★ → CD/DVD/黑膠/藍光/VCD\n"
    "│    文具(17) → 原子筆/鋼筆/筆記本\n"
    "│\n"
    "├─ 【相機、攝影與周邊】★ (限定子類)\n"
    "│  子類特化:\n"
    "│    鏡頭(23) → 品牌+鏡頭 (Canon鏡頭/Nikon鏡頭/Sony鏡頭)\n"
    "│    相機周邊(14) → 腳架/快拆板/記憶卡/電池\n"
    "│    單眼相機(8) / 底片相機(5) / 攝影機(3)\n"
    "│\n"
    "├─ 【家電與影音視聽】★ (只允許音樂視聽子類)\n"
    "│    影音/視聽/MP3(17) → MP3/隨身聽/耳機/音響/喇叭\n"
    "│\n"
    "├─ 【手機、配件與通訊】★ (限定子類)\n"
    "│    手機吊飾(14) → 手機吊飾/吊飾/掛繩/絨球\n"
    "│    iPhone週邊(1) / 展示機(1)\n"
    "│\n"
    "└─ 【原創設計良品】★ (11 允許)\n"
    "    手作 / 原創 / 設計師商品\n\n"
    "★ 信任詞 (V36 sort=rel 跨 4 大品類重驗 2026-04-27):\n"
    "  古董類有效: 收藏(30%) / 老件(13%) / 嚴選(11%) ← 可加 1-2 個\n"
    "  飾品/服飾/3C: 全部信任詞 < 3% — **不主動加** (空間留給主副詞)\n"
    "  ❌ 「精選/熱賣/稀有/推薦」全品類含率 < 1%, 沒有實證加分, 移除強推\n\n"
    "★ 避開通用詞撞現代池:\n"
    "  古董類用「古瓷瓶/古佛像/古木雕」非「瓶/佛像/木雕」\n"
    "  服飾類用「連身裙/短袖上衣」非「裙/上衣」\n"
    "  飾品類用「手鐲/項鍊/耳環」(繁體吃多邊流量)\n\n"
    "【規則 9b — 產地年代詞 (V35 sort=rel 跨 15 品類重驗 2026-04-27)】\n"
    "舊 prompt 寫「法國 3.8x / 大正 21x」是 popular 池現象, **sort=rel 跨品類平均含詞率**:\n"
    "  日本 7% / 韓國 2% / 法國 1% / 德國/義大利/英國/台灣製/大正/昭和/明治 全 0-1%\n"
    "✅ 真實有效:\n"
    "  「日本」對 茶碗(57%)/花瓶(20%)/古董類 — 商品確實是日本來源時必加\n"
    "  「韓國」對 耳環/服飾類(33%) — 韓系商品可加\n"
    "  「清代/明代/宋代」對撞池主詞 — V33 證實「清代瓶」「宋代碗」100% 進古董池\n"
    "  ★ 禁用「中國」(台灣市場忌諱); 中國物件用朝代名\n"
    "❌ 不要強加: 法國/德國/義大利/大正/昭和/明治 (sort=rel TOP 30 含率全 < 2%)\n"
    "   除非商品實質就是該產地/年代, 否則別硬塞流量信號\n"
    "  西式可選: vintage / 古董 / 老件 (商品實質匹配時)\n\n"
    "【規則 9c — 中式古瓷/古董專業詞 (咸鱼 14k 商品實測, 收藏圈會搜)】\n"
    "窯口/釉色/款式/材質是收藏買家的搜索入口, 若商品屬性明確應保留:\n"
    "  窯口 (副詞強信號): 景德鎮窯 / 汝窯 / 龍泉窯 / 官窯 / 磁州窯 / 定窯 /\n"
    "         建窯 / 鈞窯 / 哥窯 / 吉州窯 / 邢窯 / 耀州窯 / 越窯 / 湖田窯\n"
    "  釉色工藝 (副詞): 青花 / 粉彩 / 琺琅彩 / 鬥彩 / 五彩 / 釉裡紅 / 單色釉 / 三彩\n"
    "  款式 (主詞候選): 瓶 / 吊墜 / 擺件 / 盤 / 碗 / 茶壺 / 杯 / 罐 / 文房器 /\n"
    "         手鍊 / 手把件 / 項鍊\n"
    "  材質: 陶瓷 / 銅 / 和田玉 / 紫砂 / 翡翠 / 壽山石 / 錫 / 銀 / 青石 / 牛骨\n"
    "範例 (古瓷類):\n"
    "  清代粉彩花卉瓷 → 清代粉彩 花卉瓶 景德鎮窯 古董擺飾 稀有收藏\n"
    "  汝窯青瓷盤 → 汝窯青瓷 宋代古盤 擺飾 收藏稀有\n"
    "  雍正青花釉裡紅 → 清代青花 釉裡紅 官窯 擺飾 稀有收藏\n"
    "範例 (V35 重驗 2026-04-27, 移除無效產地年代):\n"
    "  胸針 → 復古胸針 別針 珍珠 (法國只 V35 顯示 3/30, 商品確實是法國再加)\n"
    "  洋裝 → 復古洋裝 連身裙 木扣 (日本只商品實是日本再加)\n"
    "  茶杯 → 古茶杯 日本 老件 (V33: 茶碗 日本 17/30 強信號)\n\n"
    "【規則 10 — 正反對比範例 (chen749 實測 0 瀏覽 vs 改寫)】\n"
    "❌ 染付5枚古美術                 → ✅ 日本大正染付擺飾 復古稀有收藏\n"
    "❌ 古董帶留 茶道擺設            → ✅ 日本大正和服帶留 復古擺飾\n"
    "❌ 日本美術品置物古董工蕓銅瓶   → ✅ 日本古董銅瓶 大正復古稀有擺飾\n"
    "❌ 中國美術人物像玉石雕刻古美術 → ✅ 清代玉石雕刻人物像 古董擺飾 收藏復古\n"
    "❌ 漆器小碟 木胎漆器 三件組     → ✅ 日本漆器小碟 木胎三件組 復古收藏\n"
    "關鍵差: 刪美術系列廢詞 + 加產地+年代+吸流量詞 + 擺飾替代裝飾\n\n"
    "【規則 11 — 標題骨架順序 (V35+V36 校正 2026-04-27)】\n"
    "骨架 (從左到右):\n"
    "  [品牌/型號] → [主詞 連續書寫] → [副詞 同義或長尾, 空格分隔] →\n"
    "  [產地/年代 商品確實是時才加] → [信任詞 古董類加 1 個]\n"
    "★ 連續主詞 +0.276~+0.348 rel (V21 最強信號)\n"
    "★ 副詞同義 (戒指→戒子, 手錶→腕錶) sort=rel TOP 30 含 30/30\n\n"
    "實例對照 (V35+V36 校正):\n"
    "  3C   → iPhone 15Pro 256G 手機殼 防摔 太空黑 (信任詞 < 1%, 不加)\n"
    "  古董 → 古董染付 茶杯 5枚 擺飾 日本 收藏 (商品實是日本+古董類加收藏)\n"
    "  飾品 → 復古胸針 別針 珍珠 純銀 (V20+V32 同義詞合理, 飾品信任詞 < 3%)\n"
    "  服飾 → 復古洋裝 連身裙 棕色 木扣 (服飾信任詞 < 1%, 不加)\n"
    "  金飾 → 925銀 手鍊 水晶 珍珠 (商品實是 925 才加)\n\n"
    "【規則 11b — V37 跨 37250 件深度分析校正 4 大常見錯誤 (2026-04-27)】\n\n"
    "★ 錯誤 1: 形容詞型輸家詞絕對禁用 (V41 8,681 件硬數據+ eBay 961k 件實證 雙重驗證):\n"
    "  ❌ 全標題禁 (不只首詞): 收藏 / 老件 / 嚴選 / 老物件 / 古董 / 老貨 / 精美 / 絕版 / 稀有 /\n"
    "                          頂級 / 極品 / 原裝 / 正品 / 保真 / 經典 / 限量 / 撿漏 / 難得 / 品相佳\n"
    "  V41 證據: 「收藏」贏家 32.8% / 輸家 43.5% (-10.7%) → 用越多越輸\n"
    "  V41 證據: 「老件」贏家 9.4% / 輸家 14.4% (-5.1%)  → 同向\n"
    "  V41 證據: 「日本」贏家 9.1% / 輸家 12.8% (-3.6%)  → 中文紅海被霸榜\n"
    "  ❌ 收藏 老件 玉雕擺件         → ✅ 玉雕擺件 唐代 (用具體年代取代收藏)\n"
    "  ❌ 老物件 銅章 紀念章         → ✅ 銅章 1980 紀念章 (具體年份)\n"
    "  ❌ 精美翡翠手鐲 頂級老件      → ✅ 翡翠手鐲 緬甸 A貨 冰糯種 (具體材質工藝)\n"
    "  ❌ 古董 紙鈔 民國 收藏稀有    → ✅ 民國紙鈔 中央銀行 PMG65\n"
    "  ★ 首詞必須是 IDF 高的具體詞 (品牌/朝代具體名/窯口/作家)\n"
    "  ★ 古玩類仍可保留 1 個收藏詞放尾 (古董類有效率 11%), 但不堆\n\n"
    "★ 錯誤 2: 品牌商品 (Nike/GUCCI/iPhone/賓得/PRADA/CHANEL/Coach/Pentax/VISVIM...) 必保留品牌+型號/系列/年份:\n"
    "  ❌ 復古外套                    → ✅ VISVIM 21AW 外套 復古\n"
    "  ❌ 鏡頭                        → ✅ 賓得Pentax DAL55-300 鏡頭 變焦\n"
    "  ❌ 包包                        → ✅ Coach 斜背包 經典款\n"
    "  ★ 品牌買家用「品牌+型號」精準搜 (型號 DAL55-300 / iPhone15Pro / 系列 21AW SS24 / 年份 1990s)\n"
    "  ★ 原標題出現品牌或型號代碼 (字母+數字組合) 必須完整保留, 切勿砍成通用品類\n\n"
    "★ 錯誤 3: 標題長度 20-30 字 (V41 校正: 中文 SEO sweet spot, Bing 反 stuffing):\n"
    "  ★★★ 理想 25-30 字 (中位 28 字最佳)\n"
    "  ★★★ 下限 ≥ 20 字 — 原標題實質資訊不足時, 不強行湊 25 字 (禁編造)\n"
    "  ★★★ 上限 ≤ 30 字 — 超過 Bing/Yahoo 視為 keyword stuffing 反扣分\n"
    "  ★★★ V41 重點: 不要只砍, 要「砍黑名單詞 + 補實質長尾」雙動作\n"
    "  ★★★ 禁編造: 原標題沒尺寸不要編 (例: 「翡翠手鐲 緬甸A貨」原本就 9 字, 沒可補的具體細節 → 留 20 字 OK)\n"
    "  ★ 不到 25 字, 按以下優先順序補 (僅當商品實質有此資訊):\n"
    "    - 古董類 → 補具體窯口/作家 (景德鎮窯/汝窯/中村六郎/上野良樹) — IDF 高\n"
    "    - 飾品類 → 補材質工藝 (A貨/緬甸/冰糯種/925銀/打磨) + 規格 (cm/mm/克)\n"
    "    - 服飾類 → 補品牌型號 + 尺寸 + 年份\n"
    "    - 錢幣類 → 補評級 (PMG/PCGS/公博) + 朝代具體 (鹹豐/光緒) + 局名 (寶蘇/寶泉)\n"
    "  ★ 超過 30 字, 砍順序:\n"
    "    1. 先砍助詞 (的/這只/非常/適合/精美) - V41 規則 #9 去語法\n"
    "    2. 再砍重複的同義詞 (留 1-2 個就好)\n"
    "    3. 再砍黑名單詞 (收藏/老件/原裝...)\n"
    "    4. 最後砍最不重要的修飾\n"
    "  ❌ 古玉                        → ✅ 古玉吊墜 唐代和田玉 雕工精細 5cm\n"
    "  ❌ 銅鈴 古件                  → ✅ 銅鈴鐺 明清 銅製 直徑3cm 包漿自然\n"
    "  ❌ 茶碗                        → ✅ 日本茶碗 江戶期 古赤膚山 黑釉 12cm\n"
    "  ❌ 這只精美的1970年代Cartier自動腕錶 → ✅ Cartier 山度士 1970 自動腕錶 18K 黃金\n"
    "  ★ 補詞必須對得上商品實質, 不為湊字硬塞\n\n"
    "★ 錯誤 4: 主副詞與標題每個 token 最少 2 字 (V37 實測 410 件孤立 1 字 token = SEO 廢字):\n"
    "  ❌ 老 銅鈴鐺                  → ✅ 老銅鈴鐺 (1字「老」與下一詞拼合)\n"
    "  ❌ 茶碗 杯                    → ✅ 茶碗 (尾字重複, 移除「杯」)\n"
    "  ❌ 印章 印                    → ✅ 印章\n"
    "  ❌ 古 銅鈴                    → ✅ 古銅鈴\n"
    "  ★ 例外: 顏色/量詞/數字 (紅/藍/對/組/件/三/五/雙/大/小) 可單獨, 其餘 1 字必須拼合或刪除\n\n"
    "【規則 11c — V41 新增 (基於 8,681 件硬數據 + eBay 961k 件分析 + GitHub 研究 2026-05-03)】\n\n"
    "★ V41-1 前 13 字黃金區 (Yahoo TW 官方暗示權重最高):\n"
    "  IDF 最高的詞 (品牌/朝代具體名/窯口/作家) 必須在前 13 字內出現\n"
    "  ❌ 日本帶回 老件 1970年代 Cartier 山度士 18K 自動腕錶 男錶\n"
    "  ✅ Cartier 山度士 1970 自動腕錶 18K 男款 古董 瑞士\n"
    "  (Cartier 從第 12 字 → 第 1 字, 同字符數但搜尋權重大幅提升)\n\n"
    "★ V41-2 罕見詞 IDF 不只國際品牌, 擴充清單 (BM25F 數學保證 IDF 高加分):\n"
    "  ★★★ 國際品牌 (5x 倍率): Cartier/浪琴/Dunhill/Tiffany/Rolex/Patek/Omega/Coach/HERMES\n"
    "  ★★★ 罕見窯口: 龍泉窯/汝窯/官窯/磁州窯/定窯/建窯/鈞窯/哥窯/吉州窯/邢窯/耀州窯/越窯/湖田窯\n"
    "  ★★★ 罕見釉色: 粉青釉/影青釉/天目釉/唐三彩/釉裡紅/琺琅彩/鬥彩/單色釉\n"
    "  ★★★ 罕見作家: 中村六郎/上野良樹/坂田甚內/朱可心 (日本陶瓷/紫砂大師)\n"
    "  ★★★ 具體朝代名 (7x): 鹹豐/光緒/宣統/雍正/康熙/乾隆/同治/嘉慶/道光\n"
    "  ★★★ 評級碼 (4x): PMG/PCGS/公博/華夏/NGC + 數字\n"
    "  原則: 越罕見的詞 IDF 越高, 越能拉 BM25F 排名\n\n"
    "★ V41-3 標題去語法 (eBay 961,668 件實證 — 成功 listing 無語法邏輯, 純關鍵字串列):\n"
    "  砍助詞: 的 / 這只 / 非常 / 適合 / 一個 / 一只 / 已經 / 還有 / 而且\n"
    "  ❌ 這只精美的 1970 年代 Cartier 自動腕錶非常適合收藏\n"
    "  ✅ Cartier 山度士 1970 自動腕錶 18K 黃金 瑞士製 男錶 古董手錶\n"
    "  砍 5-8 字 → 騰出空間給長尾關鍵詞\n\n"
    "★ V41-4 規格雙覆蓋 (台灣買家慣用混搭):\n"
    "  標題用 cm (短碼省字) + 商品簡述用「公分」(老買家慣用)\n"
    "  標題: 直徑 5cm / 重量 3g / 容量 200ml\n"
    "  簡述: 直徑約 5 公分 / 重量約 3 克 / 容量約 200 毫升 (補充長尾搜尋)\n\n"
    "★ V41-5 反面測試 — 看標題是否被輸家信號污染:\n"
    "  自檢清單 (任一條 True 都該重寫):\n"
    "    ❑ 含「精美/絕版/稀有/頂級/極品/原裝/正品/保真/經典/限量/撿漏/難得/品相佳」?\n"
    "    ❑ 含「收藏/老件/古董」3 個以上?\n"
    "    ❑ 標題含助詞 (的/這只/非常/適合) ?\n"
    "    ❑ 標題 < 25 字 OR > 30 字?\n"
    "    ❑ IDF 高的詞 (品牌/窯口/作家) 不在前 13 字?\n\n"
    "★ V41-6 短標題補長尾 SOP (避免 V41 黑名單砍太多反而過短):\n"
    "  AI 容易過度精簡, 砍黑名單詞後標題只剩 13-20 字 → 違反 25 字下限!\n"
    "  正確做法: 砍黑名單後必須補長尾, 維持 25-30 字\n\n"
    "  範例:\n"
    "    ❌ 砍太多: 「翡翠手鐲 緬甸 A貨」 (9字, 只剩主詞)\n"
    "    ✅ 補長尾: 「翡翠手鐲 緬甸 A貨 冰糯種 圓條 56圈口 天然」 (25字, 補規格圈口)\n\n"
    "    ❌ 砍太多: 「Cartier 自動腕錶 1970年代 男錶」 (22字)\n"
    "    ✅ 補長尾: 「Cartier 山度士 1970 自動腕錶 18K黃金 男錶 瑞士製」 (28字, 補產地+材質)\n\n"
    "    ❌ 砍太多: 「古玉吊墜 和田玉 龍紋 明清」 (14字)\n"
    "    ✅ 補長尾: 「古玉吊墜 和田玉 龍紋 明清 雕工精細 5cm 包漿自然」 (27字, 補規格+包漿)\n\n"
    "  補長尾規則:\n"
    "    1. 規格詳細 (cm/mm/克/ml/直徑/重量)\n"
    "    2. 材質具體 (緬甸 A貨/和田玉/925銀/18K/純銅/紫砂)\n"
    "    3. 年代具體 (1970/明治/江戶/雍正)\n"
    "    4. 產地/作家 (瑞士/日本/法國/中村六郎/上野良樹)\n"
    "    5. 工藝細節 (鎏金/包漿/雕工/手繪/釉裡紅)\n\n"
    "【規則 12 — 改寫後自檢清單 (V16 sort=rel 真排名實測, 2026-04-27 重驗)】\n"
    "每條改寫完自問:\n"
    "  1. 長度 20-45 字? (V16 實測 sort=rel TOP 10 平均 33-51 字, 25-40 安全)\n"
    "     ★ 短於 20 → 資訊量不夠, 跨查詢匹配少\n"
    "     ★ 長於 65 → 關鍵詞密度被稀釋, 反而降 BM25\n"
    "     ★ 若 <25 字 → 強制加:\n"
    "       - 產地/朝代 (日本/清代/民國/大正/昭和)\n"
    "       - 窯口 (景德鎮窯/汝窯/龍泉窯)\n"
    "       - 釉色/材質 (青花/粉彩/單色釉/和田玉/紫砂)\n"
    "       - 款式/尺寸 (花口/葵口/直徑約 xx cm)\n"
    "       - 信任詞 (按下面輪換規則)\n"
    "     目標: 資訊量足, 不為湊字加廢詞\n"
    "  2. 信任詞 (V36 sort=rel TOP 30 跨 4 大品類重驗 2026-04-27):\n"
    "     ★ 古董類有效: 收藏(30%)/老件(13%)/嚴選(11%) — 可加 1 個\n"
    "     ★ 飾品/服飾/3C 信任詞 < 3% — **不加, 把空間留給主副詞**\n"
    "     ❌ 全品類「精選/熱賣/稀有/推薦」< 1%, 沒有實證效益\n"
    "     ❌ 不必要強制每批分散用 5 個信任詞 (是無效操作)\n"
    "  3. 若商品實質是擺件/瓶/罐/雕像/爐 → 必加「擺飾」(Phase 20 實測 4.2x)\n"
    "  4. 若商品有朝代屬性 (清代/明代/宋代/民國/大正/昭和) → 必加朝代詞\n"
    "  5. 若商品有窯口屬性 (景德鎮窯/汝窯/龍泉窯...) → 必加窯口詞當副詞\n"
    "  6. 若商品有釉色屬性 (青花/粉彩/琺琅彩...) → 必加釉色詞\n"
    "  7. 刪了咸鱼/淘寶套話? (議價/撿漏/大開門/欣賞貼/#標籤/私聊/一線下鄉)\n"
    "  8. 禁用「中國」? (台灣市場忌諱)\n"
    "  9. 禁用日文假名 (ぁ-ん、ァ-ヶ)?\n\n"
    "【誠實提醒】真正最強排名信號是有銷量（不可控），標題只能「避免被搜不到 + 上 popular 池」。\n"
    "按 12 條規則產出即可，不必為了「看起來優化」加多餘字。\n"
)

def _seo_optimize_titles(titles: List[str], log_fn: Callable,
                         progress_fn: Optional[Callable] = None,
                         keywords: Optional[List[str]] = None,
                         secondary_keywords: Optional[List[str]] = None,
                         attrs: Optional[List[str]] = None,
                         details: Optional[List[str]] = None) -> Tuple[List[str], set]:
    """返回 (optimized_titles, responded_keys)。responded_keys 記錄 API 成功回覆的索引。

    attrs: 選填, 每題對應的「商品屬性串」(e.g. '年代:宋 | 窯口:景德鎮窯 | 材質:陶瓷')
    details: 選填, 每題對應的「商品說明摘要」(detail 前 200 字, 用於抽大師名/年代/規格等長尾)
    """
    results = list(titles)
    responded = set()
    non_empty = [(i, t) for i, t in enumerate(titles) if t.strip()]
    if not non_empty:
        return results, responded

    log_fn(f"[SEO優化] 開始優化 {len(non_empty)} 條標題...")
    seo_bs = CFG.seo_batch_size
    batches = [non_empty[i:i + seo_bs] for i in range(0, len(non_empty), seo_bs)]

    def do_seo_batch(batch):
        # 重新編號為 0..N，避免模型對非零起始 key 自行重編號導致結果錯位
        orig_keys = [k for k, _ in batch]
        items = []
        for local_i, (k, t) in enumerate(batch):
            item = {"key": local_i, "title": t}
            if keywords and k < len(keywords) and keywords[k]:
                item["主詞"] = keywords[k]
            if secondary_keywords and k < len(secondary_keywords) and secondary_keywords[k]:
                item["副詞"] = secondary_keywords[k]
            if attrs and k < len(attrs) and attrs[k]:
                item["屬性"] = attrs[k]
            if details and k < len(details) and details[k]:
                item["說明摘要"] = details[k][:200]
            items.append(item)
        payload = {
            "instruction": (
                "對每條商品標題生成Yahoo拍賣SEO優化版本。"
                "「主詞」完整出現在開頭，「副詞」完整出現在標題中。"
                "若有「屬性」欄位, 代表原採集來源的結構化商品資訊 (年代/窯口/款式/釉色/材質/品牌), "
                "這是高可信度事實, 應利用它判斷商品類別並把關鍵屬性(如窯口=景德鎮窯、釉色=青花)融入標題。"
                "若有「說明摘要」欄位, 從中**主動抽取**高 IDF 熱搜詞補入標題: "
                "  ✅ 大師/作家名 (顧景舟/中村六郎/朱可心/喬治) "
                "  ✅ 具體年代 (1972/明治/江戶/民國三年) "
                "  ✅ 規格 (16.9cm/370cc/95克) "
                "  ✅ 評級分數 (PCGSXF45/公博85分) "
                "  ✅ 工藝/材質 (老紫泥/螭龍祥雲/天然綠松石/真金真鑽) "
                "  → 這些熱搜詞優先級高於標題原有內容!\n"
                "不同概念間用空格分隔。禁止重複詞語。禁止日文假名。禁用「中國」(台灣市場忌諱)。"
                "原標題資訊少時 (<20字), **必須從說明摘要補長尾達 25-30 字**, 不要為湊字數加無關詞。"
                '輸出JSON：{"results":[{"key":<int>,"seo_title":"..."}]}'
            ),
            "items": items,
        }
        body = {
            "model": CFG.seo_model or CFG.model,
            "messages": [
                {"role": "system", "content": SEO_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            # response_format 由 prompt 內指定，部分中轉站不支持此參數
            "temperature": 0.1,
        }
        try:
            content = _chat(body, CFG.timeout)
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*$", "", content.strip())
            try:
                parsed = json.loads(content)
            except Exception:
                m = re.search(r"\{.*\}", content, flags=re.S)
                if not m:
                    return {}
                parsed = json.loads(m.group(0))
            arr = parsed.get("results", [])
            # 把 local 0..N 的 key 映射回原始 key
            local_result = {int(x["key"]): str(x["seo_title"]) for x in arr if "key" in x and "seo_title" in x}
            result = {}
            for local_k, seo in local_result.items():
                if 0 <= local_k < len(orig_keys):
                    result[orig_keys[local_k]] = seo
            return result
        except Exception:
            return {}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, CFG.workers)) as ex:
        futs = {ex.submit(do_seo_batch, b): b for b in batches}
        for fut in as_completed(futs):
            if _is_stopped():
                # 取消所有未完成的 future
                for f in futs:
                    f.cancel()
                log_fn("[SEO優化] 收到停止信號，中止中...")
                break
            try:
                res = fut.result()
                for k, seo in res.items():
                    responded.add(k)
                    if seo.strip():
                        results[k] = seo.strip()
            except Exception:
                pass
            done += 1
            if progress_fn:
                progress_fn(done, len(batches), "SEO優化")

    changed_count = sum(1 for i, t in non_empty if results[i] != t)
    log_fn(f"[SEO優化] 完成，{len(responded)}/{len(non_empty)} 條已處理（{changed_count} 條標題有調整）")
    return results, responded

# ──────────────────── 進度保存/讀取 ────────────────────
import pickle as _pickle

def _progress_path(out_path: str) -> str:
    """進度文件路徑：輸出文件旁邊的 .progress 文件"""
    return out_path + ".progress"

def save_progress(out_path: str, data: dict):
    """保存中間進度"""
    pp = _progress_path(out_path)
    with open(pp, "wb") as f:
        _pickle.dump(data, f)

def load_progress(out_path: str) -> Optional[dict]:
    """讀取進度文件，若不存在或損壞返回 None"""
    pp = _progress_path(out_path)
    if not os.path.exists(pp):
        return None
    try:
        with open(pp, "rb") as f:
            return _pickle.load(f)
    except Exception:
        return None

def clear_progress(out_path: str):
    """刪除進度文件（完成後清理）"""
    pp = _progress_path(out_path)
    try:
        os.remove(pp)
    except OSError:
        pass


# ──────────────────── 主翻譯流水線 ────────────────────
def translate_dataframe(
    df: pd.DataFrame, tcol: str, dcol: str,
    log_fn: Callable = print,
    progress_fn: Optional[Callable] = None,
    stop_check: Optional[Callable] = None,
    ask_fn: Optional[Callable] = None,
    out_path: str = "",
) -> pd.DataFrame:
    """
    翻譯 DataFrame 中的標題和說明欄位。

    progress_fn(current, total, stage_name) — 進度回調
    stop_check() -> bool — 若返回 True 則中止
    ask_fn(question) -> bool — 向用戶提問，True=繼續等待, False=跳過
    out_path — 輸出文件路徑，用於進度保存
    """
    global _STOP_FN
    _STOP_FN = stop_check
    _acquire_route_manager()
    try:
        return _translate_dataframe_inner(df, tcol, dcol, log_fn, progress_fn, stop_check, ask_fn, out_path)
    finally:
        _release_route_manager()


def _translate_dataframe_inner(
    df: pd.DataFrame, tcol: str, dcol: str,
    log_fn: Callable, progress_fn: Optional[Callable],
    stop_check: Optional[Callable],
    ask_fn: Optional[Callable] = None,
    out_path: str = "",
) -> pd.DataFrame:
    out = df.copy()
    titles = out[tcol].fillna("").astype(str).tolist()
    descs = out[dcol].fillna("").astype(str).tolist()

    title_segs = [[x.strip()] if x.strip() else [] for x in titles]
    desc_segs = [split_segments(x) for x in descs]

    # ── 跳過正文翻譯模式（閒魚導出等已繁體的來源） ──
    if not CFG.enable_translate:
        log_fn("[翻譯] 已按用戶設定跳過正文翻譯")
        # 只做基礎簡繁轉換 + 用語本地化，不走 API
        seg_map: Dict[str, str] = {}
        for segs in title_segs + desc_segs:
            for s in segs:
                if s not in seg_map:
                    seg_map[s] = _apply_cn_tw_terms(cc.convert(s))

        out["標題（繁體）"] = [
            "".join([seg_map.get(s, s) for s in segs]) if segs else ""
            for segs in title_segs
        ]
        out["說明（繁體）"] = [
            "\n".join([seg_map.get(s, s) for s in segs if s]) if segs else ""
            for segs in desc_segs
        ]
    else:
        # ── 正常翻譯流程 ──

        # ── 分類片段 ──
        seen = set()
        need_api = []
        need_s2t = []
        keep_same = []
        for segs in title_segs + desc_segs:
            for s in segs:
                if s in seen:
                    continue
                seen.add(s)
                if is_latin_only(s):
                    keep_same.append(s)
                elif has_kana(s):
                    need_api.append(s)
                elif has_cjk(s):
                    if looks_like_chinese(s) and not looks_like_japanese_kanji(s):
                        need_s2t.append(s)
                    else:
                        need_api.append(s)
                else:
                    keep_same.append(s)

        # ── 載入快取（日文片段需驗證快取值是否有效） ──
        cache = load_cache()
        seg_map: Dict[str, str] = {}
        for s in cache:
            if s not in seen:
                continue
            cached_val = cache[s]
            # 如果原文是日文，但快取值看起來仍然主要是日文 → 視為無效快取
            if s in need_api and cached_val:
                # 條件1：快取值和原文完全相同（根本沒翻）
                if cached_val == s:
                    continue
                # 條件2：快取值的假名比例仍然很高（>40%），說明翻譯不完整
                if len(cached_val) > 0:
                    kana_chars = len(re.findall(r'[\u3040-\u30FF\uFF66-\uFF9D]', cached_val))
                    if kana_chars / len(cached_val) > 0.4:
                        continue
            # 條件3：確保快取值是繁體中文（簡體→繁體轉換）
            if cached_val and has_cjk(cached_val):
                cached_val = cc.convert(cached_val)
            seg_map[s] = cached_val

        s2t_pairs = {s: _apply_cn_tw_terms(cc.convert(s)) for s in need_s2t if s not in seg_map}
        seg_map.update(s2t_pairs)
        append_cache(s2t_pairs)

        keep_pairs = {s: s for s in keep_same if s not in seg_map}
        seg_map.update(keep_pairs)
        # keep_same 不寫快取 — 純英數片段不經 API，快取無意義

        api_pending = [s for s in need_api if s not in seg_map]

        total_segs = len(seen)
        cached = total_segs - len(api_pending)
        log_fn(f"片段統計：共 {total_segs} 段，快取命中 {cached}，需 API 翻譯 {len(api_pending)}")
        log_fn(f"語言分佈：日文(需API) {len(need_api)} | 中文(簡→繁) {len(need_s2t)} | 英數(保留) {len(keep_same)}")
        learned_count = len(_load_learned_dict())
        if learned_count:
            log_fn(f"自學習詞典：{learned_count} 條映射已載入")

        # ── API 翻譯 ──
        api_responded = set()  # 追蹤收到 API 回應的索引（不靠翻譯內容判斷）
        if api_pending:
            if stop_check and stop_check():
                return out

            items = list(enumerate(api_pending))
            batches = [items[i:i + CFG.batch_size] for i in range(0, len(items), CFG.batch_size)]
            done_batches = 0

            with ThreadPoolExecutor(max_workers=max(1, CFG.workers)) as ex:
                fut_to_b = {}
                for bidx, b in enumerate(batches):
                    fut = ex.submit(translate_segments_api, b, CFG.timeout, CFG.max_retries)
                    fut_to_b[fut] = (bidx, b)

                for fut in as_completed(fut_to_b):
                    if stop_check and stop_check():
                        for f in fut_to_b:
                            f.cancel()
                        log_fn("[翻譯] 收到停止信號，中止中...")
                        break

                    bidx, b = fut_to_b[fut]
                    try:
                        res = fut.result()
                        pairs = {}
                        for k, zh in res.items():
                            src = api_pending[k]
                            seg_map[src] = zh
                            pairs[src] = zh
                            api_responded.add(k)
                        append_cache(pairs)
                        # 部分結果：只重試缺失的 key（小批次並發補救）
                        got_keys = set(res.keys())
                        missing_items = [(k, txt) for k, txt in b if k not in got_keys]
                        if missing_items:
                            log_fn(f"[批次{bidx}] 部分成功 {len(res)}/{len(b)}，補翻 {len(missing_items)} 條...")
                            _retry_translate_small_batches(missing_items, api_pending, seg_map, log_fn)
                            for k, _ in missing_items:
                                if api_pending[k] in seg_map:
                                    api_responded.add(k)
                    except Exception as e:
                        log_fn(f"[批次{bidx}失敗] {e}，小批次並發重試...")
                        _retry_translate_small_batches(b, api_pending, seg_map, log_fn)
                        for k, _ in b:
                            if api_pending[k] in seg_map:
                                api_responded.add(k)

                    done_batches += 1
                    if progress_fn:
                        progress_fn(done_batches, len(batches), "翻譯")

        # ── 翻譯失敗偵測（基於是否收到 API 回應，而非比較翻譯內容） ──
        if api_pending and not (stop_check and stop_check()):
            failed_indices = [i for i in range(len(api_pending)) if i not in api_responded]
            failed_segs = [api_pending[i] for i in failed_indices]
            if failed_segs:
                fail_rate = len(failed_segs) / len(api_pending)
                # 先靜默重試一輪
                if fail_rate > 0.3:
                    log_fn(f"[翻譯補翻] {len(failed_segs)}/{len(api_pending)} 條未翻譯，自動補翻中...")
                    retry_items = [(i, api_pending[i]) for i in failed_indices]
                    _retry_translate_small_batches(retry_items, api_pending, seg_map, log_fn)
                    for i in failed_indices:
                        if api_pending[i] in seg_map:
                            api_responded.add(i)
                    failed_indices = [i for i in range(len(api_pending)) if i not in api_responded]
                    failed_segs = [api_pending[i] for i in failed_indices]
                    if failed_segs:
                        log_fn(f"[翻譯補翻] 補翻後仍有 {len(failed_segs)} 條未翻譯")
                    else:
                        log_fn(f"[翻譯補翻] 全部補翻成功！")
                    fail_rate = len(failed_segs) / len(api_pending) if api_pending else 0

                # 只有 90% 以上失敗才判定為 API 故障，彈窗詢問
                if fail_rate > 0.9 and ask_fn:
                    log_fn(f"[翻譯] 警告：{len(failed_segs)}/{len(api_pending)} 條翻譯失敗 ({fail_rate:.0%})")
                    while True:
                        user_choice = ask_fn(
                            f"翻譯遇到 API 問題，大量內容未翻譯。\n\n"
                            f"已翻譯：{len(api_pending) - len(failed_segs)}/{len(api_pending)} 條\n"
                            f"未翻譯：{len(failed_segs)} 條\n\n"
                            f"點「是」→ 立即重試（等 API 恢復後點）\n"
                            f"點「否」→ 停止處理，不輸出文件"
                        )
                        if not user_choice:
                            raise _ProgressSaved(f"翻譯中斷（{len(api_pending) - len(failed_segs)}/{len(api_pending)} 已翻譯）")

                        log_fn(f"[翻譯] 重試 {len(failed_segs)} 條...")
                        retry_items = [(i, api_pending[i]) for i in failed_indices]
                        _retry_translate_small_batches(retry_items, api_pending, seg_map, log_fn)
                        for i in failed_indices:
                            if api_pending[i] in seg_map:
                                api_responded.add(i)
                        failed_indices = [i for i in range(len(api_pending)) if i not in api_responded]
                        failed_segs = [api_pending[i] for i in failed_indices]
                        if not failed_segs:
                            log_fn(f"[翻譯] 全部翻譯完成！")
                            break
                        fail_rate = len(failed_segs) / len(api_pending)
                        if fail_rate <= 0.3:
                            log_fn(f"[翻譯] 剩餘 {len(failed_segs)} 條未翻譯，繼續處理")
                            break

    # ── 組裝結果（所有文字統一後處理：假名映射 + 用語本地化） ──
    # NOTE: 此段只在 enable_translate=True 時執行（跳過翻譯模式已在上面提前組裝）
    def _post_fix(s: str) -> str:
        s = replace_japanese_fixed(s)
        s = replace_katakana_to_latin(s)
        s = _apply_cn_tw_terms(s)  # 硬轉繁體的大陸用語→台灣用語
        s = _apply_english_to_chinese(s)  # 英文寶石名/材質名→中文
        s = _cleanup_japanese_residual(s)  # 日文殘留句式清理
        s = _normalize_excel_escapes(s)  # ★ _x000d_ → 空格
        s = _dedup_repeated_phrases(s)  # ★ 連續重複片段去重 (品牌名兜底)
        return s

    if CFG.enable_translate:
        out["標題（繁體）"] = [
            _post_fix("".join([seg_map.get(s, s) for s in segs])) if segs else ""
            for segs in title_segs
        ]
        out["說明（繁體）"] = [
            _post_fix("\n".join([seg_map.get(s, s) for s in segs if s])) if segs else ""
            for segs in desc_segs
        ]

    # ── Step 2: 假名清除（僅在不開SEO時執行，開SEO的話在SEO後統一清） ──
    # ★ 用 gpt-5.5 後直譯就乾淨, 1 輪兜底足矣 (原本 3 輪是 mini 弱才需要)
    if CFG.enable_translate and CFG.enable_kana_cleanup and not CFG.enable_seo:
        MAX_KANA_ROUNDS = 1
        for round_num in range(1, MAX_KANA_ROUNDS + 1):
            kana_titles = []
            kana_indices = []
            for idx, title in enumerate(out["標題（繁體）"].tolist()):
                if title and has_kana(title):
                    kana_titles.append(title)
                    kana_indices.append(idx)
            if not kana_titles:
                break
            if stop_check and stop_check():
                return out
            # 每輪縮小批次提高精度
            round_batch = max(4, CFG.batch_size // (round_num * 2))
            log_fn(f"[假名清除 第{round_num}輪] {len(kana_titles)} 條殘留假名，批次={round_batch}")
            saved_batch = CFG.batch_size
            try:
                CFG.batch_size = round_batch
                fixed = _cleanup_kana_batch(kana_titles, log_fn)
            finally:
                CFG.batch_size = saved_batch
            fix_count = 0
            for idx, fixed_title in zip(kana_indices, fixed):
                if fixed_title and not has_kana(fixed_title):
                    _learn_from_kana_fix(kana_titles[kana_indices.index(idx)], fixed_title)
                    out.at[idx, "標題（繁體）"] = fixed_title
                    fix_count += 1
            log_fn(f"[假名清除 第{round_num}輪] 修復 {fix_count}/{len(kana_titles)} 條")
            if fix_count == 0:
                log_fn("[假名清除] 本輪無新修復，停止重試")
                break

    # ── Step 3: SEO 優化（含關鍵詞提取 + 重試） ──
    if CFG.enable_seo:
        if stop_check and stop_check():
            return out
        translated_titles = out["標題（繁體）"].fillna("").astype(str).tolist()

        # 抽取商品簡述結構化屬性 + Phase 29 類別推斷 profile 提示
        # Phase 23 咸鱼 14k 實測 88.5% 商品含結構化屬性, 含窯口/釉色/年代等
        # Phase 29 實測 63 類 popular 池主詞候選 → AI 查對應 profile 不再亂選主詞
        summaries = out["商品簡述"].fillna("").astype(str).tolist() if "商品簡述" in out.columns else ['']*len(translated_titles)
        seo_attrs = [extract_seo_attrs(s, t) for s, t in zip(summaries, translated_titles)]
        # 抽 detail 摘要 (前 200 字, 用於 SEO 抽大師名/年代/規格)
        details_raw = out["說明"].fillna("").astype(str).tolist() if "說明" in out.columns else ['']*len(translated_titles)
        seo_details = [d.replace('\n', ' ').replace('\r', ' ')[:200] for d in details_raw]
        attr_count = sum(1 for a in seo_attrs if a)
        det_count = sum(1 for d in seo_details if len(d) >= 20)
        cat_hint_count = sum(1 for a in seo_attrs if '類別:' in a)
        if attr_count:
            log_fn(f"[SEO屬性] 商品簡述 {attr_count} + 類別 {cat_hint_count} + 說明摘要 {det_count}")

        # 嘗試從進度文件恢復 SEO 狀態
        _resumed_seo = False
        seo_keywords = None
        seo_secondary = None
        seo_titles = None
        responded_keys = None

        if out_path:
            prog = load_progress(out_path)
            if prog and prog.get("stage") == "seo" and prog.get("row_count") == len(translated_titles):
                done_count = len(prog.get('responded_keys', set()))
                # 完成數太少（<10%）時不恢復，重新做關鍵詞提取 + SEO
                if done_count >= max(1, len(translated_titles) // 10):
                    log_fn(f"[進度恢復] 偵測到上次 SEO 進度：{done_count}/{len(translated_titles)} 條已完成")
                    seo_keywords = prog["seo_keywords"]
                    seo_secondary = prog["seo_secondary"]
                    seo_titles = prog["seo_titles"]
                    responded_keys = prog["responded_keys"]
                    _resumed_seo = True
                else:
                    log_fn(f"[進度恢復] 上次進度僅 {done_count}/{len(translated_titles)} 條，重新開始")
                    clear_progress(out_path)

        if not _resumed_seo:
            # Step 3a: 提取搜索關鍵詞 (傳入商品簡述屬性讓 AI 做更精準判斷)
            seo_keywords, seo_secondary = _extract_seo_keywords(
                translated_titles, log_fn, progress_fn, attrs=seo_attrs)

            if stop_check and stop_check():
                return out

            # Step 3b: SEO標題優化 (傳入屬性 + 說明摘要)
            seo_titles, responded_keys = _seo_optimize_titles(
                translated_titles, log_fn, progress_fn,
                keywords=seo_keywords, secondary_keywords=seo_secondary,
                attrs=seo_attrs, details=seo_details)

        # ── SEO 重試 + 進度保存機制 ──
        def _do_seo_retry(retry_items):
            """執行一輪 SEO 重試，返回補救數量"""
            r_kws = [seo_keywords[i] if i < len(seo_keywords) else "" for i, _ in retry_items]
            r_sec = [seo_secondary[i] if i < len(seo_secondary) else "" for i, _ in retry_items]
            r_attrs = [seo_attrs[i] if i < len(seo_attrs) else "" for i, _ in retry_items]
            r_details = [seo_details[i] if i < len(seo_details) else "" for i, _ in retry_items]
            r_titles = [t for _, t in retry_items]
            r_results, r_responded = _seo_optimize_titles(
                r_titles, log_fn, progress_fn,
                keywords=r_kws, secondary_keywords=r_sec, attrs=r_attrs, details=r_details)
            fixed = 0
            for j, ((orig_idx, _), seo) in enumerate(zip(retry_items, r_results)):
                if j in r_responded and seo != translated_titles[orig_idx]:
                    seo_titles[orig_idx] = seo
                    responded_keys.add(orig_idx)
                    fixed += 1
            return fixed

        def _save_seo_progress():
            """保存當前 SEO 進度"""
            if out_path:
                save_progress(out_path, {
                    "stage": "seo",
                    "row_count": len(translated_titles),
                    "seo_keywords": seo_keywords,
                    "seo_secondary": seo_secondary,
                    "seo_titles": seo_titles,
                    "responded_keys": responded_keys,
                })

        def _get_missing():
            return [(i, translated_titles[i]) for i in range(len(translated_titles))
                    if translated_titles[i].strip() and i not in responded_keys]

        total_non_empty = sum(1 for t in translated_titles if t.strip())

        # 第一輪自動重試（不問用戶）
        retry_items = _get_missing()
        if retry_items and not (stop_check and stop_check()):
            fail_rate = len(retry_items) / total_non_empty if total_non_empty else 0
            if fail_rate > 0.5:
                wait_secs = 30
                log_fn(f"[SEO重試] {len(retry_items)}/{total_non_empty} 條未回應 ({fail_rate:.0%})，等待 {wait_secs}s 後重試...")
                for _w in range(wait_secs):
                    if stop_check and stop_check():
                        break
                    time.sleep(1)
            if not (stop_check and stop_check()):
                log_fn(f"[SEO重試] 重試 {len(retry_items)} 條...")
                fixed = _do_seo_retry(retry_items)
                log_fn(f"[SEO重試] 補救 {fixed}/{len(retry_items)} 條")

        # 檢查是否需要問用戶
        retry_items = _get_missing()
        if retry_items and not (stop_check and stop_check()):
            fail_rate = len(retry_items) / total_non_empty if total_non_empty else 0

            if fail_rate > 0.1 and ask_fn:
                # 有顯著失敗 → 保存進度，問用戶
                _save_seo_progress()
                while True:
                    user_choice = ask_fn(
                        f"SEO 優化遇到 API 問題，部分商品未完成。\n\n"
                        f"已完成：{total_non_empty - len(retry_items)}/{total_non_empty} 條\n"
                        f"未完成：{len(retry_items)} 條\n\n"
                        f"點「是」→ 立即重試（等 API 恢復後點）\n"
                        f"點「否」→ 保存進度，下次可繼續"
                    )
                    if not user_choice:
                        _save_seo_progress()
                        log_fn(f"[SEO] 進度已保存，{len(retry_items)} 條待完成。下次執行相同文件時將自動恢復。")
                        # 不輸出不完整的結果 — 拋出特殊異常讓 GUI 處理
                        raise _ProgressSaved(f"SEO 進度已保存（{total_non_empty - len(retry_items)}/{total_non_empty} 完成）")

                    # 用戶選擇重試
                    log_fn(f"[SEO] 重試 {len(retry_items)} 條...")
                    fixed = _do_seo_retry(retry_items)
                    log_fn(f"[SEO重試] 補救 {fixed}/{len(retry_items)} 條")
                    _save_seo_progress()

                    retry_items = _get_missing()
                    if not retry_items:
                        log_fn(f"[SEO] 全部完成！")
                        break
                    fail_rate = len(retry_items) / total_non_empty if total_non_empty else 0
                    if fail_rate <= 0.1:
                        log_fn(f"[SEO] 剩餘 {len(retry_items)} 條未回應（{fail_rate:.0%}），繼續處理")
                        break

        # 完成後清理進度文件
        if out_path:
            clear_progress(out_path)

        # Step 3c: SEO標題假名清除（SEO生成可能從翻譯標題帶入殘留假名）
        if CFG.enable_kana_cleanup:
            seo_kana_indices = []
            seo_kana_titles = []
            for idx, t in enumerate(seo_titles):
                if t and has_kana(t):
                    seo_kana_indices.append(idx)
                    seo_kana_titles.append(t)
            if seo_kana_titles:
                log_fn(f"[SEO假名清除] {len(seo_kana_titles)} 條SEO標題仍含假名，清除中...")
                # ★ 5.5 模型下殘留稀少, 1 輪兜底足夠 (原本 2 輪是 mini 弱才需要)
                for round_num in range(1, 2):
                    if not seo_kana_titles:
                        break
                    if stop_check and stop_check():
                        break
                    round_batch = max(4, CFG.batch_size // (round_num * 2))
                    saved_batch = CFG.batch_size
                    try:
                        CFG.batch_size = round_batch
                        fixed_seo = _cleanup_kana_batch(seo_kana_titles, log_fn)
                    finally:
                        CFG.batch_size = saved_batch
                    still_kana_indices = []
                    still_kana_titles = []
                    fix_count = 0
                    for bi, (idx, fixed_t) in enumerate(zip(seo_kana_indices, fixed_seo)):
                        if fixed_t and not has_kana(fixed_t):
                            _learn_from_kana_fix(seo_kana_titles[bi], fixed_t)
                            seo_titles[idx] = fixed_t
                            fix_count += 1
                        elif fixed_t and has_kana(fixed_t):
                            still_kana_indices.append(idx)
                            still_kana_titles.append(fixed_t)
                    log_fn(f"[SEO假名清除 第{round_num}輪] 修復 {fix_count}/{len(seo_kana_titles)} 條")
                    seo_kana_indices = still_kana_indices
                    seo_kana_titles = still_kana_titles
                    if fix_count == 0:
                        break

        # Step 3d: Yahoo 算法驗證 + 自動修復（代碼級硬性保障，不依賴 AI）
        fix_stats = {"kw_inserted": 0, "dedup": 0, "kana_fixed": 0, "moved_to_start": 0, "space_added": 0}
        for idx in range(len(seo_titles)):
            t = seo_titles[idx]
            if not t or not t.strip():
                continue
            pk = seo_keywords[idx] if idx < len(seo_keywords) else ""
            sk = seo_secondary[idx] if idx < len(seo_secondary) else ""

            # 清洗關鍵詞本身（去假名、套用映射）
            if pk:
                pk = _post_fix(pk)
                if has_kana(pk):
                    pk = ""  # 含假名的關鍵詞不能用，會污染標題
            if sk:
                sk = _post_fix(sk)
                if has_kana(sk):
                    sk = ""

            # 4a: 套用 fixed map（清除舊快取/SEO生成的殘留假名詞）
            t_before = t
            t = _post_fix(t)
            if t != t_before:
                fix_stats["kana_fixed"] += 1

            # 4b: 算法自動修復（關鍵詞插入、位置調整、去重、加空格、防撞池）
            # context = 原題 + 屬性, 讓 auto_fix 知道朝代/產地來判斷主詞該加什麼前綴
            t_before = t
            _ctx = ''
            try:
                orig_title = out.iloc[idx].get('標題', '') if idx < len(out) else ''
                orig_attr = seo_attrs[idx] if idx < len(seo_attrs) else ''
                _ctx = f'{orig_title} {orig_attr}'
            except Exception:
                pass
            t = auto_fix_seo_title(t, pk, sk, context=_ctx)
            if t != t_before:
                # 判斷修了什麼
                if " " not in t_before.strip() and " " in t.strip():
                    fix_stats["space_added"] += 1
                if pk and pk not in t_before:
                    fix_stats["kw_inserted"] += 1
                elif pk and not t_before.startswith(pk):
                    fix_stats["moved_to_start"] += 1
                parts_before = t_before.split()
                if len(parts_before) != len(set(parts_before)):
                    fix_stats["dedup"] += 1

            seo_titles[idx] = t

        # 4c: 品質評分統計（用清洗後的關鍵詞評分，與修復階段一致）
        scores = []
        low_score_samples = []
        for idx in range(len(seo_titles)):
            t = seo_titles[idx]
            if not t or not t.strip():
                continue
            pk = seo_keywords[idx] if idx < len(seo_keywords) else ""
            sk = seo_secondary[idx] if idx < len(seo_secondary) else ""
            # 清洗關鍵詞（與 4a/4b 修復階段保持一致）
            if pk:
                pk = _post_fix(pk)
                if has_kana(pk):
                    pk = ""
            if sk:
                sk = _post_fix(sk)
                if has_kana(sk):
                    sk = ""
            result = yahoo_relevancy_score(t, pk, sk)
            scores.append(result["score"])
            if result["score"] < 60 and len(low_score_samples) < 5:
                low_score_samples.append((idx, t, pk, sk, result))

        if scores:
            avg = sum(scores) / len(scores)
            perfect = sum(1 for s in scores if s >= 80)
            good = sum(1 for s in scores if 60 <= s < 80)
            poor = sum(1 for s in scores if s < 60)
            log_fn(f"[Yahoo算法驗證] 平均分：{avg:.1f}/100 | 優秀(≥80)：{perfect} | 良好(60-79)：{good} | 需改進(<60)：{poor}")
            log_fn(f"[自動修復] 空格斷字：{fix_stats['space_added']} | 關鍵詞插入：{fix_stats['kw_inserted']} | 移至開頭：{fix_stats['moved_to_start']} | 去重複：{fix_stats['dedup']} | 假名修復：{fix_stats['kana_fixed']}")
            if low_score_samples:
                log_fn(f"[低分樣本]")
                for idx, t, pk, sk, r in low_score_samples:
                    log_fn(f"  [{idx}] ({r['score']:.0f}分) 主={pk} 副={sk} → {t[:50]}")
                    for iss in r["issues"]:
                        log_fn(f"       ⚠ {iss}")

        out["SEO標題"] = seo_titles

        # 將評分寫入 DataFrame, 供外部過濾使用
        all_scores = []
        for idx in range(len(seo_titles)):
            t = seo_titles[idx]
            if not t or not t.strip():
                all_scores.append(0.0)
                continue
            pk = seo_keywords[idx] if idx < len(seo_keywords) else ""
            sk = seo_secondary[idx] if idx < len(seo_secondary) else ""
            if pk:
                pk = _post_fix(pk)
                if has_kana(pk): pk = ""
            if sk:
                sk = _post_fix(sk)
                if has_kana(sk): sk = ""
            all_scores.append(yahoo_relevancy_score(t, pk, sk)["score"])
        out["_seo_score"] = all_scores

    # ── 商品簡述映射 ──
    if "商品簡述" in out.columns:
        out["商品簡述"] = out["商品簡述"].fillna("").astype(str).map(apply_fixed_summary)
    elif "商品简述" in out.columns:
        out["商品简述"] = out["商品简述"].fillna("").astype(str).map(apply_fixed_summary)

    # ── 覆蓋原欄位，刪除中間列 ──
    if "SEO標題" in out.columns:
        out[tcol] = out["SEO標題"]
        out.drop(columns=["SEO標題"], inplace=True)
    elif "標題（繁體）" in out.columns:
        out[tcol] = out["標題（繁體）"]
    if "標題（繁體）" in out.columns:
        out.drop(columns=["標題（繁體）"], inplace=True)
    if "說明（繁體）" in out.columns:
        out[dcol] = out["說明（繁體）"]
        out.drop(columns=["說明（繁體）"], inplace=True)

    return out

# ──────────────────── Excel 工具 ────────────────────
TITLE_SYNONYMS = [
    "標題", "标题", "商品標題", "商品标题", "title", "name", "商品名",
    "品名", "タイトル", "商品タイトル", "商品名稱", "商品名称"
]
DESC_SYNONYMS = [
    "說明", "说明", "商品說明", "商品说明", "描述", "詳情", "详细",
    "description", "desc", "內容", "内容", "説明", "商品説明",
    "商品資訊", "商品信息"
]

def resolve_column_name(df: pd.DataFrame, preferred: str, candidates: List[str]) -> str:
    cols = list(df.columns)
    norm = lambda s: "".join(str(s).strip().lower().split())
    nmap = {norm(c): c for c in cols}
    if preferred and norm(preferred) in nmap:
        return nmap[norm(preferred)]
    for c in candidates:
        if norm(c) in nmap:
            return nmap[norm(c)]
    raise KeyError(f"找不到需要的列。可用列：{cols}")

def load_first_sheet(path: str, sheet_name=None) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path, dtype=str, encoding="utf-8")
    if sheet_name is None:
        xls = pd.ExcelFile(path, engine="openpyxl")
        first = xls.sheet_names[0]
        df = pd.read_excel(xls, sheet_name=first, dtype=str, engine="openpyxl")
    else:
        df = pd.read_excel(path, sheet_name=sheet_name, dtype=str, engine="openpyxl")
    return df


# ──────────────────── 品質篩選（Yahoo 排名分析） ────────────────────
# 非商品關鍵詞（交易/物流說明，不該上架的）
NON_PRODUCT_KEYWORDS = [
    "私聊", "下單", "特惠", "一口價", "直接拍",
    "請先", "聯繫", "不退", "專拍", "客製", "代拍",
    "訂金", "預留", "按圖", "亂拍", "私訊", "專用",
    "我想要", "私聊吧", "感興趣",
]

def quality_analyze(df: pd.DataFrame, tcol: str, log_fn: Callable = print,
                    custom_filter_words: Optional[List[str]] = None,
                    dedup: bool = True) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    對翻譯後的 DataFrame 做品質分析，分成四個表：
    1. good_df  — 合格商品（直接上架）
    2. warn_df  — 有瑕疵但可修改（標題偏短、無空格等）
    3. bad_df   — 問題商品（非商品、空標題、匹配自訂過濾詞）
    4. dup_df   — 重複商品（標題+簡述完全一樣，只保留第一條）

    custom_filter_words: 使用者自訂的過濾關鍵詞列表

    每個 DataFrame 新增欄位：
    - _品質分數: Yahoo 排名分數
    - _品質標籤: excellent/good/warning/bad
    - _問題說明: 問題原因
    """
    titles = df[tcol].fillna("").astype(str).tolist()
    filter_words = list(NON_PRODUCT_KEYWORDS)
    if custom_filter_words:
        filter_words.extend(custom_filter_words)

    scores_col = []
    labels_col = []
    issues_col = []
    cleaned_titles = []  # 清理後的標題（去掉過濾詞）

    for idx, title in enumerate(titles):
        issues = []
        label = "excellent"

        if not title.strip():
            scores_col.append(0)
            labels_col.append("bad")
            issues_col.append("空標題")
            cleaned_titles.append(title)
            continue

        # Yahoo 評分
        words = title.split()
        pk = words[0] if words else ""
        sk = words[1] if len(words) > 1 else ""
        sc = yahoo_relevancy_score(title, pk, sk)
        score = float(sc["score"])

        # 非商品檢測（智能判斷：少量過濾詞在長標題中→清理；大量或短標題→問題商品）
        matched_kw = [kw for kw in filter_words if kw in title]
        if matched_kw:
            real_len = len(title) - sum(len(kw) for kw in matched_kw)
            if len(matched_kw) >= 2 or real_len < 8:
                # 多個過濾詞或去掉後內容太少 → 非商品
                issues.append(f"匹配過濾詞：{', '.join(matched_kw[:3])}")
                label = "bad"
            else:
                # 只有1個過濾詞且剩餘內容充足 → 自動清理該詞
                cleaned = title
                for kw in matched_kw:
                    cleaned = cleaned.replace(kw, "")
                cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
                if cleaned != title:
                    issues.append(f"已自動清理詞：{matched_kw[0]}")
                    title = cleaned

        # 極短
        if len(title) <= 3:
            issues.append(f"極短標題（{len(title)}字）")
            if label != "bad":
                label = "bad"
        elif len(title) < 8:
            issues.append(f"標題偏短（{len(title)}字）")
            if label != "bad":
                label = "warning"

        # 純數字
        if re.match(r'^[\d\s\.\-\+\,]+$', title):
            issues.append("純數字/符號標題")
            label = "bad"

        # 假名
        if has_kana(title):
            issues.append("含日文假名")
            if label != "bad":
                label = "warning"

        # 無空格
        if " " not in title.strip() and len(title) > 5:
            issues.append("無空格分詞")
            if label not in ("bad", "warning"):
                label = "warning"

        # 分數分級
        if label not in ("bad", "warning"):
            if score >= 80:
                label = "excellent"
            elif score >= 60:
                label = "good"
            else:
                label = "warning"
                issues.append(f"低分（{score:.0f}分）")

        scores_col.append(score)
        labels_col.append(label)
        issues_col.append("；".join(issues) if issues else "")
        cleaned_titles.append(title)  # 可能已被清理

    df_out = df.copy()
    df_out[tcol] = cleaned_titles  # 寫回清理後的標題
    df_out["_品質分數"] = scores_col
    df_out["_品質標籤"] = labels_col
    df_out["_問題說明"] = issues_col

    good_mask = df_out["_品質標籤"].isin(["excellent", "good"])
    warn_mask = df_out["_品質標籤"] == "warning"
    bad_mask = df_out["_品質標籤"] == "bad"

    good_df = df_out[good_mask].copy()
    warn_df = df_out[warn_mask].copy()
    bad_df = df_out[bad_mask].copy()

    # 去重：標題相同的商品 → 檢查條碼
    #   條碼不同 = 不同商品，標題加後綴 (1)(2) 區分，保留在合格表
    #   條碼也相同（或無條碼欄）= 真正重複，移到重複表
    dup_df = pd.DataFrame()
    barcode_col = None
    BARCODE_SYNONYMS = ["商品條碼", "商品条码", "條碼", "条码", "barcode", "Barcode", "BARCODE",
                        "商品編號", "商品编号", "SKU", "sku"]
    if dedup:
        for syn in BARCODE_SYNONYMS:
            if syn in good_df.columns:
                barcode_col = syn
                break

        # 找出標題重複的行
        dup_title_mask = good_df[tcol].duplicated(keep=False)  # 所有重複的都標記
        if dup_title_mask.any() and barcode_col:
            # 有條碼欄：按標題分組，條碼不同的加後綴保留，條碼也相同的才算真重複
            suffix_count = 0
            real_dup_indices = []
            new_titles = good_df[tcol].tolist()
            indices = good_df.index.tolist()

            # 按標題分組
            title_groups: dict = {}  # title -> [(list_pos, df_index, barcode)]
            for i, idx in enumerate(indices):
                title = new_titles[i]
                bc = str(good_df.at[idx, barcode_col]).strip() if barcode_col else ""
                if bc == "nan":
                    bc = ""
                title_groups.setdefault(title, []).append((i, idx, bc))

            for title, group in title_groups.items():
                if len(group) <= 1:
                    continue
                # 按條碼再分組
                bc_seen: dict = {}  # barcode -> first list_pos
                for list_pos, df_idx, bc in group:
                    if bc and bc in bc_seen:
                        # 條碼相同 → 真正重複，移除
                        real_dup_indices.append(df_idx)
                    elif bc:
                        # 條碼不同 → 不同商品，需要加後綴區分
                        if bc not in bc_seen:
                            bc_seen[bc] = list_pos
                    else:
                        # 無條碼 → 按舊邏輯視為重複
                        if bc_seen:
                            real_dup_indices.append(df_idx)
                        else:
                            bc_seen[""] = list_pos

                # 對同標題但不同條碼的商品加後綴 (1)(2)...
                diff_bc_items = [(lp, di) for lp, di, bc in group if di not in real_dup_indices]
                if len(diff_bc_items) > 1:
                    for seq, (list_pos, df_idx) in enumerate(diff_bc_items[1:], 1):
                        new_titles[list_pos] = f"{new_titles[list_pos]} ({seq})"
                        suffix_count += 1

            good_df[tcol] = new_titles

            if real_dup_indices:
                dup_mask = good_df.index.isin(real_dup_indices)
                dup_df = good_df[dup_mask].copy()
                dup_df["_問題說明"] = "重複商品（標題與條碼均相同）"
                good_df = good_df[~dup_mask].copy()

            if suffix_count > 0:
                log_fn(f"[品質篩選] {suffix_count} 條標題相同但條碼不同，已加後綴區分保留")
        elif dup_title_mask.any():
            # 無條碼欄，按舊邏輯：標題重複直接移除
            dup_mask_good = good_df[tcol].duplicated(keep="first")
            if dup_mask_good.any():
                dup_df = good_df[dup_mask_good].copy()
                dup_df["_問題說明"] = "重複標題（已保留第一條）"
                good_df = good_df[~dup_mask_good].copy()

    log_fn(f"[品質篩選] 合格: {len(good_df)} | 警告: {len(warn_df)} | 問題: {len(bad_df)} | 重複: {len(dup_df)}")
    cleaned_count = sum(1 for x in issues_col if "已自動清理詞" in x)
    if cleaned_count > 0:
        log_fn(f"[品質篩選] 已自動清理 {cleaned_count} 條標題中的過濾詞（保留為合格）")
    if len(dup_df) > 0:
        log_fn(f"[品質篩選] 重複商品 {len(dup_df)} 條（標題與條碼均相同）已移至「重複商品」分頁")
    if len(bad_df) > 0:
        log_fn(f"[品質篩選] 問題商品已移至「問題商品」分頁")
    if len(warn_df) > 0:
        log_fn(f"[品質篩選] 有瑕疵商品已移至「需注意」分頁")

    return good_df, warn_df, bad_df, dup_df
