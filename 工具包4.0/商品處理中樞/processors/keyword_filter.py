# -*- coding: utf-8 -*-
"""
關鍵詞過濾處理器
讀取 關鍵詞.xlsx, 命中違規關鍵詞的商品標記為不合格

設計原則:
  - 賣家慣用「[特價福利]」「[福利]」當標籤前綴吸引注意 (33% 商品有), 商品本身合法
  - 但「特價福利」「福利」也是違規詞表內 → 直接擋會丟太多合法商品
  - 解法: 過濾前先清掉「標題開頭方括號標籤」, 然後再掃
  - 結果: [特價福利] 在開頭 = 移除 (賣家標籤); 中間/末尾出現「福利」沒括號 = 仍違規
"""
import re
import pandas as pd
from typing import Callable, List, Tuple, Optional
from processors.utils import append_reason

LogFn = Callable[[str], None]


# 標題開頭的 promo 標籤格式 (中英文方括號/中括號/圓括號 + 促銷詞)
# 用 fullmatch 保證 promo 詞跟括號是配對的, 不誤傷
PROMO_TAG_KEYWORDS = (
    '特價福利', '福利特價', '特惠福利', '福利特惠',
    '特賣福利', '福利特賣',
    '福利',                  # 注意: 純「福利」也算
    '特價', '特惠', '特賣',  # 純促銷詞
    '清倉', '清倉特賣',
)
# 構造 regex: 開頭 (允許空白) + 任一括號 + 任一 promo 詞 + 對應閉括號 + 空白/結尾
_promo_alt = '|'.join(re.escape(k) for k in PROMO_TAG_KEYWORDS)
_PROMO_TAG_PREFIX_RE = re.compile(
    rf'^\s*'
    rf'(?:'
    rf'\[\s*(?:{_promo_alt})\s*\]'        # [...]
    rf'|【\s*(?:{_promo_alt})\s*】'        # 【...】
    rf'|\(\s*(?:{_promo_alt})\s*\)'        # (...)
    rf'|(?:{_promo_alt})'                  # 純文字「特價福利」直接放開頭也算 (賣家偷懶不打括號)
    rf')'
    rf'(?=[\s\W]|$)'                       # 後面必須是空白/標點/結尾, 避免誤吃 "特價福利棒"
)


def _strip_promo_tag_prefix(title: str) -> Tuple[str, bool]:
    """移除標題最開頭的 [特價福利]/[福利]/[特價] 之類 promo 標籤.
    返回 (清洗後標題, 是否真的清過).
    """
    if not title:
        return title, False
    out = title
    changed = False
    # 反覆 strip (避免雙重前綴 [福利][特價] 或 [特價][福利])
    for _ in range(5):  # 最多 5 層, 防無限 loop
        new = _PROMO_TAG_PREFIX_RE.sub('', out, count=1).lstrip(' \t　')
        if new == out:
            break
        out = new
        changed = True
    return out, changed


# ★ 4.0.67: hardcoded critical keywords (必抓, 不依賴 admin 維護 xlsx).
#   理由: 關鍵詞.xlsx 在 protected list, 雲端更新不會推給同事 (避免覆寫他們自定義).
#   admin 加 xlsx 同事拿不到. 「克價類」是賣家虧錢級別 critical 字眼, 必須代碼層 hardcode.
#   實例: 4070 SUPER 用戶採集牙雕 1048g 「價格為克價」, 起標價 25 = ¥25/克, 整件 ¥26,200,
#   舊邏輯只掃標題, 「克價」字眼在說明欄漏抓 → 25 元下單虧 ¥26,175.
HARDCODED_CRITICAL_KEYWORDS = [
    # 克價類 (賣家標的是 ¥/克, 採集器當總價會虧到爆)
    # ★ 保守原則: 只保留「明確克價意義」的詞, 不加 /克 /g /元/克 /元/g (會誤殺
    #   「淨重100g」「重量約200/克」「成本約50元/克」等正常商品描述)
    # ★ 不加「一克」(會誤殺「一克拉鑽石/寶石」)
    '價格為克價', '克價', '克价',
    '按克計', '按克计', '按克算',
    '每克', '克單價', '克单价',
    '克起拍', '克起標', '克起标',
    # ★ 4.0.80: 標價非賣價類 (同克價陷阱: 賣家標小數字實際另計價, 採集器當總價會虧到爆)
    '標價非賣價', '标价非卖价',
    '標價不是賣價', '标价不是卖价',
    '非實際售價', '非实际售价',
    '非真實售價', '非真实售价',
]

# ★ 4.0.76 / 4.0.77 精簡: hardcoded exempt rules (代碼層豁免, 讓 admin 不用 xlsx 個別維護).
#   理由: admin 維護 xlsx 加排除詞同事拿不到 (xlsx 在 protected 不打進雲端).
#   admin xlsx 加排除詞「同城可」只對 admin 機器生效, 同事仍會誤判.
#   解法: 代碼層 hardcode 一份豁免規則, 同事拉雲端代碼後 keyword_filter 自動 merge.
#   邏輯: 違規詞 → list of 「同時出現任一就豁免」的關鍵字 (xlsx + 此 dict 取 union)
#   ★ 4.0.77: 精簡只保留「自提」+ 「同城可」 (admin 指定範圍, 其他違規詞照 xlsx 原邏輯)
HARDCODED_EXEMPT_FOR = {
    '自提': ['同城'],  # 4.0.78: 改成更寬鬆的「同城」(涵蓋「同城可/同城」/「同城自提」等)
}

# ★ 4.0.79: hardcoded title_only — 只掃標題, 不掃說明欄.
#   理由: 4.0.67 改成「掃標題+說明」是為了抓「克價/淘寶 URL」這類critical 字眼在說明欄.
#         但很多「賣家自陳/流程」字眼也常在合法商品說明欄出現, 4.0.67 後大量誤殺.
#   實例: 5893 用戶 9179 件批 keyword 命中 26% 大爆漲, root cause 就是這些詞抓到說明欄.
#   解法: 此 set 內的詞只掃標題, 說明欄無視. 標題出現仍正常命中 (賣家把這些放標題真違規).
HARDCODED_TITLE_ONLY = {
    # 賣家展示舊商品狀態 (賣家常在說明展示之前售過的同款)
    '已賣出', '已出貨', '已賣了', '暫不出', '已售出', '已售', '已結緣', '已出', '僅欣賞',
    # 賣家流程/交易自陳
    '定金', '剪標', '差價', '求購', '收購', '專拍',
    # 用戶/物流自陳
    '勿拍', '實體店', '自提',
    # 通路/推廣
    '鏈接', '直播', '預定', '客單', '客定', '轉帳', '高價收',
}


def load_keywords(xlsx_path: str, log_fn: LogFn = print
                  ) -> List[Tuple[str, List[str]]]:
    """
    讀取 關鍵詞.xlsx, 返回:
    [(違規關鍵詞, [不含關鍵詞列表]), ...]
    ★ 4.0.67: 跟 HARDCODED_CRITICAL_KEYWORDS 合併 (代碼層必抓, xlsx 補充)
    """
    log_fn(f"讀取關鍵詞: {xlsx_path}")
    df = pd.read_excel(xlsx_path, engine='openpyxl')

    if '違規關鍵詞' not in df.columns:
        raise ValueError("關鍵詞.xlsx 必須包含欄位: 違規關鍵詞")

    kw_list = []
    # ★ 4.0.67: 先加 hardcoded critical (無排除詞)
    seen = set()
    for kw in HARDCODED_CRITICAL_KEYWORDS:
        if kw and kw not in seen:
            kw_list.append((kw, []))
            seen.add(kw)
    if kw_list:
        log_fn(f"  ★ hardcoded critical (克價類): {len(kw_list)} 個 (代碼層必抓, 不依賴 xlsx)")
    for _, row in df.iterrows():
        kw = row['違規關鍵詞']
        if pd.isna(kw) or not str(kw).strip():
            continue

        excludes = []
        excl_raw = row.get('不含關鍵詞', '')
        if pd.notna(excl_raw) and str(excl_raw).strip():
            excludes = [e.strip() for e in str(excl_raw).split(',') if e.strip()]

        # ★ 4.0.76: merge hardcoded exempts (代碼層自動補豁免, 同事不用維護 xlsx)
        kw_str = str(kw).strip()
        if kw_str in HARDCODED_EXEMPT_FOR:
            # union 兩邊排除詞 (xlsx + hardcoded), 去重保序
            seen_ex = set(excludes)
            for ex in HARDCODED_EXEMPT_FOR[kw_str]:
                if ex not in seen_ex:
                    excludes.append(ex)
                    seen_ex.add(ex)

        kw_list.append((kw_str, excludes))

    # 4.0.76: log hardcoded exempt 命中數
    hardcoded_exempt_hits = sum(1 for kw, _ in kw_list if kw in HARDCODED_EXEMPT_FOR)
    log_fn(f"關鍵詞載入完成: {len(kw_list)} 個違規詞 (含 {sum(1 for _, e in kw_list if e)} 個有排除條件)")
    if hardcoded_exempt_hits:
        log_fn(f"  ★ 4.0.76: {hardcoded_exempt_hits} 個違規詞自動 merge hardcoded 豁免 (物流方式如「同城可自提」)")
    return kw_list


def apply_keyword_filter(df: pd.DataFrame,
                         kw_list: List[Tuple[str, List[str]]],
                         log_fn: LogFn = print) -> dict:
    """
    掃描標題, 命中違規關鍵詞則標記 _filter_reason
    返回統計 dict

    ★ 預處理: 移除「標題開頭的方括號 promo 標籤」(例 [特價福利]/[福利]/[特價]) — 賣家慣用前綴
    ★ 移除後再掃; 移除後的標題寫回 df, 後續階段都看清洗後版本
    ★ 中間/末尾出現的促銷詞 (沒方括號) → 仍違規 (這通常是真違規商品在用)
    """
    stats = {'hit': 0, 'clean': 0, 'promo_stripped': 0}

    if '標題' not in df.columns:
        log_fn("[警告] 無標題欄位, 跳過關鍵詞過濾")
        return stats

    # ── 預處理: 清標題開頭的 [特價福利] 類標籤 ──
    for idx in df.index:
        if pd.isna(df.at[idx, '標題']):
            continue
        orig = str(df.at[idx, '標題'])
        stripped, changed = _strip_promo_tag_prefix(orig)
        if changed:
            df.at[idx, '標題'] = stripped
            stats['promo_stripped'] += 1
    if stats['promo_stripped']:
        log_fn(f"  → 預清: {stats['promo_stripped']} 個標題清掉開頭 [特價福利]/[福利]/[特價] 類標籤")

    # ── 掃清洗後標題 + 說明 ──
    # ★ 4.0.67: 4070 SUPER 用戶採集到「日本牙雕 重量1048g 價格為克價」(起標價 25 = ¥25/克 = 整件 ¥26,200).
    #   舊邏輯只掃標題, 「價格為克價」字眼在說明欄漏抓 → 賣家會被 25 元下單虧 ¥26,175.
    #   現在同時掃「標題 + 說明」(或同義欄: 商品簡述/描述/內容)
    # ★ 4.0.79: HARDCODED_TITLE_ONLY 內的詞**只掃標題**, 不掃說明 (賣家自陳類在說明常合法)
    desc_cols = [c for c in ('說明', '商品簡述', '描述', '商品描述', '內容') if c in df.columns]
    for idx in df.index:
        title = str(df.at[idx, '標題']) if pd.notna(df.at[idx, '標題']) else ''
        # 合併說明欄文字 (掃內容用, 不改 df)
        desc_text = ''
        for dc in desc_cols:
            v = df.at[idx, dc]
            if pd.notna(v):
                desc_text += ' ' + str(v)
        scan_text = title + ' ' + desc_text
        if not scan_text.strip():
            continue

        hit_kw = None
        hit_in_desc = False  # 命中位置 (給 reason 用)
        for kw, excludes in kw_list:
            # ★ 4.0.79: title_only 詞只看標題, 其他詞看 標題+說明
            check_text = title if kw in HARDCODED_TITLE_ONLY else scan_text
            if kw in check_text:
                # 檢查排除條件 (在 check 範圍內出現排除詞就豁免)
                if excludes and any(ex in check_text for ex in excludes):
                    continue  # 豁免
                hit_kw = kw
                hit_in_desc = (kw not in title) and (kw in desc_text)
                break

        if hit_kw:
            reason = f'違規關鍵詞:{hit_kw}' + ('(說明欄)' if hit_in_desc else '')
            df.at[idx, '_filter_reason'] = append_reason(
                df.at[idx, '_filter_reason'], reason)
            stats['hit'] += 1
        else:
            stats['clean'] += 1

    log_fn(f"關鍵詞過濾完成: 命中{stats['hit']} | 通過{stats['clean']}")
    return stats
