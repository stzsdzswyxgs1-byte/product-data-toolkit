# -*- coding: utf-8 -*-
"""闲鱼采集 — 診斷腳本 (由 诊断.bat 呼叫)

一條一條檢查「電腦 → adb → USB 線 → 手機 → phone server (port 10102)」這條鏈,
任一段斷掉就清楚指出哪段 + 怎麼修. 不會修任何東西, 只報告.

順序符合啟動流程:
  1. Python + 套件
  2. adb.exe 路徑
  3. adb devices (有沒插線 / 手機開不開 USB 調試)
  4. phone_config.py (DEVICE_SERIAL / PHONE_IP / PHONE_UTDID)
  5. adb device 跟 phone_config DEVICE_SERIAL 是否一致
  6. adb forward 是否已建 tcp:10102 (= 启动USB转发.bat 是否跑過)
  7. http://PHONE_IP:10102/test 是否回 ok
  8. http://PHONE_IP:10102/request 是否回 APP header (包含 x-utdid)
  9. utdid 跟 phone_config PHONE_UTDID 是否一致
"""
from __future__ import annotations
import sys
import os
import json
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ADB = HERE.parent / 'platform-tools' / 'adb.exe'
PHONE_CONFIG = HERE / 'phone_config.py'

# 統計
PASS = 0
FAIL = 0
WARN_COUNT = 0


def OK(msg: str):
    global PASS
    PASS += 1
    print(f'  ✓ {msg}')


def FAIL_(msg: str, fix: str = ''):
    global FAIL
    FAIL += 1
    print(f'  ✗ {msg}')
    if fix:
        for line in fix.split('\n'):
            print(f'      → {line}')


def WARN(msg: str, fix: str = ''):
    global WARN_COUNT
    WARN_COUNT += 1
    print(f'  ⚠ {msg}')
    if fix:
        for line in fix.split('\n'):
            print(f'      → {line}')


def section(title: str):
    print(f'\n[{title}]')


def http_get(url: str, timeout: int = 3):
    """GET, 返回 (status, body_bytes); 失敗 raise."""
    req = urllib.request.Request(url, headers={'User-Agent': 'goofish-diag/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ============================================================
# 1. Python + 必要套件
# ============================================================
def check_python():
    section('1. Python 環境')
    pv = sys.version_info
    if pv >= (3, 8):
        OK(f'Python {pv.major}.{pv.minor}.{pv.micro}')
    else:
        FAIL_(f'Python {pv.major}.{pv.minor} 太舊 (要 >= 3.8)',
              '裝 Python 3.10+ 然後重跑「1_依赖.bat」')

    # 必要套件
    for pkg in ('requests', 'curl_cffi', 'openpyxl'):
        try:
            __import__(pkg)
            OK(f'已裝 {pkg}')
        except ImportError:
            FAIL_(f'沒裝 {pkg}', f'cmd 執行: pip install {pkg}\n或者重跑「1_依赖.bat」')


# ============================================================
# 2. adb.exe 是否存在
# ============================================================
def check_adb():
    section('2. adb.exe (USB 橋接工具)')
    if ADB.exists():
        OK(f'adb.exe 在 {ADB}')
        return True
    else:
        FAIL_(f'adb.exe 不在 {ADB}',
              '工具包應該帶 platform-tools/, 是不是被刪了?\n'
              '解壓「工具包4.0.zip」確認 platform-tools/ 跟 闲鱼采集0420/ 在同一層')
        return False


# ============================================================
# 3. adb devices — 看到實體手機?
# ============================================================
def check_adb_devices():
    section('3. adb devices (USB 線插了沒?)')
    try:
        r = subprocess.run([str(ADB), 'devices'], capture_output=True, text=True,
                           timeout=8, encoding='utf-8', errors='replace')
    except FileNotFoundError:
        FAIL_('叫不出 adb.exe', '前一步已報 adb.exe 不存在')
        return None
    except subprocess.TimeoutExpired:
        FAIL_('adb devices 卡住 8 秒', '\n'.join([
            'adb 服務卡死, cmd 執行:',
            f'  "{ADB}" kill-server',
            f'  "{ADB}" start-server']))
        return None

    if r.returncode != 0:
        FAIL_(f'adb devices 失敗 (exit {r.returncode}): {r.stderr.strip()[:120]}')
        return None

    real = []
    emu = []
    for line in r.stdout.split('\n'):
        line = line.strip()
        if not line or line.startswith('List') or '\t' not in line:
            continue
        serial, state = line.split('\t', 1)
        state = state.strip()
        if state != 'device':
            WARN(f'設備 {serial} 狀態異常: {state!r}',
                 'unauthorized = 手機跳出彈窗請按「允許」 + 勾「永遠允許」\n'
                 'offline = 拔線重插 / 手機重啟 USB 調試')
            continue
        if serial.startswith('emulator-') or serial.startswith('sigma'):
            emu.append(serial)
        else:
            real.append(serial)

    if not real and not emu:
        FAIL_('adb 看不到任何手機', '\n'.join([
            '依序檢查:',
            '1. USB 線真的插上了嗎? (兩端都要 — 手機跟電腦)',
            '2. 手機 已開「開發者選項」 → 「USB 調試」?',
            '3. 手機跳「允許 USB 調試」彈窗時要按「允許」+ 勾「永遠允許這台電腦」',
            '4. 換條 USB 線 (有些線只供電不傳資料)',
            '5. 試另一個 USB 孔']))
        return None

    if emu and not real:
        WARN(f'只看到模擬器 ({emu}), 沒實體手機',
             '實體手機 USB 沒接好, 模擬器跑閒魚 APP 風控會跳; 接實體手機')

    for s in real:
        OK(f'實體手機: {s}')
    for s in emu:
        print(f'    (略) 模擬器: {s}')

    return real[0] if real else (emu[0] if emu else None)


# ============================================================
# 4. phone_config.py 讀值
# ============================================================
def read_phone_config():
    section('4. phone_config.py')
    if not PHONE_CONFIG.exists():
        FAIL_(f'{PHONE_CONFIG.name} 不存在',
              '跑「1_依赖.bat」會自動建; 或手動建檔, 內容:\n'
              'DEVICE_SERIAL = "你的手機adb序號"\n'
              'PHONE_IP = "127.0.0.1"')
        return {}
    try:
        ns = {}
        with open(PHONE_CONFIG, 'r', encoding='utf-8') as f:
            exec(f.read(), ns)
    except Exception as e:
        FAIL_(f'讀 phone_config.py 失敗: {e}',
              '檔案語法壞了, 砍掉重跑「1_依赖.bat」')
        return {}

    cfg = {k: v for k, v in ns.items() if not k.startswith('_') and k.isupper()}
    serial = cfg.get('DEVICE_SERIAL', '')
    ip = cfg.get('PHONE_IP', '')
    utdid = cfg.get('PHONE_UTDID', '')

    if serial:
        OK(f'DEVICE_SERIAL = {serial!r}')
    else:
        WARN('DEVICE_SERIAL 是空的', '第一次跑會自動填; 也可以等下流程跑出來再驗')

    if ip:
        OK(f'PHONE_IP = {ip!r}')
    else:
        FAIL_('PHONE_IP 是空的', '應該是 127.0.0.1 (USB) 或 192.168.x.x (WiFi)')

    if utdid:
        OK(f'PHONE_UTDID = {utdid[:24]}... ({len(utdid)} 字元)')
    else:
        WARN('PHONE_UTDID 是空的 (多手機環境會分不清)',
             '不影響單手機; 第一次連上後會自動填')

    return cfg


# ============================================================
# 5. adb device 跟 config 對得上嗎
# ============================================================
def check_serial_match(adb_serial, cfg):
    section('5. config 跟 adb 對齊')
    cfg_serial = cfg.get('DEVICE_SERIAL', '')
    if not cfg_serial:
        WARN('config 沒 DEVICE_SERIAL, 跳過比對')
        return
    if not adb_serial:
        WARN('adb 沒看到設備, 跳過比對')
        return
    if cfg_serial == adb_serial:
        OK(f'兩邊一致: {adb_serial}')
    else:
        WARN(f'config DEVICE_SERIAL={cfg_serial!r}  ≠  adb 看到的 {adb_serial!r}',
             '是不是換了手機? 砍掉 phone_config.py 重跑會自動更新')


# ============================================================
# 6. adb forward tcp:10102
# ============================================================
def check_adb_forward():
    section('6. adb forward (USB 端口轉發)')
    try:
        r = subprocess.run([str(ADB), 'forward', '--list'], capture_output=True,
                           text=True, timeout=5, encoding='utf-8', errors='replace')
    except Exception as e:
        FAIL_(f'adb forward --list 失敗: {e}')
        return False

    has_10102 = False
    for line in r.stdout.split('\n'):
        line = line.strip()
        if 'tcp:10102' in line:
            has_10102 = True
            OK(f'已建立: {line}')
    if not has_10102:
        FAIL_('沒看到 tcp:10102 forward',
              '雙擊「启动USB转发.bat」先建 forward, 再跑「闲鱼采集器.bat」')
        return False
    return True


# ============================================================
# 7-9. phone server HTTP
# ============================================================
def check_phone_server(cfg):
    section('7. phone server /test (端口 10102 健康檢查)')
    ip = cfg.get('PHONE_IP', '127.0.0.1') or '127.0.0.1'
    base = f'http://{ip}:10102'

    # 7. /test
    try:
        st, body = http_get(f'{base}/test', timeout=3)
        if st == 200 and b'"ok"' in body:
            OK(f'{base}/test → 200 ok')
        else:
            FAIL_(f'{base}/test 回應不對: status={st}, body={body[:120]!r}',
                  '手機 server 跑了但回應壞了, 重啟手機端 server APP')
            return
    except Exception as e:
        FAIL_(f'{base}/test 連不上: {type(e).__name__}: {e}', '\n'.join([
            '手機端 phone server (sigma 之類) 沒跑, 或 forward 沒建好',
            '檢查順序:',
            '  1. 手機解鎖, 打開 phone server APP (通常叫 sigma / mtop_signer)',
            '  2. 桌面 cmd 執行: "..\\platform-tools\\adb.exe" forward --list',
            '     看到 tcp:10102 才表示 USB forward 通了',
            '  3. 沒看到 → 雙擊「启动USB转发.bat」']))
        return

    # 8. /request
    section('8. phone server /request (拿 APP header 模板)')
    try:
        st, body = http_get(f'{base}/request?count=1', timeout=5)
        if st != 200:
            FAIL_(f'{base}/request HTTP {st}')
            return
        data = json.loads(body)
        raws = data.get('req', [])
        if not raws:
            FAIL_('/request 回空 req 列表', '\n'.join([
                '手機 server 還沒抓到 APP 真實 header.',
                '解法: 手機解鎖, 打開閒魚 APP, 隨便點一個商品讓它發個請求,',
                'phone server 才會擷到 header 模板.']))
            return
        OK(f'/request 回 {len(raws)} 個 header 模板')
        # 解 utdid
        adb_utdid = None
        for kv in raws[0].split(', '):
            if kv.startswith('x-utdid='):
                adb_utdid = urllib.parse.unquote(kv[len('x-utdid='):])
                break
        if adb_utdid:
            OK(f'手機回的 x-utdid = {adb_utdid[:24]}...')
        else:
            WARN('header 模板沒帶 x-utdid', '手機 server 版本舊?')

        # 9. utdid 跟 config 比對
        section('9. utdid 一致性 (多手機環境分辨)')
        cfg_utdid = cfg.get('PHONE_UTDID', '')
        if not cfg_utdid:
            WARN('config 沒 PHONE_UTDID, 跳過比對')
        elif not adb_utdid:
            WARN('手機沒回 utdid, 跳過比對')
        elif cfg_utdid == adb_utdid:
            OK(f'兩邊一致: {adb_utdid[:24]}...')
        else:
            WARN(f'\n      config: {cfg_utdid[:24]}...\n      手機:   {adb_utdid[:24]}...',
                 '是不是換了手機? 砍掉 phone_config.py 重跑會自動更新')
    except Exception as e:
        FAIL_(f'{base}/request 失敗: {type(e).__name__}: {e}',
              '/test 通了但 /request 不通, phone server 半死狀態, 重啟手機端 APP')


# ============================================================
# Main
# ============================================================
def main():
    print('=' * 60)
    print('  闲鱼采集 — 環境診斷')
    print('=' * 60)

    check_python()
    if not check_adb():
        finish()
        return

    adb_serial = check_adb_devices()
    cfg = read_phone_config()
    check_serial_match(adb_serial, cfg)

    if check_adb_forward():
        check_phone_server(cfg)

    finish()


def finish():
    print()
    print('=' * 60)
    if FAIL == 0 and WARN_COUNT == 0:
        print(f'  ✅ 全綠 ({PASS} 項通過) — 環境 OK, 可以跑「闲鱼采集器.bat」')
    elif FAIL == 0:
        print(f'  ⚠ {PASS} 項通過, {WARN_COUNT} 個警告 — 大致 OK 但看上面 ⚠ 訊息')
    else:
        print(f'  ❌ {PASS} 項通過, {FAIL} 個失敗, {WARN_COUNT} 個警告')
        print(f'     先按上面 → 提示修, 全綠了再跑「闲鱼采集器.bat」')
    print('=' * 60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n用戶中斷')
    except Exception as e:
        print(f'\n[診斷腳本本身崩了] {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
