# -*- coding: utf-8 -*-
"""GPT-image-2 首圖優化 — V3 PRESERVE prompt
- 為每個商品的首圖 (圖片欄第 1 個 path) 跑 image edits
- 結果儲存為 0.jpg 放在同資料夾, 排在原 1.jpg 之前
- 更新「圖片」欄位: path/to/0.jpg|原本所有 path
- 違禁品 / 已過濾商品自動跳過 (省 image API 配額)
"""
import os
import time
import base64
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from PIL import Image

API_ENDPOINT = 'https://api.example.com/v1/images/edits'
API_KEY = '<TEST_API_KEY>'
MODEL = 'gpt-image-2'
TIMEOUT = (15, 240)  # (connect, read) — connect 短防 DNS/握手卡死, read 長吸收中介 90s wall + CF flush + VPN 抖動


def _is_network_blip(exc, elapsed, err_code=None, status_code=None):
    """4.0.30 / 4.0.73: 改成 wrapper, 統一邏輯在 processors.utils.is_network_blip"""
    try:
        from processors.utils import is_network_blip
        return is_network_blip(exc, elapsed=elapsed, err_code=err_code, status_code=status_code)
    except (ImportError, TypeError):
        # fallback: 簡化版 (TypeError 包: 舊版 utils 沒 status_code 參數)
        if status_code in (502, 504, 522, 523, 524):
            return True
        if err_code or exc is None:
            return False
        msg = str(exc)
        return ('Max retries exceeded' in msg or 'HTTPSConnectionPool' in msg
                or 'IncompleteRead' in msg or 'ConnectionError' in type(exc).__name__)


MAX_RETRIES = 2  # 原始 1 次 + retry 1 次
WORKERS = 96  # ⚠ 預設值, 實際運行時 stage 開頭呼叫 get_optimal_workers(9.0) 動態取
              # 範例: 4 帳號 → 40, 2 帳號 → 20 (撞牆會 cascade 自動降)
SIZE = '1024x1024'  # gpt-image-2 不支援 512×512 (400 錯誤), 必須 1024+

# ★ 失敗類型記錄 (路徑 → 'api_fail' / 'individual') — 給 ckpt 判斷重試或標完成
_last_fail_info = {}

# V3 PRESERVE prompt 心法 — 通用前綴 + 品類專用後綴
COMMON_PRESERVE = (
    "Edit ONLY the background and lighting. PRESERVE the product 100% identically. "
    "★★★ ABSOLUTE RULES (violations destroy product value): ★★★ "
    "1. NEVER hallucinate or fill in any part of the product that is cropped, hidden, or partially shown in the input — "
    "if a coin is half-cropped, output a half-cropped coin (DO NOT complete it). "
    "2. NEVER invent or modify text/numbers: serial numbers, grading scores (PMG/ACG/GBCA/PCGS), "
    "dimensions (e.g. 8x6.5), barcodes, dates, signatures, model numbers — "
    "all text/digits MUST be IDENTICAL to input pixel-by-pixel. "
    "3. NEVER smooth or repaint product surface — keep all scratches, patina, oxidation, wear marks. "
    "4. NEVER change product proportions, position, or count (if 5 spoons, output exactly 5 spoons). "
    "5. NEVER remove protective cases (PMG/PCGS/GBCA grading slabs, jewelry boxes) — these ARE part of the product. "
    "6. PRESERVE: exact silhouette, all surface texture, original color tone, patina, age wear, "
    "brand marks, inscriptions, seal stamps (印款/底款), grading slab text, all numbers and barcodes. "
    "7. Keep original camera angle, product orientation, and any cropping/framing from input. "
    "Negative: no smoothing, no fake reflections, no color enhancement, no detail invention, "
    "no plastic look, no over-saturation, no AI-generated symmetry artifacts, no completing missing parts, "
    "no rewriting digits, no added text, no watermark, no logo. "
    # ★ 4.0.64: 加強 negative — audit 32 張 image_opt 輸出發現「integrity·innovate」複合 logo 殘留 (3% rate),
    #   GPT-image-2 把「中文方框+水平線+英文 italic」誤當商品 inscription 保留. 列具體 type 阻止:
    "★ STRICTLY REMOVE these (do NOT preserve as inscriptions): "
    "no e-commerce platform name overlay (taobao/淘寶/閒魚/xianyu/shopee/蝦皮/pinduoduo/拼多多/1688/mogu/小紅書/xiaohongshu/京東/jd/天貓/tmall in any text), "
    "no seller's complex composite logo (中文方框 + 上下水平線 + 英文 italic small text like 'integrity·innovate'/'shi.to'/'luxury' overlay floating in background), "
    "no anti-piracy text overlay (盜圖必究/實物拍攝/版權所有/抄襲必究/翻版必究), "
    "no seller's promotional banner (包郵/順豐/質保/24h極速/現貨/滿XX減XX), "
    "no seller's price tag overlay (¥XXX/¥XXX,XXX/$XXX 浮在商品旁的價格貼紙), "
    "no seller's specification text overlay (高XX寬XX/直徑XX/重XX斤/尺寸約Xcm 浮在背景空白處), "
    "no seller's signature stamp box overlay (賣家方框印章+地名+時間戳 浮在背景), "
    "no QR code overlay, no app screenshot UI elements (status bar/search bar/buttons/comment counts). "
    "These are SELLER-ADDED watermarks separated from product body — REMOVE them. "
    "Only PRESERVE inscriptions/marks PHYSICALLY ENGRAVED/PRINTED ON the product surface itself "
    "(coin Chinese era characters/玉牌銘文/PMG slab text/ jewelry box label sticker). "
)

PROMPTS = {
    '古董瓷器': COMMON_PRESERVE + "Background: aged elm wood shelf with subtle warm tone, warm museum-style spotlight from upper-right at 45 degrees, soft natural shadow. Photorealistic, 1:1 square, 1024x1024.",
    '古董錢幣': COMMON_PRESERVE + "Background: clean light grey gradient, soft top-down even lighting, faint shadow beneath. Coin orientation and angle identical to original. Sharp focus on inscriptions and patina. Photorealistic, 1:1, 1024x1024.",
    '飾品玉石': COMMON_PRESERVE + "Background: matte black velvet, soft backlight from behind to reveal natural translucency (水头), subtle rim light. Show internal fissures and color gradient as in original. Photorealistic auction catalog, 1:1, 1024x1024.",
    '紫砂壺茶具': COMMON_PRESERVE + "Background: dark walnut wood tea table, soft 45-degree window light from upper-left, subtle steam wisp in upper-right corner. Original clay color and carved inscriptions must remain visible exactly as input. Photorealistic, 1:1, 1024x1024.",
    '古董其他': COMMON_PRESERVE + "Background: warm aged wood surface or dark burgundy velvet, museum-style soft directional warm light at 3000K from upper-left, slight vignette. Photorealistic, 1:1, 1024x1024.",
    '服飾鞋包': COMMON_PRESERVE + "Background: clean off-white linen flat surface OR pure white seamless backdrop, soft natural daylight from above, subtle realistic shadow. Original fabric texture, color, hardware patina must remain unchanged. Photorealistic flat-lay, 1:1, 1024x1024.",
    '通用':     COMMON_PRESERVE + "Background: warm neutral tone appropriate to product, soft directional studio lighting, single hero angle. Photorealistic e-commerce hero image, 1:1, 1024x1024.",
}


def _categorize(cat_path: str) -> str:
    """根據「拍賣類別名稱」分流 prompt"""
    p = str(cat_path or '')
    if '瓷器' in p: return '古董瓷器'
    if '錢幣' in p or '古錢' in p or '紙幣' in p or '徽章' in p or '紀念' in p: return '古董錢幣'
    if '玉' in p or '翡翠' in p or '飾品' in p or '珠寶' in p or '銀飾' in p or '黃金' in p or '手鐲' in p: return '飾品玉石'
    if '紫砂' in p or '茶具' in p or '茶器' in p or '壺' in p: return '紫砂壺茶具'
    if '服飾' in p or '服裝' in p or '鞋' in p or '包包' in p or '配件' in p: return '服飾鞋包'
    if '古董' in p or '骨董' in p or '收藏' in p or '藝術' in p: return '古董其他'
    return '通用'


def _optimize_one_image(src_img_path: str, cat: str, log_fn: Callable, bc: str = '', img_idx: int = 0) -> Optional[str]:
    """跑 GPT-image-2 edits + retry, 成功回傳輸出 0.jpg 路徑, 失敗回 None.
    bc / img_idx: 給 X-Trace header 用 (admin Claude review 時知道哪商品)
    """
    if not os.path.exists(src_img_path):
        log_fn(f'  ⚠ 圖片不存在: {src_img_path}')
        return None

    img_dir = os.path.dirname(src_img_path)
    out_path = os.path.join(img_dir, '0.jpg')

    prompt = PROMPTS.get(cat, PROMPTS['通用'])

    # ★ Input 壓縮到 1024 max (壓測證實: 24% 加速, 30k 省 32hr)
    # 大原圖 (4096×3072) 上傳網路慢, 壓到 1024 上傳快
    upload_bytes = None
    try:
        # ★ 4.0.71: 應用 EXIF orientation (iPhone 原圖會躺著, 影響 GPT-image-2 處理方向)
        from PIL import ImageOps
        with Image.open(src_img_path) as img:
            img = ImageOps.exif_transpose(img).convert('RGB')
            if max(img.size) > 1024:
                ratio = 1024 / max(img.size)
                img = img.resize((int(img.size[0]*ratio), int(img.size[1]*ratio)), Image.LANCZOS)
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, 'JPEG', quality=88)
            upload_bytes = buf.getvalue()
    except Exception:
        upload_bytes = None  # fallback 用原檔

    last_err = ''
    # ★ APIMonitor (連續真 API 失敗 → PauseException)
    try:
        from checkpoint_manager import get_monitor
        _monitor = get_monitor()
    except Exception:
        _monitor = None
    last_status = None; last_err_code = None; last_err_type = None; last_err_msg = None; last_exc = None
    actual_attempts = 0
    elapsed = 0  # 給 _is_network_blip 用 (即使 requests.post 立刻拋, 也有值可比)
    for attempt in range(MAX_RETRIES):
        actual_attempts += 1
        t0 = time.time()
        # 重設此次 attempt 的狀態 — 避免上次 attempt 的 stale 值污染分類判斷
        # 例: attempt 0 拋 IncompleteRead, attempt 1 拿到 upstream_error → 不重設會看 attempt 0 的 exc 誤判 blip
        last_status = None
        last_err_code = None
        last_err_type = None
        last_err_msg = None
        last_exc = None
        # X-Trace header (image_opt = stage_k_edit, multipart req but header 仍可加)
        try:
            from processors.feedback_collector import make_xtrace_single
            _xtrace = make_xtrace_single('stage_k_edit', bc or 'unknown', img_idx, retry=attempt)
        except Exception:
            _xtrace = ''
        _req_headers = {'Authorization': f'Bearer {API_KEY}'}
        if _xtrace:
            _req_headers['X-Trace'] = _xtrace
        try:
            # ★ 4.0.45: 用 shared session (TLS keep-alive). 高 RTT (300-400ms) 下省 ~1s/request handshake.
            try:
                from processors.utils import get_shared_session
                _http = get_shared_session()
            except Exception:
                _http = requests  # fallback
            if upload_bytes:
                from io import BytesIO
                files = {'image': ('1.jpg', BytesIO(upload_bytes), 'image/jpeg')}
                r = _http.post(
                    API_ENDPOINT,
                    headers=_req_headers,
                    files=files,
                    data={'model': MODEL, 'prompt': prompt, 'n': '1', 'size': SIZE},
                    timeout=TIMEOUT,
                )
            else:
                with open(src_img_path, 'rb') as fi:
                    r = _http.post(
                        API_ENDPOINT,
                        headers=_req_headers,
                        files={'image': ('1.jpg', fi, 'image/jpeg')},
                        data={'model': MODEL, 'prompt': prompt, 'n': '1', 'size': SIZE},
                        timeout=TIMEOUT,
                    )
            elapsed = time.time() - t0
            last_status = r.status_code
            # 中介合約 v3 (chunked + keepalive): 永遠 HTTP 200, 看 body error.code 分流
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception as e:
                    last_err = f'200 but JSON parse fail: {str(e)[:60]}'
                    last_err_msg = last_err
                    break

                b64 = (j.get('data') or [{}])[0].get('b64_json', '')
                if b64:
                    png_bytes = base64.b64decode(b64)
                    from io import BytesIO
                    img = Image.open(BytesIO(png_bytes)).convert('RGB')
                    # ★ 4.0.72: atomic write — 防 process kill 中間寫到一半損壞 0.jpg.
                    #   下一次 resume 看 0.jpg exists 會跳過 → 損壞圖被當已完成 → 上 Yahoo 變壞圖.
                    tmp_out = out_path + '.opt.tmp'
                    try:
                        img.save(tmp_out, 'JPEG', quality=92, optimize=True)
                        os.replace(tmp_out, out_path)  # atomic
                    except Exception:
                        try:
                            if os.path.exists(tmp_out):
                                os.remove(tmp_out)
                        except Exception:
                            pass
                        raise
                    sz_kb = os.path.getsize(out_path) // 1024
                    if _monitor: _monitor.record_success()
                    return out_path

                # 200 + 無 b64 → 看 body error.code
                err = j.get('error') or {}
                err_code = str(err.get('code') or '')
                err_type = str(err.get('type') or '')
                err_msg = str(err.get('message') or '')[:200]
                last_err_code = err_code; last_err_type = err_type; last_err_msg = err_msg

                if err_code == 'content_policy_violation':
                    last_err = f'違禁: {err_msg[:80] or err_code}'
                    break
                elif err_code == 'stream_disconnected':
                    # 中介觀察值: 過去案例 retry 很少救回 (OpenAI moderation 砍流), 但上游異常期
                    # 部分是 transient (GPU OOM 之類) → 給 1 次 retry 機會, 不 break
                    last_err = f'stream_disconnected ({elapsed:.0f}s)'
                elif err_code == 'middleware_timeout':
                    last_err = f'middleware_timeout 圖太複雜 ({elapsed:.0f}s)'
                    break
                elif err_code in ('upstream_timeout', 'upstream_connection_error',
                                  'upstream_error', 'unknown_error'):
                    last_err = f'{err_code} ({elapsed:.0f}s)'
                else:
                    last_err = f'未知 error: {err_code or err_msg[:60]} (不重試)'
                    break
            else:
                # ★ 非 200 也嘗試解析 body 看 middleware 標記
                # 中介 v3.4: 503 + error.type='middleware_classified' = 中介自我保護, 不算真失敗
                try:
                    j = r.json()
                    err = j.get('error') or {}
                    last_err_code = err.get('code')
                    last_err_type = err.get('type')
                    last_err_msg = (err.get('message') or '')[:200]
                except Exception: pass
                last_err = f'HTTP {r.status_code} {last_err_code or "(連線錯誤, retry)"}'
            if attempt < MAX_RETRIES - 1:
                time.sleep(3)
                continue
        except Exception as e:
            elapsed = time.time() - t0
            last_exc = e
            last_err = f'exception {str(e)[:80]}'
            if attempt < MAX_RETRIES - 1:
                time.sleep(3)
                continue
    # 區分網路層中斷 vs 真 API 失敗 (帶 err_code 雙重保險)
    is_blip = _is_network_blip(last_exc, elapsed, last_err_code, status_code=last_status)
    blip_tag = ' [網路層, 不計 hub fail]' if is_blip else ''
    log_fn(f'  ❌ {os.path.basename(img_dir)}: {actual_attempts} 次失敗 - {last_err}{blip_tag}')
    # ★ 通報 APIMonitor: 用 classify_failure 統一判斷
    # 網路層中斷 (ChunkedEncodingError / ReadTimeout < 100s) 不通報 — 中介那端成功了, 是 CF / VPN 問題
    if _monitor and not is_blip:
        _monitor.record_fail(status_code=last_status, error_code=last_err_code,
                             error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
    # ★ 判斷失敗類型 (給 ckpt 用): 用 should_retry (區別於 classify_failure)
    # should_retry=True → mark_failed (下次 resume 會重試, 因暫時問題)
    # should_retry=False → mark_done (不重試, 個別圖永久問題)
    try:
        from checkpoint_manager import APIMonitor
        retry = APIMonitor.should_retry(
            status_code=last_status, error_code=last_err_code,
            error_type=last_err_type, error_message=last_err_msg, exception=last_exc)
    except Exception:
        retry = False
    _last_fail_info[src_img_path] = 'api_fail' if retry else 'individual'
    return None


def optimize_first_images(df, log_fn: Callable, progress_fn: Optional[Callable] = None, log_detail_fn: Optional[Callable] = None, ckpt=None) -> dict:
    log_detail = log_detail_fn if log_detail_fn else log_fn
    """
    對 df 每一行的「圖片」欄第一張跑優化, 結果寫成同資料夾的 0.jpg, 並更新「圖片」欄。

    跳過條件 (省 image API 配額):
    - _filter_reason 有值 (違禁/低分/未映射)
    - 「圖片」欄為空
    - 第 1 張圖檔不存在

    返回 stats: {'total':N, 'optimized':K, 'skipped':S, 'failed':F}
    """
    has_reason = df['_filter_reason'].fillna('').str.strip().astype(bool) if '_filter_reason' in df.columns else None

    tasks = []  # [(idx, src_path, cat)]
    n_skip_filter = 0
    n_skip_no_img = 0
    n_skip_close_up = 0  # 局部特寫商品, 不跑 image_opt 避免 AI 編造
    n_skip_already_done = 0  # ★ 0.jpg 已存在 (之前跑成功過)
    # ★ 計數器提前初始化 (skip 路徑也需要更新, 不只是新跑成功的時候)
    n_reject_first_applied = 0
    n_reject_indices_total = 0  # 被過濾的圖總數 (V65 全圖 reject)
    for idx, row in df.iterrows():
        if has_reason is not None and has_reason.loc[idx]:
            n_skip_filter += 1
            continue
        img_field = str(row.get('圖片', '') or '')
        if not img_field:
            n_skip_no_img += 1
            continue
        paths = [p.strip() for p in img_field.split('|') if p.strip()]
        if not paths or not os.path.exists(paths[0]):
            n_skip_no_img += 1
            continue
        # ★ 局部特寫商品 → 跳過 image_opt (避免 AI 把局部腦補成完整商品)
        if '_skip_image_opt' in df.columns:
            sk = row.get('_skip_image_opt')
            if isinstance(sk, bool) and sk:
                n_skip_close_up += 1
                continue
            if isinstance(sk, str) and sk.strip().lower() in ('true','是','yes','1'):
                n_skip_close_up += 1
                continue
        # ★ 0.jpg 已存在 = 之前跑成功過 image_opt, 跳過 (file-system check 比 ckpt 可靠)
        # 因為 image_opt 跑成功後 df 的「圖片」欄會改成 0.jpg|1.jpg|..., paths[0] 會變
        # ckpt 記的 key 是當時的 paths[0] = 1.jpg, 但 resume 後 paths[0] = 0.jpg, ckpt 找不到
        # → 用 file system 直接檢查 0.jpg 存在性最可靠
        img_dir = os.path.dirname(paths[0])
        zero_jpg_path = os.path.join(img_dir, '0.jpg')
        # ★ 4.0.72: resume sanity check — 之前舊版 atomic 不全, kill 中可能留下 0/損壞 0.jpg.
        #   只看 exists 會把損壞圖當已完成跳過, 上 Yahoo 變壞圖. 加 size > 1KB 檢查.
        _zero_ok = False
        if os.path.exists(zero_jpg_path):
            try:
                _zero_ok = os.path.getsize(zero_jpg_path) > 1024  # 有效 jpeg 至少 1KB
            except Exception:
                _zero_ok = False
            if not _zero_ok:
                # 損壞檔, 刪掉重跑
                try:
                    os.remove(zero_jpg_path)
                    log_fn(f'  ⚠ 偵測損壞 0.jpg ({zero_jpg_path[-60:]}), 已刪重跑')
                except Exception:
                    pass
        if _zero_ok:
            n_skip_already_done += 1
            # ★ Bug 修: 即便跳過 optimization, 仍需把 0.jpg 寫回 df['圖片'] 並套用 _reject_indices
            # 沒這段, 用戶第二次跑時 0.jpg 雖然在磁碟上但不會出現在輸出 Excel 的 圖片欄
            reject_indices = set()
            rv = row.get('_reject_indices', '')
            if rv and isinstance(rv, str):
                try:
                    reject_indices = set(int(x) for x in rv.split(',') if x.strip())
                except Exception:
                    pass
            # 套 reject 過濾原 paths (0.jpg 不算 reject)
            kept_paths = [p for i, p in enumerate(paths) if i not in reject_indices]
            # 確保 0.jpg 在最前面, 並去重
            new_paths = [zero_jpg_path] + [p for p in kept_paths if p != zero_jpg_path]
            df.at[idx, '圖片'] = '|'.join(new_paths)
            if reject_indices:
                if 0 in reject_indices:
                    n_reject_first_applied += 1
                n_reject_indices_total += len(reject_indices)
            continue
        cat = _categorize(row.get('拍賣類別名稱', ''))
        tasks.append((idx, paths[0], cat, paths))

    # ★ Checkpoint: 也用 ckpt 多一層 skip (有些 0.jpg 不存在但 ckpt 有 mark, 例如損壞圖)
    if ckpt is not None:
        before = len(tasks)
        tasks = [t for t in tasks if not ckpt.is_done('image_opt', t[1])]
        skipped_resume = before - len(tasks)
        if skipped_resume > 0:
            log_fn(f'[圖片優化] ⏭ Resume 跳過 {skipped_resume} 件 (ckpt 標完成)')
    if n_skip_already_done > 0:
        log_fn(f'[圖片優化] ⏭ 跳過 {n_skip_already_done} 件 (0.jpg 已存在 = 之前跑成功)')

    log_fn(f'[圖片優化] 待處理 {len(tasks)} 件 | 跳過違禁/低分 {n_skip_filter} | 跳過無圖 {n_skip_no_img} | 跳過局部特寫 {n_skip_close_up}')
    if not tasks:
        return {'total': len(df), 'optimized': 0, 'skipped': n_skip_filter + n_skip_no_img + n_skip_close_up, 'failed': 0}

    n_ok, n_fail = 0, 0
    t0 = time.time()
    done = 0

    def _run(task):
        idx, src_path, cat, all_paths = task
        # 從 df 撈商品條碼當 X-Trace bc; img_idx=0 因為 image_opt 處理首圖
        try:
            bc = str(df.at[idx, '商品條碼']) if '商品條碼' in df.columns else f'IDX{idx}'
        except Exception:
            bc = f'IDX{idx}'
        out_path = _optimize_one_image(src_path, cat, log_fn, bc=bc, img_idx=0)
        return idx, out_path, all_paths

    success_codes = []  # 成功的商品條碼 (詳細日誌)
    fail_codes = []     # 失敗的商品條碼+原因
    try:
        from checkpoint_manager import get_monitor, get_optimal_workers, DynamicSemaphore
        _mon_io = get_monitor()
        # ★ 直接抓中介建議 (image_edit = avg 9s)
        actual_workers, w_info = get_optimal_workers(task_type='image_edit', warmup=True, max_cap=96)
        # ★ 4.0.32: hub 端 RTT-aware cap (218ms+ 並發砍到 30, 避免 TLS 撐爆)
        try:
            from processors.utils import apply_hub_cap
            capped = apply_hub_cap(actual_workers)
            if capped < actual_workers:
                log_fn(f'  [image_opt] 中介建議 {actual_workers} → hub RTT cap → {capped}')
                actual_workers = capped
        except Exception: pass
        log_fn(f'  [image_opt] 動態並發: {actual_workers} (來源={w_info.get("source")}, '
               f'active_accts={w_info.get("codex_active_accounts", "?")}, hubs={w_info.get("active_hubs", "?")}, '
               f'my_share={w_info.get("my_rpm_share", "?")})')
    except Exception as e:
        _mon_io = None
        actual_workers = WORKERS
        DynamicSemaphore = None
        log_fn(f'  [image_opt] 動態並發失敗, 用預設 {actual_workers}: {e}')

    # ★ DynamicSemaphore 限流: pool max=POOL_MAX, 實際並發由 dsem 控制 (可 mid-flight 調整)
    POOL_MAX = 128  # 上限 (給未來 6 帳號留 buffer)

    # ★ 4.0.35: 用 AdaptiveCapController 取代純 DynamicSemaphore — 跑批中根據 success rate
    #   自動 ratchet up/down. 起點是 4.0.34 hub_cap 後的 actual_workers, max 是中介原始 suggested
    #   (從 w_info 拿到, fallback 用 actual_workers * 2 或 96).
    #   多用戶不同 RTT 場景下, 各自找各自 sweet spot.
    try:
        from processors.utils import get_or_create_adaptive_cap, reset_adaptive_caps
        # 第一次跑時 reset (避免之前 batch state 殘留)
        # 注意: image_opt 同 batch 內 retry 階段也想用同 controller 累積統計
        # 所以不在這 reset, 而是 pipeline 開始時 reset (見 pipeline.py)
        max_cap = max(actual_workers * 2, 48)  # adaptive 可 ratchet up 到這個上限
        ac = get_or_create_adaptive_cap('image_opt', initial_cap=actual_workers,
                                         max_cap=max_cap, min_cap=8)
        ac.set_log_fn(log_fn)
        log_fn(f'  [image_opt] adaptive cap 啟用: 起點={actual_workers}, max={max_cap}, min=8')
    except Exception as _e:
        ac = None
        log_fn(f'  [image_opt] adaptive cap init 失敗 (退回靜態): {_e}')

    dsem = DynamicSemaphore(actual_workers) if (DynamicSemaphore and not ac) else None
    last_adjust_done = [0]
    adjust_lock = __import__('threading').Lock()

    # wrap _run 加 cap (adaptive 優先, 退回 dsem)
    _run_orig = _run
    def _run_with_sem(t):
        if ac is not None:
            if not ac.acquire():
                return None, None, []  # cancelled
            try:
                result = _run_orig(t)
                # report 結果 — out_path None 算 fail
                if result and len(result) >= 2 and result[1]:  # out_path 非空 = success
                    ac.report_success()
                else:
                    # 拿 last_exc 判 blip (caller 已寫 _last_fail_info, 但簡化: 直接 report_fail without exc)
                    # ac.report_fail 內部會過濾 blip — 這裡傳 None 視為真 fail
                    ac.report_fail()
                return result
            finally:
                ac.release()
        if dsem is not None:
            if not dsem.acquire():
                return None, None, []
            try:
                return _run_orig(t)
            finally:
                dsem.release()
        return _run_orig(t)

    pool = ThreadPoolExecutor(max_workers=POOL_MAX if dsem else actual_workers)
    futures = {pool.submit(_run_with_sem, t): t for t in tasks}
    try:
        for f in as_completed(futures):
            if _mon_io and getattr(_mon_io, 'user_stop', False):
                # ★ user_stop: 喚醒 ac/dsem 等待者 + 取消 pending future + 等 in-flight
                # 4.0.36: ac (AdaptiveCap) 取代 dsem 後, 必須喊 ac.shutdown() 否則
                # 卡 acquire() 的 thread 永遠 hang → finally pool.shutdown(wait=True) deadlock
                if ac: ac.shutdown()
                if dsem: dsem.shutdown()
                pool.shutdown(wait=False, cancel_futures=True)
                cur_active = dsem.active if dsem else actual_workers
                log_fn(f'  [image_opt] 收到用戶停止, 提早結束 (已跑 {done}/{len(tasks)}, in-flight {cur_active} 個跑完)')
                break
            try:
                idx, out_path, all_paths = f.result()
                src = all_paths[0] if all_paths else ''
                code = os.path.basename(os.path.dirname(src)) if src else '?'
                if out_path:
                    success_codes.append(code)
                    # V65: 優先讀 _reject_indices (全圖 reject), V64 兼容讀 _reject_first_img
                    reject_indices = set()
                    if '_reject_indices' in df.columns:
                        rv = df.at[idx, '_reject_indices']
                        if rv and isinstance(rv, str):
                            try:
                                reject_indices = set(int(x) for x in rv.split(',') if x.strip())
                            except: pass
                    if not reject_indices and '_reject_first_img' in df.columns:
                        # 兼容: 沒 _reject_indices 但有 _reject_first_img → index 0
                        rj_val = df.at[idx, '_reject_first_img']
                        if isinstance(rj_val, bool):
                            if rj_val: reject_indices = {0}
                        elif isinstance(rj_val, str):
                            if rj_val.strip().lower() in ('true','是','yes','1'):
                                reject_indices = {0}
                    # 過濾路徑列表
                    kept_paths = [p for i, p in enumerate(all_paths) if i not in reject_indices]
                    # ★ 去重: 防 all_paths 已含 0.jpg (resume case), 避免重複加
                    new_paths = [out_path]
                    seen = {out_path}
                    for p in kept_paths:
                        if p not in seen:
                            seen.add(p); new_paths.append(p)
                    new_field = '|'.join(new_paths)
                    if reject_indices:
                        if 0 in reject_indices:
                            n_reject_first_applied += 1
                        n_reject_indices_total += len(reject_indices)
                    df.at[idx, '圖片'] = new_field
                    n_ok += 1
                else:
                    n_fail += 1
                    fail_codes.append((code, '生成失敗'))
            except Exception as e:
                # ★ PauseException 不能吞, 傳到 pipeline 觸發 paused
                if 'PauseException' in type(e).__name__: raise
                log_fn(f'  ❌ 任務異常: {str(e)[:100]}')
                n_fail += 1
                fail_codes.append(('?', f'異常: {str(e)[:60]}'))
            done += 1
            # ★ Checkpoint: 區分 mark_done / mark_failed
            #   - 成功 (有 0.jpg) → mark_done
            #   - 個別圖失敗 (stream_disconnected/middleware_timeout/違禁) → mark_done (不重試, 重試也沒用)
            #   - API 失效 (HTTP 5xx/upstream_error/exception) → mark_failed (下次繼續會重試)
            if ckpt is not None:
                try:
                    src_path = (futures.get(f) or (None, None, None, []))[1]
                    if src_path:
                        # 看 _last_fail_info 判斷類型
                        fail_type = _last_fail_info.pop(src_path, None)
                        # out_path 有值 = 成功; None + fail_type='individual' = 個別圖; None + 'api_fail' = API 失效
                        if fail_type == 'api_fail':
                            ckpt.mark_failed('image_opt', src_path)  # 下次重試
                        else:
                            ckpt.mark_done('image_opt', src_path)  # 成功 或 個別圖 (不重試)
                    if done % 20 == 0:
                        ckpt.save(df=df)
                except Exception: pass
            if progress_fn:
                progress_fn('圖片優化', done, len(tasks))
            _step = max(1, len(tasks)//10)
            if done == 1 or done % _step == 0 or done == len(tasks):
                elapsed = time.time() - t0
                eta = (len(tasks) - done) * (elapsed / done) if done else 0
                log_fn(f'  [圖片優化] {done}/{len(tasks)} ({done*100/len(tasks):.0f}%) | 耗時 {elapsed:.0f}s | ETA {eta:.0f}s')
            # ★ mid-flight re-ping: 每 200 件 ping /v1/health, 變化 ≥ 8 就調 dsem target
            if dsem is not None and done - last_adjust_done[0] >= 200:
                with adjust_lock:
                    if done - last_adjust_done[0] >= 200:  # double-check
                        last_adjust_done[0] = done
                        try:
                            new_w, w_info2 = get_optimal_workers(task_type='image_edit', warmup=False, max_cap=POOL_MAX)
                            cur_target = dsem.target
                            if abs(new_w - cur_target) >= 8:
                                old, new = dsem.set_target(new_w)
                                log_fn(f'  [image_opt] 🔄 mid-flight 並發調整 {old} → {new} '
                                       f'(中介更新: hubs={w_info2.get("active_hubs", "?")}, my_share={w_info2.get("my_rpm_share", "?")})')
                        except Exception: pass
    finally:
        pool.shutdown(wait=True)  # 確保 in-flight 跑完釋放資源

    # ★ 自動 retry mark_failed 的 items (避免 stage 結束 cleanup ckpt 後永久丟失)
    # 只 retry 因 API 暫時問題失敗的 (mark_failed), 不 retry 個別圖永久問題 (mark_done)
    if ckpt is not None:
        for retry_round in range(1, 3):  # 最多 retry 2 輪
            # 找出 mark_failed 但沒有 0.jpg 的 tasks
            failed_tasks = []
            for orig_t in tasks:
                src_p = orig_t[1]
                # 已 mark_done 的 skip; 已有 0.jpg 的 skip
                if ckpt.is_done('image_opt', src_p): continue
                img_dir = os.path.dirname(src_p)
                if os.path.exists(os.path.join(img_dir, '0.jpg')): continue
                failed_tasks.append(orig_t)
            if not failed_tasks: break
            log_fn(f'  [image_opt] 🔄 自動 retry 第 {retry_round} 輪: {len(failed_tasks)} 件 mark_failed')
            log_fn(f'  [image_opt] 等 30s 讓中介穩定...')
            time.sleep(30)
            # 重 ping 拿最新並發
            try:
                rt_workers, _rt_info = get_optimal_workers(task_type='image_edit', warmup=False, max_cap=POOL_MAX)
            except Exception:
                rt_workers = max(8, actual_workers // 2)
            # ★ 4.0.32: retry 階段並發逐輪砍半 (避免重撞同款 TLS timeout)
            #   高 RTT (218ms) 下 retry 仍用全並發只會繼續失敗 — 之前同事 retry 第 2 輪只 1/20 成功
            try:
                from processors.utils import apply_hub_cap
                capped = apply_hub_cap(rt_workers, retry_round=retry_round)
                if capped < rt_workers:
                    log_fn(f'  [image_opt] retry 並發砍半: {rt_workers} → {capped} (高 RTT/retry 階段)')
                    rt_workers = capped
            except Exception: pass
            log_fn(f'  [image_opt] retry 並發: {rt_workers}')
            # 跑 retry
            rt_pool = ThreadPoolExecutor(max_workers=rt_workers)
            rt_dsem = DynamicSemaphore(rt_workers) if DynamicSemaphore else None
            def _rt_run(t):
                if rt_dsem and not rt_dsem.acquire(): return None, None, []
                try: return _run_orig(t)
                finally:
                    if rt_dsem: rt_dsem.release()
            rt_futures = {rt_pool.submit(_rt_run, t): t for t in failed_tasks}
            rt_done = 0
            rt_ok = 0
            try:
                for f in as_completed(rt_futures):
                    if _mon_io and getattr(_mon_io, 'user_stop', False):
                        if rt_dsem: rt_dsem.shutdown()
                        rt_pool.shutdown(wait=False, cancel_futures=True)
                        log_fn(f'  [image_opt retry] 收到用戶停止')
                        break
                    try:
                        idx, out_path, all_paths = f.result()
                        rt_done += 1
                        src_p = (rt_futures.get(f) or (None, None, None, []))[1]
                        if out_path:
                            rt_ok += 1
                            success_codes.append(os.path.basename(os.path.dirname(src_p)) if src_p else '?')
                            # 把 0.jpg 加進 df 圖片欄
                            reject_indices = set()
                            if '_reject_indices' in df.columns:
                                rv = df.at[idx, '_reject_indices']
                                if rv and isinstance(rv, str):
                                    try: reject_indices = set(int(x) for x in rv.split(',') if x.strip())
                                    except: pass
                            kept_paths = [p for i, p in enumerate(all_paths) if i not in reject_indices]
                            # ★ 去重: all_paths 可能已含 0.jpg (resume case), 避免重複加
                            new_paths = [out_path]
                            seen = {out_path}
                            for p in kept_paths:
                                if p not in seen:
                                    seen.add(p); new_paths.append(p)
                            df.at[idx, '圖片'] = '|'.join(new_paths)
                            n_ok += 1
                            n_fail = max(0, n_fail - 1)
                            if src_p: ckpt.mark_done('image_opt', src_p)
                        else:
                            # 失敗仍 mark_failed, 下輪會再 retry (如果還在 2 輪內)
                            if src_p:
                                fail_type = _last_fail_info.pop(src_p, None)
                                if fail_type == 'api_fail':
                                    ckpt.mark_failed('image_opt', src_p)
                                else:
                                    ckpt.mark_done('image_opt', src_p)  # 個別圖 → 永久 skip
                    except Exception as e:
                        if 'PauseException' in type(e).__name__: raise
            finally:
                rt_pool.shutdown(wait=True)
            log_fn(f'  [image_opt] 🔄 retry 第 {retry_round} 輪完成: {rt_ok}/{len(failed_tasks)} 成功')
            if rt_ok == 0: break  # 全部失敗就不再 retry

    # 對「跳過局部特寫」的商品也應用 _reject_indices 過濾 (不生 0.jpg, 但仍清髒圖)
    n_close_up_filtered = 0
    if n_skip_close_up > 0:
        for idx, row in df.iterrows():
            if has_reason is not None and has_reason.loc[idx]: continue
            sk = row.get('_skip_image_opt') if '_skip_image_opt' in df.columns else None
            is_close_up = (isinstance(sk,bool) and sk) or (isinstance(sk,str) and sk.strip().lower() in ('true','是','yes','1'))
            if not is_close_up: continue
            img_field = str(row.get('圖片','') or '')
            paths = [p.strip() for p in img_field.split('|') if p.strip()]
            if not paths: continue
            reject_indices = set()
            if '_reject_indices' in df.columns:
                rv = df.at[idx,'_reject_indices']
                if rv and isinstance(rv,str):
                    try: reject_indices = set(int(x) for x in rv.split(',') if x.strip())
                    except: pass
            kept = [p for i,p in enumerate(paths) if i not in reject_indices]
            if kept and len(kept) < len(paths):
                df.at[idx,'圖片'] = '|'.join(kept)
                n_close_up_filtered += 1

    elapsed = time.time() - t0
    log_fn(f'[圖片優化] 完成: 成功 {n_ok} | 失敗 {n_fail} | 跳過違禁 {n_skip_filter + n_skip_no_img} | 跳過局部特寫 {n_skip_close_up} (其中清髒圖 {n_close_up_filtered}) | 棄 1.jpg: {n_reject_first_applied} | 棄圖總數: {n_reject_indices_total} | 耗時 {elapsed:.0f}s')
    # ★ 詳細日誌: 失敗詳情 + 成功列表
    if fail_codes:
        log_detail(f'[圖片優化] 失敗詳情 ({len(fail_codes)} 件):')
        for code, reason in fail_codes:
            log_detail(f'    ❌ {code} → {reason}')
    if success_codes:
        log_detail(f'[圖片優化] 成功列表 ({len(success_codes)} 件): {", ".join(success_codes[:50])}{" ..." if len(success_codes)>50 else ""}')

    return {
        'total': len(df),
        'optimized': n_ok,
        'failed': n_fail,
        'skipped_filter': n_skip_filter + n_skip_no_img,
        'skipped_close_up': n_skip_close_up,
        'reject_first_applied': n_reject_first_applied,
        'reject_imgs_total': n_reject_indices_total,
        'elapsed': elapsed,
    }
