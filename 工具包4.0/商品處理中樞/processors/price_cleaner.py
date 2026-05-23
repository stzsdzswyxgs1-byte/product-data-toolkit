# -*- coding: utf-8 -*-
"""
價格/數字清洗處理器
- 從標題和說明中刪除價格數字、促銷文字
- 支援兩個規則表: 只刪數字 / 全部刪除
- 保護帶單位的數字 (如 31cm, 50mm, 5號)
"""
import re
import pandas as pd
from typing import Callable, List

LogFn = Callable[[str], None]

# ── 保護段落標記 ──
_KEEP_L = "⟦KEEP⟧"
_KEEP_R = "⟦/KEEP⟧"

_DIG_ENCODE = str.maketrans({
    '0': '〇', '1': '一', '2': '二', '3': '三', '4': '四',
    '5': '五', '6': '六', '7': '七', '8': '八', '9': '九',
    '０': '〇', '１': '一', '２': '二', '３': '三', '４': '四',
    '５': '五', '６': '六', '７': '七', '８': '八', '９': '九',
})
_DIG_DECODE = str.maketrans({
    '〇': '0', '零': '0', '一': '1', '二': '2', '三': '3', '四': '4',
    '五': '5', '六': '6', '七': '7', '八': '8', '九': '9',
})

# 默認保護單位
_DEFAULT_PROTECT = ["mm", "cm", "g", "kg", "号", "號", "k", "K"]


def _build_protect_regexes(items: list) -> list:
    """構建保護正則列表"""
    regs = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        if raw.lower().startswith("re:"):
            try:
                regs.append(re.compile(raw[3:].strip()))
            except re.error:
                pass
            continue
        unit = re.escape(raw)
        if re.fullmatch(r"[A-Za-z]+", raw):
            pat = rf"(?i)(?<![0-9０-９])([0-9０-９]+(?:[\.．][0-9０-９]+)?)\s*{unit}\b"
        else:
            pat = rf"(?<![0-9０-９])([0-9０-９]+(?:[\.．][0-9０-９]+)?)\s*{unit}"
        try:
            regs.append(re.compile(pat))
        except re.error:
            pass
    return regs


def _protect_text(text: str, protect_regs: list) -> str:
    if not isinstance(text, str) or not text or not protect_regs:
        return text
    def _wrap(m):
        return f"{_KEEP_L}{m.group(0).translate(_DIG_ENCODE)}{_KEEP_R}"
    for reg in protect_regs:
        text = reg.sub(_wrap, text)
    return text


def _restore_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    def _un(m):
        return m.group(1).translate(_DIG_DECODE)
    return re.sub(re.escape(_KEEP_L) + r"(.*?)" + re.escape(_KEEP_R), _un, text, flags=re.S)


def _number_only_replacement(match):
    """只刪除捕獲組中的數字部分 (供預編譯正則使用)"""
    full = match.group(0)
    before = full[:match.start(1) - match.start(0)]
    after = full[match.end(1) - match.start(0):]
    return before + after


_YUAN_RE = re.compile(r'\d+\s*元')


def load_price_cleaner(number_removal_path: str, full_removal_path: str,
                       protect_path: str = '', log_fn: LogFn = print) -> dict:
    """
    載入清洗規則, 返回 dict:
      number_rules: list of regex patterns (只刪數字)
      full_rules: list of regex/string patterns (全部刪除)
      protect_regs: list of compiled regexes (保護規則)
    """
    result = {'number_rules': [], 'full_rules': [], 'protect_regs': []}

    # 只刪數字規則 (預編譯)
    if number_removal_path:
        try:
            df = pd.read_excel(number_removal_path, engine='openpyxl')
            raw = [str(x).strip() for x in df.iloc[:, 0].dropna().tolist() if str(x).strip()]
            compiled = []
            bad = 0
            for p in raw:
                try:
                    compiled.append(re.compile(p))
                except re.error:
                    bad += 1
            result['number_rules'] = compiled
            log_fn(f"只刪數字規則: {len(compiled)} 條" + (f" ({bad} 條無效已跳過)" if bad else ''))
        except Exception as e:
            log_fn(f"[警告] 讀取只刪數字規則失敗: {e}")

    # 全部刪除規則 (預編譯)
    if full_removal_path:
        try:
            df = pd.read_excel(full_removal_path, engine='openpyxl')
            raw = [str(x).strip() for x in df.iloc[:, 0].dropna().tolist() if str(x).strip()]
            compiled = []
            bad = 0
            for p in raw:
                try:
                    compiled.append(re.compile(p))
                except re.error:
                    bad += 1
            result['full_rules'] = compiled
            log_fn(f"全部刪除規則: {len(compiled)} 條" + (f" ({bad} 條無效已跳過)" if bad else ''))
        except Exception as e:
            log_fn(f"[警告] 讀取全部刪除規則失敗: {e}")

    # 保護規則
    protect_items = _DEFAULT_PROTECT
    if protect_path:
        try:
            df = pd.read_excel(protect_path, engine='openpyxl')
            items = [str(x).strip() for x in df.iloc[:, 0].dropna().tolist() if str(x).strip()]
            if items:
                protect_items = items
        except Exception:
            pass
    result['protect_regs'] = _build_protect_regexes(protect_items)
    log_fn(f"數字保護規則: {len(result['protect_regs'])} 條")

    return result


def _clean_text(text: str, number_rules: list, full_rules: list,
                protect_regs: list, strip_yuan: bool) -> str:
    """對單個字串執行完整清洗流程 (假設規則均為預編譯正則)"""
    if strip_yuan:
        text = _YUAN_RE.sub('', text)
    if protect_regs:
        text = _protect_text(text, protect_regs)
    for reg in number_rules:
        text = reg.sub(_number_only_replacement, text)
    for reg in full_rules:
        text = reg.sub('', text)
    if protect_regs:
        text = _restore_text(text)
    return text


def apply_price_cleaning(df: pd.DataFrame, rules: dict,
                         log_fn: LogFn = print) -> dict:
    """
    對標題和說明應用價格/數字清洗
    返回統計 dict
    """
    stats = {'title_cleaned': 0, 'desc_cleaned': 0}
    number_rules = rules.get('number_rules', [])
    full_rules = rules.get('full_rules', [])
    protect_regs = rules.get('protect_regs', [])

    if not number_rules and not full_rules:
        log_fn("[跳過] 無清洗規則")
        return stats

    # 標題和說明都清洗 (在SEO之前執行, 清洗完再做SEO優化)
    for col in ['標題', '說明']:
        if col not in df.columns:
            continue
        stat_key = 'title_cleaned' if col == '標題' else 'desc_cleaned'
        strip_yuan = (col == '說明')

        # 拉成 list 一次處理, 避免 df.at[] 逐格讀寫的開銷
        originals = df[col].tolist()
        new_values = list(originals)
        changed = 0
        for i, val in enumerate(originals):
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            text = val if isinstance(val, str) else str(val)
            if not text:
                continue
            cleaned = _clean_text(text, number_rules, full_rules, protect_regs, strip_yuan)
            if cleaned != text:
                new_values[i] = cleaned
                changed += 1

        if changed:
            df[col] = new_values
        stats[stat_key] = changed

    log_fn(f"價格清洗完成: 標題{stats['title_cleaned']}處 | 說明{stats['desc_cleaned']}處")
    return stats
