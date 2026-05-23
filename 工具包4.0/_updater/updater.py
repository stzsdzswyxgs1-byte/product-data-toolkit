# -*- coding: utf-8 -*-
"""工具包4.0 雲端更新器 — 同步式, 啟動時跑一次, 失敗不 block.

用法:
    python updater.py --app product_hub
    python updater.py --app product_hub --dry-run        # 只查不下載
    python updater.py --app product_hub --force          # 忽略 lock 強跑

主要 invariants (不能違反):
    1. 失敗永遠不 block app 啟動 (exit 0)
    2. 寫 current_version.txt 是流程最後一步, 之前任何失敗都不寫
    3. PROTECTED 模式: zip 內含 PROTECTED 檔 → 拒絕整個 zip
    4. 解壓兩階段: staging dir → 全成功才 atomic move 到正式位置
    5. 純 stdlib: 不依賴 requests / pyyaml / 任何 .venv 內套件
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# 強制 UTF-8 stdout (Windows console 預設 GBK 會印亂碼)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

# 路徑: _updater/updater.py → 4.0/_updater/.. = 4.0/
HERE = Path(__file__).resolve().parent
TOOLKIT_ROOT = HERE.parent

# 把 _updater/ 加進 import path 以便 import app_registry
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app_registry import APPS, get_app, validate_version, version_gt  # noqa: E402


# ────────────────────────────────────────────────────────────────
# 配置 + log
# ────────────────────────────────────────────────────────────────

CONFIG_PATH = HERE / 'update_config.json'
LOCK_STALENESS_SEC = 1800       # 30 分鐘無條件搶 lock (NTP 容差)
DOWNLOAD_TIMEOUT_SEC = 120      # zip 下載
CHECK_TIMEOUT_SEC = 5           # version check (失敗就跳過)
TEMP_AGE_THRESHOLD = 3600       # 殘檔 1 小時清理

LOG_PREFIX = '[updater]'


def log(msg, level='info'):
    """同步 log 到 stdout. start.bat 會看到."""
    icon = {'info': '', 'warn': '⚠ ', 'err': '✗ ', 'ok': '✓ '}.get(level, '')
    print(f'{LOG_PREFIX} {icon}{msg}', flush=True)


def load_config():
    """讀 update_config.json. 缺/壞 → log + return None (不 block)."""
    if not CONFIG_PATH.exists():
        log(f'config 不存在 ({CONFIG_PATH.name}), 跳過更新', 'warn')
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception as e:
        log(f'config 讀取失敗: {e}', 'warn')
        return None
    worker_url = cfg.get('worker_url', '').rstrip('/')
    read_key = cfg.get('read_key', '')
    if not worker_url or not read_key:
        log('config 缺 worker_url 或 read_key, 跳過更新', 'warn')
        return None
    return {'worker_url': worker_url, 'read_key': read_key}


# ────────────────────────────────────────────────────────────────
# Lock (per-app, 雙條件: PID 活著 + mtime 在 30 分鐘內)
# ────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """跨平台 PID 存活檢測. Windows 用 OpenProcess, Unix 用 kill 0."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
            if not handle:
                # 不存在或無權限. ERROR_INVALID_PARAMETER (87) 表示 PID 不存在
                err = kernel32.GetLastError()
                if err == 87:
                    return False
                # 無權限 → 視為活著 (保守, 不誤搶 system PID)
                return True
            try:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True   # 出錯保守視為活, 不亂搶
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return True


class LockHeld(Exception):
    """別人持有 lock, 跳過 update."""


def acquire_lock(app_folder: Path, force: bool = False):
    """取得 per-app lock. 雙條件:
       - PID 活著且 mtime < 30 分鐘 → 別人在跑, raise LockHeld
       - 否則 → 強搶 (mtime > 30min 或 PID 死)
    返回 lock_path (給 finally 用 release_lock 釋放).
    """
    lock_path = app_folder / '.updater.lock'

    if force and lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass

    for attempt in range(3):
        try:
            # O_EXCL 原子建立
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f'{os.getpid()}|{int(time.time())}|{socket.gethostname()}'.encode('utf-8'))
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            pass

        # 已存在: 檢查持有者
        try:
            mtime = lock_path.stat().st_mtime
            content = lock_path.read_text(encoding='utf-8', errors='replace').strip()
            old_pid = 0
            try:
                old_pid = int(content.split('|')[0])
            except (ValueError, IndexError):
                pass

            mtime_age = time.time() - mtime
            if mtime_age > LOCK_STALENESS_SEC:
                # 太老, 強搶 (NTP 容差: OS mtime 比 lock 內 timestamp 可靠)
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
            elif not _pid_alive(old_pid):
                # PID 死了, 搶
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
            else:
                raise LockHeld(f'另一個 updater 在跑 (pid={old_pid}, age={mtime_age:.0f}s)')
        except FileNotFoundError:
            # 在我們檢查時 lock 被別人刪了, 重試
            continue
    raise LockHeld('lock 競爭失敗 (3 retry 用完)')


def release_lock(lock_path: Path):
    """釋放 lock. 失敗不 raise."""
    try:
        if lock_path.exists():
            content = lock_path.read_text(encoding='utf-8', errors='replace').strip()
            try:
                old_pid = int(content.split('|')[0])
                if old_pid == os.getpid():
                    lock_path.unlink()
            except (ValueError, IndexError):
                pass
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────
# 殘檔清理 (啟動時跑一次)
# ────────────────────────────────────────────────────────────────

def cleanup_stale(app_folder: Path):
    """清掉 1 小時前的 update_temp 和 staging 殘檔."""
    now = time.time()
    for p in app_folder.glob('.update_temp_*.zip'):
        try:
            if now - p.stat().st_mtime > TEMP_AGE_THRESHOLD:
                p.unlink()
                log(f'清掉殘檔 {p.name}')
        except OSError:
            pass
    staging = app_folder / '.update_staging'
    if staging.exists():
        try:
            if now - staging.stat().st_mtime > TEMP_AGE_THRESHOLD:
                shutil.rmtree(staging, ignore_errors=True)
                log(f'清掉殘 staging dir')
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────
# Worker 通訊 (pure stdlib)
# ────────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict, timeout: int):
    """簡單 GET, 返回 (status, body_bytes). 失敗 raise."""
    req = urllib.request.Request(url, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def check_version(cfg: dict, app_id: str):
    """GET /update/check?app=X. 返回 version.json dict 或 None."""
    url = f'{cfg["worker_url"]}/update/check?app={app_id}'
    headers = {
        'Authorization': f'Bearer {cfg["read_key"]}',
        'User-Agent': 'Mozilla/5.0 toolkit-updater/4.0',
    }
    try:
        status, body = _http_get(url, headers, CHECK_TIMEOUT_SEC)
    except urllib.error.URLError as e:
        log(f'網路問題: {e.reason if hasattr(e, "reason") else e}, 跳過更新', 'warn')
        return None
    except (socket.timeout, TimeoutError):
        log(f'check 超時 ({CHECK_TIMEOUT_SEC}s), 跳過更新', 'warn')
        return None
    except Exception as e:
        log(f'check 失敗: {e}, 跳過更新', 'warn')
        return None

    if status != 200:
        log(f'check 回 HTTP {status}, 跳過更新', 'warn')
        return None
    try:
        return json.loads(body)
    except Exception:
        log('check 回非 JSON, 跳過', 'warn')
        return None


def download_zip(cfg: dict, app_id: str, expected_sha256: str, expected_size: int, dest: Path):
    """串流下載 zip 到 dest, 邊下邊算 SHA256.
    返回 True 成功; False 失敗 (網路/SHA/size 任一不對).
    """
    url = f'{cfg["worker_url"]}/update/download?app={app_id}'
    headers = {
        'Authorization': f'Bearer {cfg["read_key"]}',
        'User-Agent': 'Mozilla/5.0 toolkit-updater/4.0',
    }
    req = urllib.request.Request(url, headers=headers)
    sha = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as r:
            if r.status != 200:
                log(f'download 回 HTTP {r.status}, 跳過', 'warn')
                return False
            with open(dest, 'wb') as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
    except urllib.error.URLError as e:
        log(f'下載網路問題: {e}, 跳過', 'warn')
        return False
    except (socket.timeout, TimeoutError):
        log(f'下載超時 ({DOWNLOAD_TIMEOUT_SEC}s), 跳過', 'warn')
        return False
    except Exception as e:
        log(f'下載失敗: {e}, 跳過', 'warn')
        return False

    actual_sha = sha.hexdigest()
    if expected_sha256 and actual_sha != expected_sha256:
        log(f'SHA256 不對! expect={expected_sha256[:16]}.. got={actual_sha[:16]}..', 'err')
        return False
    if expected_size and written != expected_size:
        log(f'size 不對! expect={expected_size} got={written}', 'err')
        return False
    return True


# ────────────────────────────────────────────────────────────────
# zip 安全檢查 (zip-slip + symlink + protected + whitelist)
# ────────────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """NFKC normalize + casefold (Win 不分大小寫). 用於 pattern match."""
    return unicodedata.normalize('NFKC', name).casefold()


def _is_safe_path(member_name: str, target_dir: Path) -> bool:
    """zip-slip 檢查 (3 層):
       1. 拒 .. / 開頭 / / Win 磁碟字母 / UNC / null / control char
       2. abs path 必須在 target_dir 內 (用 commonpath 在 Win 也對)
       3. 拒 system 路徑 (Windows / system32 等)
    """
    # 1. 字元/前綴黑名單
    if not member_name or len(member_name) > 1024:
        return False
    if '\x00' in member_name:
        return False
    for ch in member_name:
        if ord(ch) < 0x20 and ch not in '\t':
            return False
    if member_name.startswith('/') or member_name.startswith('\\'):
        return False
    if re.match(r'^[a-zA-Z]:', member_name):           # Win 磁碟字母
        return False
    if member_name.startswith('\\\\') or member_name.startswith('//'):
        return False
    parts = member_name.replace('\\', '/').split('/')
    if '..' in parts:
        return False

    # 2. commonpath 檢查 (case-insensitive on Windows)
    target_abs = target_dir.resolve()
    full = (target_dir / member_name).resolve()
    try:
        common = Path(os.path.commonpath([str(target_abs), str(full)]))
    except ValueError:
        return False
    # Windows 大小寫不敏感比較
    if os.name == 'nt':
        if str(common).casefold() != str(target_abs).casefold():
            return False
    else:
        if common != target_abs:
            return False

    # 3. system 路徑黑名單 (paranoid)
    full_low = str(full).lower()
    for s in ('windows\\', 'system32\\', 'program files\\', 'programdata\\',
              '/etc/', '/usr/', '/sys/', '/proc/'):
        if s in full_low:
            return False

    return True


def _is_symlink_entry(zinfo: zipfile.ZipInfo) -> bool:
    """zip 內 entry 是否標記為 symlink (Unix mode S_IFLNK).
    stdlib zipfile 不會還原 symlink, 但防呆: 拒整個 zip.
    """
    mode = zinfo.external_attr >> 16
    return stat.S_ISLNK(mode)


def _match_any(name: str, patterns: list[str]) -> bool:
    """fnmatch any pattern (大小寫不敏感, NFKC). pattern 用 / 為路徑分隔.
    支援 ** 跨層 (例如 'output*/**')."""
    n = _normalize_name(name.replace('\\', '/'))
    for p in patterns:
        pp = _normalize_name(p)
        if '**' in pp:
            # ** 簡化: 把 **/ 換成 .*/, * 不跨 / (但實作上用 fnmatch + recursive)
            # 用 pathlib.PurePath.match 不支援 **, 用 regex
            regex = pp.replace('.', r'\.').replace('**', '__DBLSTAR__').replace('*', '[^/]*').replace('__DBLSTAR__', '.*').replace('?', '[^/]')
            if re.fullmatch(regex, n):
                return True
        else:
            if fnmatch.fnmatchcase(n, pp):
                return True
            # 也試 path-aware: pattern 'config.json' 應 match './config.json' 和 'config.json'
            if '/' not in pp and '/' not in n and fnmatch.fnmatchcase(n, pp):
                return True
    return False


def validate_zip(zip_path: Path, app_cfg: dict, app_folder: Path):
    """預檢 zip:
       - is_zipfile
       - 每個 entry 過 _is_safe_path
       - 拒絕 symlink entry
       - 拒絕含 protected 內容 (admin 不該包)
       - 必須全部 entry 都在 whitelist (拒未授權檔)
    返回 (zinfo_list, error_msg). 成功 error_msg=None.
    """
    if not zipfile.is_zipfile(zip_path):
        return None, 'zip 殘缺'

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zinfos = zf.infolist()
    except zipfile.BadZipFile as e:
        return None, f'zip 解析失敗: {e}'

    if not zinfos:
        return None, 'zip 空的'

    # 大小限制 (防 zip bomb)
    total_uncompressed = sum(z.file_size for z in zinfos)
    if total_uncompressed > 500 * 1024 * 1024:
        return None, f'解壓後超過 500MB ({total_uncompressed/1024/1024:.0f}MB) 拒絕'

    whitelist = app_cfg['whitelist']
    protected = app_cfg['protected']

    for z in zinfos:
        if z.is_dir():
            continue
        # 1. path 安全
        if not _is_safe_path(z.filename, app_folder):
            return None, f'惡意路徑: {z.filename!r}'
        # 2. symlink 拒絕
        if _is_symlink_entry(z):
            return None, f'zip 含 symlink entry: {z.filename!r} 拒絕'
        # 3+4. 雙層檢查: protected 優先 (給清楚錯誤訊息), whitelist 兜底
        in_protected = _match_any(z.filename, protected)
        in_whitelist = _match_any(z.filename, whitelist)
        # 雙在 whitelist + protected 時 (overlap), whitelist 優先, 放行
        # 例: 假如 admin 設 protected 含過廣 pattern 攔到 whitelist 的 current_version.txt
        if in_protected and not in_whitelist:
            return None, f'zip 含 protected 檔 (用戶資料): {z.filename!r}'
        if not in_whitelist:
            return None, f'zip 含 whitelist 外檔: {z.filename!r}'

    return zinfos, None


# ────────────────────────────────────────────────────────────────
# 兩階段解壓
# ────────────────────────────────────────────────────────────────

def two_phase_extract(zip_path: Path, app_folder: Path):
    """Phase 1: 解到 staging dir
       Phase 2: 全成功 → 對每個檔 atomic move 到正式位置
       任何失敗 → 刪 staging, 不動正式檔
    返回 (success, n_files, requirements_changed)
    """
    staging = app_folder / '.update_staging'
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=False)

    requirements_changed = False

    try:
        # Phase 1: 解到 staging
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for z in zf.infolist():
                if z.is_dir():
                    continue
                # 二次安全檢查 (paranoid, validate_zip 已查過)
                if not _is_safe_path(z.filename, staging):
                    raise ValueError(f'unsafe path in extract: {z.filename}')
                target = staging / z.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(z) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)

        # Phase 2: 從 staging atomic move 到 app_folder
        # 先收集所有要 move 的檔
        files_to_move = []
        for staged in staging.rglob('*'):
            if staged.is_file():
                rel = staged.relative_to(staging)
                rel_norm = str(rel).replace('\\', '/').lower()
                # 跳過 current_version.txt — 由 atomic_write_version 在流程最後寫
                # 這樣 pip install 失敗時 version 不會半步更新
                if rel_norm == 'current_version.txt':
                    continue
                files_to_move.append((staged, app_folder / rel, rel))

        # Atomic move (file by file)
        moved = 0
        for staged, real, rel in files_to_move:
            real.parent.mkdir(parents=True, exist_ok=True)
            # requirements.txt 變動? (用 read_text 處理 Windows \r\n vs Unix \n)
            if str(rel).replace('\\', '/').lower() == 'requirements.txt':
                try:
                    if not real.exists():
                        requirements_changed = True
                    else:
                        a = real.read_text(encoding='utf-8', errors='replace')
                        b = staged.read_text(encoding='utf-8', errors='replace')
                        if a.strip() != b.strip():
                            requirements_changed = True
                except Exception:
                    requirements_changed = True
            # PermissionError retry (Windows 文件鎖)
            for attempt in range(3):
                try:
                    os.replace(str(staged), str(real))
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        raise
            moved += 1

        # 刪空的 staging 子目錄
        shutil.rmtree(staging, ignore_errors=True)

        return True, moved, requirements_changed
    except Exception as e:
        log(f'解壓失敗: {e}', 'err')
        shutil.rmtree(staging, ignore_errors=True)
        return False, 0, False


# ────────────────────────────────────────────────────────────────
# pip install (新依賴)
# ────────────────────────────────────────────────────────────────

def auto_pip_install(app_folder: Path) -> bool:
    """跑 pip install -r requirements.txt (用該 app 的 .venv)."""
    venv_python = app_folder / '.venv' / 'Scripts' / 'python.exe'
    if not venv_python.exists():
        venv_python = app_folder / '.venv' / 'bin' / 'python'   # POSIX
    if not venv_python.exists():
        log('沒找到 .venv/python, 跳過 pip install', 'warn')
        return False
    req = app_folder / 'requirements.txt'
    if not req.exists():
        return True
    log('安裝新依賴 (pip install)...')
    try:
        result = subprocess.run(
            [str(venv_python), '-m', 'pip', 'install', '-r', str(req),
             '--disable-pip-version-check', '--no-cache-dir', '--quiet'],
            timeout=600, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        if result.returncode == 0:
            log('依賴安裝完成', 'ok')
            return True
        log(f'pip install 失敗 (rc={result.returncode})', 'err')
        log(f'  stderr: {result.stderr[:300]}')
        return False
    except subprocess.TimeoutExpired:
        log('pip install 超過 10 分鐘, 放棄', 'err')
        return False
    except Exception as e:
        log(f'pip install 異常: {e}', 'err')
        return False


# ────────────────────────────────────────────────────────────────
# Atomic 寫 version.txt
# ────────────────────────────────────────────────────────────────

def atomic_write_version(app_folder: Path, version: str):
    """Atomic 寫 current_version.txt (透過 .tmp + os.replace)."""
    target = app_folder / 'current_version.txt'
    tmp = app_folder / 'current_version.txt.tmp'
    tmp.write_text(version, encoding='utf-8')
    os.replace(str(tmp), str(target))


def read_local_version(app_folder: Path) -> str:
    """讀 local version. 缺/壞回 '0.0.0' (永遠落後, 必觸發更新)."""
    p = app_folder / 'current_version.txt'
    if not p.exists():
        return '0.0.0'
    try:
        v = p.read_text(encoding='utf-8-sig').strip()
        validate_version(v)
        return v
    except Exception:
        return '0.0.0'


# ────────────────────────────────────────────────────────────────
# TG 通知 (best effort)
# ────────────────────────────────────────────────────────────────

def notify_tg(app_id: str, app_cfg: dict, old_ver: str, new_ver: str, changelog: str):
    """讀該 app 的 config.json 找 tg_id + 從 update_config.json 拿 tg_bot_token, 發訊息.
    Opt-in: update_config.json 沒 tg_bot_token 就 silent skip (不污染 zip 給所有同事散布 token).
    失敗不 raise (best effort)."""
    app_folder = TOOLKIT_ROOT / app_cfg['folder']
    cfg_path = app_folder / 'config.json'
    if not cfg_path.exists():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    except Exception:
        return
    tg_id = str(cfg.get('tg_id', '')).strip()
    if not tg_id:
        return
    # bot token: opt-in 從 update_config.json 拿; 沒設則靜默不發
    upd_cfg = load_config()
    bot_token = (upd_cfg or {}).get('tg_bot_token', '') if upd_cfg else ''
    if not bot_token:
        return  # opt-in 未啟用, console log 已夠用
    text = (
        f'🔄 {app_cfg["display_name"]} 已更新\n'
        f'版本: {old_ver} → {new_ver}\n'
    )
    if changelog:
        text += f'更新內容: {changelog}\n'
    text += f'時間: {time.strftime("%Y-%m-%d %H:%M:%S")}'
    try:
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        data = json.dumps({'chat_id': tg_id, 'text': text}).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────

def run_update(app_id: str, dry_run: bool, force: bool) -> int:
    """返回 exit code (永遠 0; 失敗只 log warn)."""
    try:
        validate_version  # noop
        app_cfg = get_app(app_id)
    except ValueError as e:
        log(str(e), 'err')
        return 0   # invalid app id 也不 block

    app_folder = TOOLKIT_ROOT / app_cfg['folder']
    if not app_folder.exists():
        log(f'app 資料夾不存在: {app_folder}', 'warn')
        return 0

    # 殘檔清理
    cleanup_stale(app_folder)

    # Lock
    try:
        lock_path = acquire_lock(app_folder, force=force)
    except LockHeld as e:
        log(f'{e}, 跳過 update check')
        return 0

    try:
        return _run_update_locked(app_id, app_cfg, app_folder, dry_run)
    finally:
        release_lock(lock_path)


def _run_update_locked(app_id, app_cfg, app_folder, dry_run) -> int:
    cfg = load_config()
    if not cfg:
        return 0

    # 版本檢查
    info = check_version(cfg, app_id)
    if not info:
        return 0

    remote_ver = info.get('version', '')
    expected_sha = info.get('sha256', '')
    expected_size = info.get('size', 0)
    changelog = info.get('changelog', '')

    try:
        validate_version(remote_ver)
    except ValueError as e:
        log(f'遠端版本格式錯: {e}', 'warn')
        return 0

    local_ver = read_local_version(app_folder)
    if not version_gt(remote_ver, local_ver):
        # 已是最新或更新 (rollback 後 admin 會 push 新號)
        return 0

    log(f'發現新版: {local_ver} → {remote_ver}{" (DRY-RUN)" if dry_run else ""}')
    if changelog:
        log(f'更新內容: {changelog[:200]}')

    if dry_run:
        return 0

    # Thundering herd jitter
    time.sleep(random.uniform(0, 5))

    # 下載 zip 到 temp (含 sha 防雙開)
    sha_short = (expected_sha[:8] if expected_sha else 'nosha')
    temp_path = app_folder / f'.update_temp_{sha_short}.zip'
    if temp_path.exists():
        try: temp_path.unlink()
        except OSError: pass

    if not download_zip(cfg, app_id, expected_sha, expected_size, temp_path):
        try: temp_path.unlink()
        except OSError: pass
        return 0

    # 驗證 zip
    zinfos, err = validate_zip(temp_path, app_cfg, app_folder)
    if err:
        log(f'zip 驗證失敗: {err}', 'err')
        try: temp_path.unlink()
        except OSError: pass
        return 0

    # 兩階段解壓
    ok, n_files, req_changed = two_phase_extract(temp_path, app_folder)
    try: temp_path.unlink()
    except OSError: pass
    if not ok:
        log('解壓未完整成功, 不寫 version (下次自動重試)', 'err')
        return 0

    log(f'已套用 {n_files} 個檔')

    # pip install (若 requirements 變動)
    if req_changed:
        if not auto_pip_install(app_folder):
            log('依賴安裝失敗, 不寫 version (下次重試)', 'err')
            return 0

    # 寫 version (atomic, 流程最後一步)
    try:
        atomic_write_version(app_folder, remote_ver)
        log(f'已更新 {local_ver} → {remote_ver}', 'ok')
    except Exception as e:
        log(f'寫 version 失敗: {e}', 'err')
        return 0

    # TG 通知 (best effort)
    notify_tg(app_id, app_cfg, local_ver, remote_ver, changelog)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True, help='app_id (例: product_hub)')
    parser.add_argument('--dry-run', action='store_true', help='只查不下載')
    parser.add_argument('--force', action='store_true', help='忽略 lock 強跑')
    args = parser.parse_args()

    try:
        sys.exit(run_update(args.app, args.dry_run, args.force))
    except KeyboardInterrupt:
        log('用戶中斷', 'warn')
        sys.exit(0)
    except Exception as e:
        log(f'未預期錯誤: {type(e).__name__}: {e}', 'err')
        sys.exit(0)   # 永遠 0 (不 block 啟動)


if __name__ == '__main__':
    main()
