# -*- coding: utf-8 -*-
"""
自動映射處理器
當出現未映射的煤爐分類時:
  1. 查 煤爐分類完整.xlsx 取得日文路徑
  2. 用關鍵詞召回奇摩分類候選
  3. GPT選出最佳匹配
  4. 輸出新映射行 (格式同 煤爐映射奇摩分類.xlsx)
"""
import json
import re
import time
import pandas as pd
from collections import Counter
from typing import Callable, Dict, List, Tuple, Optional

LogFn = Callable[[str], None]

MAPPING_HEADERS = ['煤爐ID', '煤爐中文分類', '煤爐日文分類', '奇摩ID', '奇摩分類']


# ─── 數據載入 ───

def load_full_mercari(xlsx_path: str, log_fn: LogFn = print) -> Dict[int, Tuple[str, str]]:
    """
    讀取 煤爐分類完整.xlsx
    返回 {cat_id: (name, path)}
    """
    df = pd.read_excel(xlsx_path, engine='openpyxl')
    result = {}
    for _, row in df.iterrows():
        try:
            cid = int(row['cat_id'])
            name = str(row.get('name', ''))
            path = str(row.get('path', ''))
            result[cid] = (name, path)
        except (ValueError, TypeError):
            continue
    log_fn(f"煤爐完整分類表: {len(result)} 條")
    return result


def load_yahoo_categories(xlsx_path: str, log_fn: LogFn = print) -> List[dict]:
    """
    讀取 奇摩分類.xlsx
    返回 [{'id': str, 'leaf': str, 'path': str}, ...]
    """
    df = pd.read_excel(xlsx_path, engine='openpyxl')
    cats = []
    for _, row in df.iterrows():
        yid = row.get('ID', '')
        leaf = row.get('最末級別', '')
        path = row.get('完整級', '')
        if pd.notna(yid) and pd.notna(path):
            cats.append({
                'id': str(int(yid)) if isinstance(yid, float) else str(yid),
                'leaf': str(leaf) if pd.notna(leaf) else '',
                'path': str(path),
            })
    log_fn(f"奇摩分類表: {len(cats)} 條")
    return cats


# ─── CJK 分詞 + 候選召回 ───

# 日文→中文常見分類詞對照 (提高跨語言召回率)
JP_ZH_MAP = {
    'キッチン': '廚房', 'テーブル': '餐桌', '食器': '餐具', '用品': '用品',
    'ファッション': '服飾', 'レディース': '女性', 'メンズ': '男性',
    'アクセサリー': '飾品配件', 'ジュエリー': '珠寶', 'バッグ': '包',
    'シューズ': '鞋', 'インテリア': '家居', '家具': '家具',
    'おもちゃ': '玩具', 'ゲーム': '遊戲', 'ドール': '娃娃', '人形': '人偶',
    'ハンドメイド': '手工藝', '手芸': '手工', 'ビーズ': '串珠',
    '素材': '材料', '材料': '材料', '道具': '工具',
    'フラワー': '花卉', 'ガーデニング': '園藝', '観葉植物': '觀葉植物',
    '楽器': '樂器', '音楽': '音樂', 'スポーツ': '運動',
    '美術': '美術', '芸術': '藝術', 'アート': '藝術',
    'アンティーク': '古董', 'コレクション': '收藏',
    '自動車': '汽車', 'バイク': '機車', '自転車': '自行車',
    '本': '書', '雑誌': '雜誌', '漫画': '漫畫',
    'カメラ': '相機', '家電': '家電', 'スマホ': '手機',
    'パソコン': '電腦', 'タブレット': '平板', '腕時計': '手錶',
    'コスメ': '化妝品', '香水': '香水', 'ペット': '寵物',
    '食品': '食品', '飲料': '飲料', '酒': '酒',
    '陶芸': '陶藝', '茶道': '茶道', '花瓶': '花瓶',
    '皿': '盤', 'カップ': '杯', 'グラス': '杯',
    '置物': '擺飾', '雑貨': '雜貨', '収納': '收納',
}


def _tokenize(text: str) -> List[str]:
    """CJK 2-gram + 英數詞分割 + 日中對照展開"""
    tokens = []
    parts = re.split(r'[>\-\s・、/→,]+', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 英數詞完整保留
        for word in re.findall(r'[a-zA-Z0-9]+', part):
            if len(word) >= 2:
                tokens.append(word.lower())
        # CJK 2-gram
        cjk = re.sub(r'[a-zA-Z0-9\s]+', '', part)
        for i in range(len(cjk) - 1):
            tokens.append(cjk[i:i+2])
        # 完整CJK段 (短的有辨識度)
        if 2 <= len(cjk) <= 6:
            tokens.append(cjk)
    # 日文→中文展開
    for jp, zh in JP_ZH_MAP.items():
        if jp in text:
            zh_tokens = [zh[i:i+2] for i in range(len(zh)-1)] if len(zh) > 1 else [zh]
            tokens.extend(zh_tokens)
            if 2 <= len(zh) <= 6:
                tokens.append(zh)
    # 保留片假名外來語原文 (如 ブライス→Blythe 的匹配靠完整段)
    kata_segs = re.findall(r'[\u30A0-\u30FF]{3,}', text)
    for seg in kata_segs:
        tokens.append(seg)
    return tokens


def _recall_candidates(mercari_path: str, yahoo_cats: List[dict],
                       top_k: int = 20) -> List[dict]:
    """用關鍵詞匹配從奇摩分類中召回 top_k 候選"""
    q_tokens = set(_tokenize(mercari_path))
    if not q_tokens:
        return yahoo_cats[:top_k]

    scored = []
    for cat in yahoo_cats:
        cat_tokens = set(_tokenize(cat['path']))
        overlap = len(q_tokens & cat_tokens)
        if overlap > 0:
            scored.append((overlap, cat))

    scored.sort(key=lambda x: -x[0])
    return [cat for _, cat in scored[:top_k]]


def _gpt_translate_keywords(mercari_path: str, api_config: dict,
                            log_fn: LogFn = print) -> List[str]:
    """讓GPT把日文路徑翻成中文搜索關鍵詞，用於更精準的召回"""
    try:
        import requests
    except ImportError:
        from curl_cffi import requests

    api_url = api_config.get('translate_url', '')
    api_key = api_config.get('translate_key', '')
    model = api_config.get('translate_model', 'gpt-5')

    if not api_url or not api_key:
        return []

    prompt = (
        f"把以下日文商品分類路徑翻譯成繁體中文搜索關鍵詞，用於搜索Yahoo奇摩拍賣的商品分類。\n"
        f"日文: {mercari_path}\n"
        f"要求:\n"
        f"1. 輸出5-8個繁中關鍵詞，用逗號分隔，不要其他文字\n"
        f"2. 包含: 直譯詞 + 台灣常用同義詞 + 上位概念詞\n"
        f"3. 英文品牌名/專有名詞保留原文(如Blythe, LEGO)\n"
        f"4. 關鍵詞要適合搜索Yahoo拍賣分類(如: 居家、手錶與飾品配件、玩具模型等)\n"
        f"例: 「ファッション -> レディース -> アクセサリー」\n"
        f"  → 「女性,流行飾品,女性流行飾品,飾品配件,配件,手錶與飾品配件」"
    )

    url = api_url.rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'temperature': 0,
        'max_tokens': 100,
        'messages': [{'role': 'user', 'content': prompt}],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            content = resp.json()['choices'][0]['message']['content'].strip()
            keywords = [kw.strip() for kw in content.split(',') if kw.strip()]
            log_fn(f"    翻譯關鍵詞: {', '.join(keywords)}")
            return keywords
    except Exception:
        pass
    return []


def _enhanced_recall(mercari_path: str, yahoo_cats: List[dict],
                     zh_keywords: List[str], top_k: int = 20) -> List[dict]:
    """
    增強版召回: 日文token + 中文翻譯關鍵詞，雙路召回合併
    """
    # 路線1: 原始日文token匹配
    jp_tokens = set(_tokenize(mercari_path))

    # 路線2: GPT翻譯的中文關鍵詞
    zh_tokens = set()
    for kw in zh_keywords:
        zh_tokens.update(_tokenize(kw))
        # 完整詞也加入
        if len(kw) >= 2:
            zh_tokens.add(kw)

    all_query_tokens = jp_tokens | zh_tokens

    scored = []
    for cat in yahoo_cats:
        cat_text = cat['path'] + ' ' + cat['leaf']
        cat_tokens = set(_tokenize(cat_text))

        # 計算匹配分: 中文關鍵詞命中權重更高
        jp_overlap = len(jp_tokens & cat_tokens)
        zh_overlap = len(zh_tokens & cat_tokens)
        # 完整關鍵詞匹配 bonus
        full_kw_bonus = sum(3 for kw in zh_keywords if kw in cat_text)

        score = jp_overlap + zh_overlap * 2 + full_kw_bonus
        if score > 0:
            scored.append((score, cat))

    scored.sort(key=lambda x: -x[0])
    return [cat for _, cat in scored[:top_k]]


# ─── GPT 匹配 ───

def _gpt_match(mercari_id: int, mercari_path: str, candidates: List[dict],
               api_config: dict, log_fn: LogFn = print) -> Optional[dict]:
    """
    調用 GPT 從候選中選出最佳奇摩分類
    返回 {'yahoo_id': str, 'yahoo_path': str, 'chinese_path': str, 'confidence': int}
    """
    try:
        import requests
    except ImportError:
        try:
            from curl_cffi import requests
        except ImportError:
            log_fn("[錯誤] 需要 requests 或 curl_cffi 庫")
            return None

    api_url = api_config.get('translate_url', '')
    api_key = api_config.get('translate_key', '')
    model = api_config.get('seo_model', 'gpt-5')

    if not api_url or not api_key:
        log_fn("[跳過] 未配置API Key, 無法自動匹配")
        return None

    # 構建候選列表
    cand_lines = []
    for c in candidates:
        cand_lines.append(f"  id={c['id']} | leaf={c['leaf']} | path={c['path']}")
    cand_text = '\n'.join(cand_lines)

    system_prompt = (
        "你是「跨平台類目對齊」專家。任務：把 Mercari(日文) 類目，匹配到 Yahoo TW(繁中) 的末級類目。\n"
        "輸出必須是 JSON（不要多任何字）：\n"
        '{"best_id": "<string>", "confidence": <0-100>, "reason": "<short>", "chinese_path": "<日文路徑的繁體中文翻譯>"}\n'
        "規則：\n"
        "- 必須從候選列表中選一個 best_id。\n"
        "- 若都不太像，仍要選最接近的，但 confidence 要低。\n"
        "- 盡量利用「路徑」判斷語義，而不是只看末級名字。\n"
        "- chinese_path: 把日文路徑翻成繁中，格式用 -> 分隔，例如 '廚房用品 -> 餐具 -> 餐桌用品'"
    )

    user_prompt = (
        f"Mercari:\n"
        f"- id: {mercari_id}\n"
        f"- path: {mercari_path}\n\n"
        f"Yahoo candidates:\n{cand_text}"
    )

    url = api_url.rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                log_fn(f"  [API] HTTP {resp.status_code}, 重試...")
                time.sleep(1 * (attempt + 1))
                continue

            data = resp.json()
            content = data['choices'][0]['message']['content'].strip()
            # 清理markdown包裹
            content = re.sub(r'^```json\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            result = json.loads(content)
            return result

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            log_fn(f"  [API] 解析失敗: {e}, 重試...")
            time.sleep(1)
        except Exception as e:
            log_fn(f"  [API] 請求失敗: {e}, 重試...")
            time.sleep(1 * (attempt + 1))

    return None


# ─── 主入口 ───

def auto_map_missing(
    unmapped_cat_ids: List[int],
    full_cat_path: str,
    yahoo_cat_path: str,
    api_config: dict,
    log_fn: LogFn = print,
) -> pd.DataFrame:
    """
    自動匹配未映射的煤爐分類 → 奇摩分類
    返回 DataFrame, 列: 煤爐ID, 煤爐中文分類, 煤爐日文分類, 奇摩ID, 奇摩分類
    """
    if not unmapped_cat_ids:
        return pd.DataFrame(columns=MAPPING_HEADERS)

    # 去重
    unique_ids = sorted(set(unmapped_cat_ids))
    log_fn(f"\n自動映射: {len(unique_ids)} 個未映射分類")

    # 載入數據
    full_cats = load_full_mercari(full_cat_path, log_fn)
    yahoo_cats = load_yahoo_categories(yahoo_cat_path, log_fn)

    results = []
    for i, cid in enumerate(unique_ids):
        if cid not in full_cats:
            log_fn(f"  [{i+1}/{len(unique_ids)}] ID {cid}: 完整分類表中不存在, 跳過")
            continue

        name, jp_path = full_cats[cid]
        log_fn(f"  [{i+1}/{len(unique_ids)}] ID {cid}: {jp_path}")

        # 第一步: GPT翻譯日文→中文關鍵詞 (用便宜模型)
        zh_keywords = _gpt_translate_keywords(jp_path, api_config, log_fn)

        # 第二步: 增強召回 (日文token + 中文關鍵詞雙路)
        if zh_keywords:
            candidates = _enhanced_recall(jp_path, yahoo_cats, zh_keywords, top_k=20)
        else:
            candidates = _recall_candidates(jp_path, yahoo_cats, top_k=20)

        if not candidates:
            log_fn(f"    無候選, 跳過")
            continue

        # 第三步: GPT選出最佳匹配 (用強模型)
        match = _gpt_match(cid, jp_path, candidates, api_config, log_fn)
        if not match:
            log_fn(f"    GPT匹配失敗")
            continue

        yahoo_id = str(match.get('best_id', ''))
        confidence = match.get('confidence', 0)
        reason = match.get('reason', '')
        chinese_path = match.get('chinese_path', '')

        # 找奇摩完整路徑
        yahoo_full = ''
        for yc in yahoo_cats:
            if yc['id'] == yahoo_id:
                yahoo_full = yc['path']
                break

        log_fn(f"    → 奇摩 {yahoo_id}: {yahoo_full} (信心:{confidence}) {reason}")

        results.append({
            '煤爐ID': cid,
            '煤爐中文分類': chinese_path,
            '煤爐日文分類': jp_path,
            '奇摩ID': yahoo_id,
            '奇摩分類': yahoo_full,
        })

        # 禮貌延遲
        if i < len(unique_ids) - 1:
            time.sleep(0.5)

    df = pd.DataFrame(results, columns=MAPPING_HEADERS)
    log_fn(f"自動映射完成: 成功 {len(df)}/{len(unique_ids)} 條")
    return df
