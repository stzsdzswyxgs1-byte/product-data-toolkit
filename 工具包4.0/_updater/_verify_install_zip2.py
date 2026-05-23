# -*- coding: utf-8 -*-
"""驗證 sanitized config.json"""
import zipfile, json, sys
sys.stdout.reconfigure(encoding='utf-8')

zf = zipfile.ZipFile(r'C:/Users/USERNAME\Desktop\工具包4.0_安裝包.zip')
names = zf.namelist()

# 看哪幾個 config.json 進 zip
print('=== 所有 config.json 路徑 ===')
for n in sorted(names):
    if n.endswith('config.json'):
        print(f'  {n}')

print('\n=== 商品處理中樞 config.json (sanitize 後) ===')
with zf.open('工具包4.0/商品處理中樞/config.json') as f:
    cfg = json.loads(f.read().decode('utf-8'))
print(f'  tg_id: {cfg["tg_id"]!r}  (期空)')
print(f'  last_input_file: {cfg["last_input_file"]!r}  (期空)')
print(f'  output_dir: {cfg["output_dir"]!r}  (期空)')
print(f'  desc_templates.selected: {cfg.get("desc_templates",{}).get("selected", "?")!r}  (期空)')
print()
print(f'  ★ admin 共用設定 (該保留):')
print(f'  quota.endpoint: {cfg["quota"]["endpoint"]}')
print(f'  quota.mode: {cfg["quota"]["mode"]}')
print(f'  quota.client_secret: {cfg["quota"]["client_secret"][:8]}...')
print(f'  paths.replace_xlsx: {cfg["paths"]["replace_xlsx"]}')
print(f'  steps.image_opt.enabled: {cfg["steps"]["image_opt"]["enabled"]}')
