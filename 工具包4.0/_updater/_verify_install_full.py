# -*- coding: utf-8 -*-
"""完整 audit zip 內容: 各 app sanitized config + 殘留檢查"""
import zipfile, json, sys
sys.stdout.reconfigure(encoding='utf-8')

zf = zipfile.ZipFile(r'C:/Users/USERNAME\Desktop\工具包4.0_安裝包.zip')
names = zf.namelist()
print(f'總 {len(names)} 檔\n')

# ── 必要檔 ──
must_have = [
    '工具包4.0/商品處理中樞/config.json',     # ★ 4.0.X fix: 必須含
    '工具包4.0/商品處理中樞/app.py',
    '工具包4.0/商品處理中樞/big-lama.pt',
    '工具包4.0/_updater/updater.py',
    '工具包4.0/SEO翻譯工具6.0/translator_config.json',
    '工具包4.0/SEO翻譯工具6.0/gui.py',
    '工具包4.0/闲鱼采集0420/config.json',     # ★ 4.0.X fix: 含 export_mirror.upload_url
    '工具包4.0/闲鱼采集0420/app.py',
    '工具包4.0/煤爐采集0324/server.js',
    '工具包4.0/INSTALL.txt',
]
print('=== 必要檔 ===')
for f in must_have:
    print(f'  {"OK" if f in names else "MISSING!"}  {f}')

# ── 安全/殘留 ──
print('\n=== 安全 / 殘留檢查 ===')
forbidden = [
    # 真正敏感 — admin only, 必須排除
    'update_admin_config',    # ★ admin push key, 同事拿到能假冒推假版本!
    'admin_review.py',
    'pack_update.py',
    'verify_push.py',
    'pack_install_zip',
    '_smoke_', '_check_', '_debug_', '_review_',
    'CLAUDE_HANDOFF', 'DESIGN.md',
    '_dryrun',                # admin dryrun zip
    # build artifacts
    '__pycache__',
    # admin 跑批中間態 (同事新環境不該繼承)
    '_checkpoint',
    '詳細日誌_',
    '/output/',               # admin 跑批輸出
    # admin 內部測試
    '_test/',
    # ★ 用戶要求保留 (不該在 forbidden):
    #   cookies.json / *.db / *.db-wal / .session / .pending / /data/ / ckpt_batch
]
leaked = []
for n in names:
    for p in forbidden:
        if p in n:
            leaked.append((n, p))
            break
if leaked:
    print('  *** LEAKED ***')
    for n, p in leaked:
        print(f'    {n}  (matched: {p})')
else:
    print('  OK 沒洩漏')

# ── 三個 sanitized config 內容驗證 ──
print('\n=== 商品處理中樞/config.json (sanitized) ===')
with zf.open('工具包4.0/商品處理中樞/config.json') as f:
    cfg = json.loads(f.read().decode('utf-8'))
print(f'  tg_id: {cfg["tg_id"]!r} (期空)')
print(f'  last_input_file: {cfg["last_input_file"]!r} (期空)')
print(f'  output_dir: {cfg["output_dir"]!r} (期空)')
print(f'  ★ quota.endpoint: {cfg["quota"]["endpoint"]}')
print(f'  ★ quota.client_secret: {cfg["quota"]["client_secret"][:10]}...')

print('\n=== 闲鱼采集0420/config.json (sanitized) ===')
with zf.open('工具包4.0/闲鱼采集0420/config.json') as f:
    cfg = json.loads(f.read().decode('utf-8'))
print(f'  export_dir: {cfg["export_dir"]!r} (期空)')
print(f'  img_dir: {cfg["img_dir"]!r} (期空)')
print(f'  geometry: {cfg["geometry"]!r} (期空)')
print(f'  cloud_sync.password: {cfg["cloud_sync"]["password"]!r} (期空)')
print(f'  ★ export_mirror.upload_url: {cfg["export_mirror"]["upload_url"]}')
print(f'  ★ export_mirror.token: {cfg["export_mirror"]["token"]}')
print(f'  ★ mode_texts.detail: {cfg["mode_texts"]["detail"][:40]}... (店家 URL 保留)')

print('\n=== SEO翻譯工具6.0/translator_config.json (sanitized) ===')
with zf.open('工具包4.0/SEO翻譯工具6.0/translator_config.json') as f:
    cfg = json.loads(f.read().decode('utf-8'))
print(f'  last_input: {cfg["last_input"]!r} (期空)')
print(f'  last_output: {cfg["last_output"]!r} (期空)')
print(f'  ★ api_url: {cfg["api_url"]}')
print(f'  ★ api_key: {cfg["api_key"][:8]}...')

# ── 大檔 ──
print('\n=== 大檔 (>1MB) ===')
for n in sorted(names):
    info = zf.getinfo(n)
    if info.file_size > 1024 * 1024:
        print(f'  {info.file_size/1024/1024:>6.1f} MB  {n}')

print('\n=== zip 大小 ===')
import os
print(f'  {os.path.getsize(r"C:/Users/USERNAME\Desktop\工具包4.0_安裝包.zip") / 1024 / 1024:.1f} MB')
