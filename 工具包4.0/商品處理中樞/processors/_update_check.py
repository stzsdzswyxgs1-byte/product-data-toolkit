# -*- coding: utf-8 -*-
"""4.0.46+: 解析 updater stdout log, 判斷是否「有新版但升級失敗」.
4.0.49: 額外把 log 用 cmd console codepage (GBK on zh-cn) 重新編碼印出, 中文不亂碼.
        start.bat 不再 type log 自己, 由此 wrapper 統一 print.

start.bat 必須純 ASCII (cmd 用 GBK 解 UTF-8 中文會壞), 不能直接 findstr /C:"中文".
此 wrapper 用 Python 讀 UTF-8 log:
  - 用 cmd 認的 codepage 重 print 給 console (中文正確顯示)
  - 純 ASCII exit code 給 batch 用

Usage: python _update_check.py <updater_log_path>

Exit code:
  0 = OK — 沒新版, 或升級成功 (start.bat 正常 launch)
  2 = 升級失敗 — 有新版但下載/驗證失敗 (start.bat 應 abort)
  其他 = 內部錯誤 (caller 視同 0, 別擋住啟動)
"""
import sys
import os
import locale


def _print_console(text: str):
    """把 text 用 cmd console codepage 重新編碼印出.
    Windows zh-cn cmd 默認 chcp 936 (GBK), zh-tw 950 (Big5). UTF-8 直印會亂碼.
    用 stdout.buffer 寫 binary 繞過 Python 的 default encoding.
    """
    try:
        # 找 console 用的 codepage
        cp = locale.getpreferredencoding(False) or 'gbk'
    except Exception:
        cp = 'gbk'
    try:
        encoded = text.encode(cp, errors='replace')
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except Exception:
        # fallback: 直接 print (可能仍亂)
        try:
            print(text)
        except Exception:
            pass


def check(log_path: str) -> int:
    if not log_path or not os.path.isfile(log_path):
        return 0
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return 0

    # ★ 4.0.49: 重新編碼印給 cmd console (取代 start.bat 的 type 命令, 不再亂碼)
    _print_console(content)

    # ★ 4.0.50: 嚴格策略 — 任何 updater 失敗 (連 manifest 都拉不到 / 下載失敗 / SHA 不對) 都 abort
    # 理由: 用戶反映「SSL 抖時 updater 跳過, 還能啟動 = 跑舊版沒人發現」
    # 4.0.10 updater 任何失敗都印「跳過更新」/「跳過」+ 對應 reason.
    # ★ 4.0.51: 跳過 changelog 行 — admin 寫的描述含 marker 關鍵字會 false positive abort
    #   (4.0.50 自己撞: changelog 含「check 超時」字眼被誤判成升級失敗)
    fail_markers = [
        # 連 manifest 都拉不到 (4.0.10 updater 印的 marker)
        '網路問題',
        'check 超時',
        'check 失敗',
        'check 回 HTTP',
        'check 回非 JSON',
        # 拉 zip 失敗
        '下載網路問題',
        '下載超時',
        '下載失敗',
        'download 回 HTTP',
        # 驗證失敗
        'SHA256 不對',
        'size 不對',
        # admin 設置問題 (不該擋同事啟動但要醒目報出來, 同事找 admin)
        'config 不存在',
        'config 缺',
    ]
    # ★ 4.0.51: 過濾掉 changelog 行 (admin 寫的, 含關鍵字會 false positive)
    lines_for_check = []
    for ln in content.split('\n'):
        s = ln.strip()
        # updater 印 changelog 格式: "[updater] 更新內容: <文字>"
        if s.startswith('[updater] 更新內容:'):
            continue
        lines_for_check.append(ln)
    filtered_content = '\n'.join(lines_for_check)
    if any(m in filtered_content for m in fail_markers):
        return 2  # updater 失敗 → start.bat abort
    return 0  # 沒新版 (log 空 or 已是最新, 任何 marker 都沒) 或升級成功 → 放行


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) >= 2 else ''
    sys.exit(check(path))
