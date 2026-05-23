# -*- coding: utf-8 -*-
"""驗證 工具包4.0_安裝包.zip 內容 — 必要檔在 + 沒洩漏 admin only"""
import zipfile, sys
sys.stdout.reconfigure(encoding='utf-8')

zf = zipfile.ZipFile(r'C:/Users/USERNAME\Desktop\工具包4.0_安裝包.zip')
names = zf.namelist()
print(f'總 {len(names)} 檔')

print('\n=== 必要檔在? ===')
must_have = [
    # 商品處理中樞
    '工具包4.0/商品處理中樞/app.py',
    '工具包4.0/商品處理中樞/pipeline.py',
    '工具包4.0/商品處理中樞/start.bat',
    '工具包4.0/商品處理中樞/setup_env.py',
    '工具包4.0/商品處理中樞/big-lama.pt',
    '工具包4.0/商品處理中樞/requirements.txt',
    '工具包4.0/商品處理中樞/processors/utils.py',
    '工具包4.0/商品處理中樞/processors/_update_check.py',
    # _updater
    '工具包4.0/_updater/updater.py',
    '工具包4.0/_updater/app_registry.py',
    '工具包4.0/_updater/update_config.json',
    # 4 個 app start
    '工具包4.0/SEO翻譯工具6.0/gui.py',
    '工具包4.0/煤爐采集0324/start.bat',
    '工具包4.0/煤爐采集0324/server.js',
    '工具包4.0/煤爐采集0324/lib/mercari.js',
    '工具包4.0/闲鱼采集0420/app.py',
    '工具包4.0/闲鱼采集0420/goofish_collector.py',
    '工具包4.0/手机端/setup_phone.py',
    '工具包4.0/手机端/一键配置手机.bat',
    '工具包4.0/platform-tools/adb.exe',
    # 安裝說明
    '工具包4.0/INSTALL.txt',
]
for f in must_have:
    print(f'  {"OK" if f in names else "MISSING!"}  {f}')

print('\n=== 安全: 不該洩漏 admin only ===')
forbidden = ['update_admin_config', 'admin_review', 'pack_update', 'verify_push',
             'pack_install_zip', '_smoke_', '_check_', '_debug_', '_review_',
             'CLAUDE_HANDOFF', 'DESIGN.md', '_dryrun', '__pycache__',
             'cookies.json', '.session', 'collector.db', '.pending_exports',
             '/data/', 'ckpt_batch']
leaked = []
for n in names:
    for p in forbidden:
        if p in n:
            leaked.append(n)
            break
if leaked:
    print('*** LEAKED:')
    for n in leaked:
        print(f'  {n}')
else:
    print('  OK 沒洩漏')

print('\n=== _updater/ 完整內容 ===')
for n in sorted(names):
    if '_updater/' in n:
        info = zf.getinfo(n)
        print(f'  {info.file_size:>10} bytes  {n}')

print('\n=== 大檔 (>1MB) ===')
for n in sorted(names):
    info = zf.getinfo(n)
    if info.file_size > 1024 * 1024:
        print(f'  {info.file_size/1024/1024:>6.1f} MB  {n}')

print('\n=== _updater/update_config.json 內容 (確認沒 admin_key) ===')
with zf.open('工具包4.0/_updater/update_config.json') as f:
    print(f.read().decode('utf-8'))
