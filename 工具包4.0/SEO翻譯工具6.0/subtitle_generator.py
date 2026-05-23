# -*- coding: utf-8 -*-
"""商品簡述 (brief) 自動產生器 — AI 版本

定位: 不是 SEO 排名信號, 而是「轉化率信號」
目標: 50-80 字 / 清晰賣點 / 規格 / 賣家承諾

調用 SEO 翻譯工具的同一個 LLM API, 但用獨立 prompt
"""
from typing import List, Optional
import re

SUBTITLE_PROMPT = """你是 Yahoo 拍賣商品簡述 (subtitle) 專家 V44。

【V44 核心理念 — 保留原始閒魚商品屬性, 不丟資訊】
原始閒魚 brief 含**結構化商品屬性** (年代/材質/品相/尺寸/品牌),
這些是商品實質資訊, 不是廢話! 任務是**轉繁體 + 改 Yahoo 格式**保留這些資訊,
而不是刪光換成「全台免運 細節可詢」這種廢話。

【任務】把原始閒魚簡述 + 說明的**結構化屬性**轉成 Yahoo 適合格式:
  - 移除冒號分行 (年代:當代 → 直接寫「當代」)
  - 簡轉繁
  - 用空格分隔, 單行
  - 可選結尾加「全台免運」(全店保證)

【規範】
1. 字數 20-50 字典型 (依原始資訊量, 不硬湊)
2. 不可換行 (單行 input)
3. **保留所有原始商品屬性** (年代/材質/品相/尺寸/品牌/版本/型別)
4. 簡轉繁 (年代:宋 → 宋代, 銅 → 銅, carhartt → Carhartt)
5. 不重複標題已有的詞 (那是浪費)
6. 規格用台式 (cm/mm/g, 不用厘米/毫升/克)

【絕對禁用】
形容詞: 精美 / 絕版 / 稀有 / 頂級 / 極品 / 原裝 / 正品 / 保真 / 經典 / 限量
助詞: 這只 / 非常 / 適合 / 一個 / 一只 / 已經 / 還有

【輸出格式】
純 JSON: {"1": "簡述...", "2": "簡述...", ...}

【範例 — 保留商品屬性轉繁體】

原:
  標題: 景祐元寶 宋代古錢幣 100枚 好品 無漏裂
  原簡述: 年代：宋 版本：景祐元寶 材質：銅 品相：美品 型別：通寶/重寶/元寶
  說明: 北宋銅錢 批量100枚 原圖原物 無漏裂
✅ V44: 宋代 銅質 通寶 重寶 元寶 美品 北宋銅錢 100枚 無漏裂 全台免運 (33字)
   — 保留「宋代/銅/通寶/重寶/元寶/美品」原屬性 + 加「全台免運」

原:
  標題: Carhartt JQ1066 復古 深橄欖色
  原簡述: 品牌：carhartt 成色：輕微穿著痕跡 尺碼：XL 適用季節：四季
  說明: 1997年7月美產 4x4大皮標 連帽工裝外套
✅ V44: Carhartt XL碼 1997年7月美產 4x4大皮標 連帽工裝 四季可穿 輕微穿著痕跡 全台免運 (45字)
   — 保留「品牌/尺碼/季節/成色」原屬性 + 標題沒有的「美產/皮標」

原:
  標題: 牙買加100元紙鈔 2014 全新UNC
  原簡述: 年代：當代 發行地區：亞洲 材質：紙 品相：美品
  說明: 紙塑版首發年
✅ V44: 當代亞洲 紙質 美品 紙塑版首發年 UNC全新未流通 全台免運 (29字)
   — 保留「當代/亞洲/紙/美品」 + 補「紙塑版/UNC/未流通」

原:
  標題: 生肖大銅章 雞猴年 紀念章 金雞報曉
  原簡述: 套式：獨立章 成色：品相完好無瑕疵 材質：銅 種類：其他
  說明: 白銅紫銅材質 鑲嵌微縮銀片 原盒原證
✅ V44: 獨立章 銅質 品相完好 白銅紫銅 鑲嵌微縮銀片 原盒原證 全台免運 (32字)
   — 保留「獨立章/銅/品相完好」原屬性 + 加「白銅紫銅/原盒原證」

原:
  標題: Cartier 山度士 自動腕錶 1970年代 18K 男錶
  原簡述: 成色：輕微舊痕無損傷
  說明: 直徑38mm 機械機芯 編號2526 瑞士製 包銀邊已氧化
✅ V44: 直徑38mm 機械機芯 編號2526 瑞士製 包銀邊氧化 輕微舊痕無損傷 全台免運 (39字)
   — 原簡述只有「成色」一句, 補規格細節保留資訊

原:
  標題: 翡翠手鐲 緬甸 A貨 玉鐲
  原簡述: 成色：輕微舊痕無損傷
  說明: 圈口56mm 條粗9mm 冰糯種 翠綠色
✅ V44: 圈口56mm 條粗9mm 冰糯種 翠綠色 輕微舊痕無損傷 全台免運 (29字)

只回覆 JSON, 不要其他文字。"""


def gen_subtitle_ai(items: List[dict], chat_fn, batch_size: int = 10, workers: int = 6) -> List[str]:
    """批次並發用 AI 產生 subtitle

    items: [{'title':..., 'brief':..., 'detail':...}, ...]
    chat_fn: 回傳 LLM response 的函數 (跟 _chat 一樣, 應 thread-safe)
    workers: 並發 batch 數
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [''] * len(items)
    batches = [(i, items[i:i+batch_size]) for i in range(0, len(items), batch_size)]

    def run_batch(start_idx, batch):
        items_text = []
        for j, it in enumerate(batch):
            items_text.append(
                f"{j+1}. 標題: {it.get('title','')}\n"
                f"   原簡述: {it.get('brief','')[:100]}\n"
                f"   說明: {(it.get('detail','') or '')[:300]}"
            )
        user_msg = "請為以下 {} 件商品產生簡述:\n\n{}".format(len(batch), '\n\n'.join(items_text))
        body = {
            "messages": [
                {"role": "system", "content": SUBTITLE_PROMPT},
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
            except Exception:
                m = re.search(r'\{.*\}', content, re.DOTALL)
                parsed = _json.loads(m.group()) if m else {}
            local = {}
            for k, v in parsed.items():
                try:
                    idx = int(k) - 1
                    if 0 <= idx < len(batch):
                        local[start_idx + idx] = str(v).replace('\n',' ').replace('\r',' ').strip()
                except: pass
            return local
        except Exception as e:
            print(f'  [subtitle batch err {start_idx}] {str(e)[:80]}')
            return {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_batch, s, b) for s, b in batches]
        for f in as_completed(futures):
            for idx, val in f.result().items():
                results[idx] = val
    return results


def gen_subtitle_simple(title: str, raw_brief: str = '', detail: str = '') -> str:
    """無 AI 版本 — 簡單抽具體規格 (fallback)"""
    parts = []
    full = f'{raw_brief} {detail}'

    # 抽 cm/mm/g/ml
    sizes = re.findall(r'([\u4e00-\u9fff]{1,4}約?\s?\d+(?:\.\d+)?\s*(?:cm|mm|g|kg|ml|L|公分|釐米|厘米|毫升|克|公斤))', full)
    for s in sizes[:3]:
        if s not in ' '.join(parts):
            parts.append(s.strip())

    # 抽編號/款識
    for m in re.finditer(r'(編號|底款|落款|印款)\s*[:：]?\s*([A-Za-z0-9.]+|\S{2,8})', full):
        s = f'{m.group(1)} {m.group(2)}'.strip()
        if s not in ' '.join(parts):
            parts.append(s)
            break

    # 狀態
    if '全新' in full and '全新' not in title:
        parts.append('全新')
    elif ('輕微' in full or '使用痕跡' in full):
        parts.append('使用痕跡輕微')
    elif '二手' in full and '二手' not in title:
        parts.append('二手品')

    subtitle = ' '.join(parts)
    subtitle = subtitle.replace('\n',' ').replace('\r',' ').strip()
    return re.sub(r'\s+', ' ', subtitle)[:80]


# === 測試 ===
if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    TESTS = [
        {
            'title': 'Cartier 山度士 自動腕錶 1970年代 18K 男錶',
            'brief': '成色：輕微舊痕無損傷',
            'detail': '1970年代Cartier山度士自動腕錶 直徑38mm 18K黃金錶殼 機械機芯 編號 2526 瑞士製 包銀邊已氧化',
        },
        {
            'title': '日本花瓶 深川製 彩瓷蝴蝶蘭 陶瓷擺飾 高31cm',
            'brief': '年代：當代\n材質：陶瓷',
            'detail': '日本深川製花瓶 高31cm 直徑18cm 重量2.5kg 彩瓷蝴蝶蘭紋飾 底款 深川製',
        },
        {
            'title': '袁大頭三年 PCGS XF45 銀元',
            'brief': '評級：PCGS\n品相：美品',
            'detail': '民國三年袁大頭銀幣 PCGS XF45 老彩包漿 26.5g 直徑38mm',
        },
    ]

    print('=== 簡單規則版測試 ===\n')
    for i, t in enumerate(TESTS, 1):
        sub = gen_subtitle_simple(t['title'], t['brief'], t['detail'])
        print(f'[{i}] 標題: {t["title"]}')
        print(f'    簡述 ({len(sub)}字): {sub}')
        print()

    print('\n=== AI 版本測試 (需要 LLM API) ===\n')
    # 載入 SEO 翻譯模組的 _chat
    sys.path.insert(0, r'C:/Users/USERNAME/Desktop/工具包3.0/SEO翻譯工具6.0')
    import os
    os.chdir(r'C:/Users/USERNAME/Desktop/工具包3.0/SEO翻譯工具6.0')
    import json as _json
    cfg = _json.loads(open('translator_config.json', encoding='utf-8').read())
    import translator
    translator.CFG.api_base = cfg.get('api_url', translator.CFG.api_base)
    translator.CFG.api_key = cfg.get('api_key', translator.CFG.api_key)
    translator.CFG.api_keys = cfg.get('api_keys', [])
    translator.CFG.model = cfg.get('model', translator.CFG.model)
    translator.CFG.seo_model = cfg.get('seo_model', translator.CFG.seo_model)
    translator.CFG.timeout = 60

    def chat(body):
        body['model'] = translator.CFG.seo_model or translator.CFG.model
        return translator._chat(body, translator.CFG.timeout)

    results = gen_subtitle_ai(TESTS, chat)
    for i, (t, r) in enumerate(zip(TESTS, results), 1):
        print(f'[{i}] 標題: {t["title"]}')
        print(f'    AI 簡述 ({len(r)}字): {r}')
        print()
