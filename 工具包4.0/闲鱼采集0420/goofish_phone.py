"""
Phone-assisted direct-HTTP mtop client — drop-in replacement for MtopClient.

架構 (v2, 2026-04-19 重寫):
  1. 啟動時從手機 /request 拿 APP 真實 header 模板 (utdid/devid/ttid/bx-version 等)
  2. 每個 API 呼叫:
     a. POST 到手機 /sign -> 拿 x-sign/x-umt/x-sgext/x-mini-wua (APP 真 InnerSignImpl 簽)
     b. Python 直接 GET acs.m.goofish.com/gw/<api>/<v>/?data=<url-encoded>
        所有 header 值 UrlEncodeToUpper (tianya 精確行為, 這就是以前 FAIL_SYS_ILEGEL_SIGN 的根因)
  3. 回 mtop response (含 itemDO, sellerDO 等)

優點:
  - 完全不開 APP UI, 手機不跳商品頁 — 使用者體驗完全無打擾
  - 純 HTTP + 並行 6 workers, ~0.4 秒/件
  - 14110 個商品 < 2 小時
  - 依然走 APP 簽名 (x-sign 來自手機 InnerSignImpl), 風控低

完整接口相容 goofish_api.MtopClient.call(api, version, data, referer=None, session=None)
"""
import json
import threading
import time
import urllib.parse
from pathlib import Path

import requests
# curl_cffi: impersonate Chrome TLS fingerprint — 讓阿里看起來就是個 Chrome 瀏覽器而不是 Python
try:
    from curl_cffi import requests as curl_req
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False
    curl_req = None

# ============================================================
# 配置 — 優先讀 phone_config.py (由「一键配置手机」腳本產生)
# ============================================================
import os as _os
import sys as _sys
_HERE = Path(__file__).resolve().parent

# 確保本文件所在目錄在 sys.path 最前面 (這樣 phone_config.py 能找到)
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

# phone_config.py 自動發現: 同目錄 -> 父目錄 -> 爺目錄 -> 曾祖目錄
PHONE_IP = None
DEVICE_SERIAL = ""
PHONE_UTDID = ""  # 設備指紋, 多手機環境認本機
_ns_utdid = ""  # alias for backward compat
for _up in range(5):
    _probe = _HERE
    for _ in range(_up):
        _probe = _probe.parent
    _pc = _probe / "phone_config.py"
    if _pc.exists():
        try:
            _ns = {}
            with open(_pc, "r", encoding="utf-8") as _f:
                exec(_f.read(), _ns)
            PHONE_IP = _ns.get("PHONE_IP", None)
            DEVICE_SERIAL = _ns.get("DEVICE_SERIAL", "")
            PHONE_UTDID = _ns.get("PHONE_UTDID", "")
            _ns_utdid = PHONE_UTDID
            if PHONE_IP:
                msg = f"PHONE_IP={PHONE_IP}"
                if PHONE_UTDID:
                    msg += f", utdid={PHONE_UTDID[:16]}..."
                print(f"[GF-PHONE] 读取配置 {_pc}: {msg}")
                break
        except Exception as _e:
            print(f"[GF-PHONE] 读 {_pc} 失败: {_e}")

if not PHONE_IP:
    PHONE_IP = "192.168.0.105"
    print(f"[GF-PHONE] 找不到 phone_config.py, 用默认 IP: {PHONE_IP}")
    print(f"[GF-PHONE] 解决: 跑「一键配置手机.bat」, 或手动在 {_HERE} 建 phone_config.py:")
    print(f'[GF-PHONE]   PHONE_IP = "你的手机IP"')
    print(f'[GF-PHONE]   DEVICE_SERIAL = "手机serial"')

# ADB 路徑: 爬 5 層父目錄找 platform-tools/, 找不到才 fallback
_ADB_CANDIDATES = []
for _up in range(5):
    _probe = _HERE
    for _ in range(_up):
        _probe = _probe.parent
    _ADB_CANDIDATES.append(str((_probe / "platform-tools" / "adb.exe").resolve()))
_ADB_CANDIDATES += [
    r"C:/Users/USERNAME/Downloads/platform-tools/adb.exe",
    "adb.exe",  # 如果在 PATH 裡
]
ADB = next((p for p in _ADB_CANDIDATES if _os.path.exists(p) or p == "adb.exe"), "adb.exe")
SIGN_PORT = 10102


# ============================================================
# IP 自動探測 — 啟動時驗證配置的 IP, 失敗則用 adb 自動找
# ============================================================
def _test_phone_ip(ip, port=10102, timeout=2):
    """快速測 AndServer 是否在這個 IP 上響應"""
    import urllib.request
    try:
        r = urllib.request.urlopen(f"http://{ip}:{port}/test", timeout=timeout)
        body = r.read()
        return b'"msg"' in body and b'"ok"' in body
    except Exception:
        return False


def _get_phone_utdid(ip, port=10102, timeout=3):
    """從手機 /request 拿 x-utdid (APP 層設備唯一 ID, 用於多手機區分)"""
    import urllib.request
    import urllib.parse
    import json
    try:
        r = urllib.request.urlopen(f"http://{ip}:{port}/request?count=1", timeout=timeout)
        data = json.loads(r.read())
        raws = data.get("req", [])
        if not raws:
            return None
        for kv in raws[0].split(", "):
            if kv.startswith("x-utdid="):
                return urllib.parse.unquote(kv[len("x-utdid="):])
    except Exception:
        pass
    return None


def _adb_discover_phone_ip():
    """adb 找手機 wlan0 IP. 沒 USB / 沒連 WiFi 就回 None.
    過濾模擬器 (emulator-XXXX), 優先真實手機 (序號是 hex 字串).
    """
    import subprocess
    try:
        r = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                           timeout=5, encoding="utf-8", errors="replace")
        all_devs = [l.split("\t")[0] for l in r.stdout.split("\n")
                    if "\t" in l and "device" in l and not l.startswith("List")
                    and not l.startswith("sigma")]
        if not all_devs:
            return None, None

        # 排除模擬器 (emulator-5554 等), 優先真實 USB 手機
        real_phones = [d for d in all_devs if not d.startswith("emulator-")]
        if real_phones:
            devices = real_phones
            if len(all_devs) != len(real_phones):
                emus = [d for d in all_devs if d.startswith("emulator-")]
                print(f"[GF-PHONE] 偵測到 {len(emus)} 台模擬器 ({emus}), 略過, 用真機 {real_phones[0]}")
        else:
            # 都是模擬器, 也只能用了
            devices = all_devs
            print(f"[GF-PHONE] [!] 只看到模擬器, 沒插實體手機")

        dev = devices[0]
        r = subprocess.run([ADB, "-s", dev, "shell", "ip", "-4", "addr", "show", "wlan0"],
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
        for line in r.stdout.split("\n"):
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split("inet ")[1].split("/")[0].strip()
                if ip and ip.count(".") == 3 and not ip.startswith("127."):
                    return ip, dev
    except Exception as e:
        print(f"[GF-PHONE] adb 探測異常: {e}")
    return None, None


def _save_phone_config(ip, dev_serial, utdid=None):
    """寫回 phone_config.py 下次直接用. 順便存 utdid 用於多手機區分"""
    try:
        # 沒給 utdid 就現抓 (用於辦公室多手機環境的指紋識別)
        if not utdid:
            utdid = _get_phone_utdid(ip)
        cfg = _HERE / "phone_config.py"
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('"""自動更新 by goofish_phone.py (IP 變化時自動探測)"""\n')
            f.write(f'DEVICE_SERIAL = "{dev_serial}"\n')
            f.write(f'PHONE_IP = "{ip}"\n')
            if utdid:
                f.write(f'PHONE_UTDID = "{utdid}"  # 設備指紋, 多手機環境用來認本機對應的手機\n')
        msg = f"PHONE_IP={ip}"
        if utdid:
            msg += f", utdid={utdid[:16]}..."
        print(f"[GF-PHONE] [OK] 已寫回 phone_config.py: {msg}")
    except Exception as e:
        print(f"[GF-PHONE] 寫 phone_config.py 失敗: {e}")


def _wait_for_adb_device(max_wait=60):
    """輪詢 adb devices 直到找到真實手機 (排除模擬器), 或超時"""
    import subprocess
    import time as _t
    for i in range(max_wait):
        try:
            r = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                               timeout=3, encoding="utf-8", errors="replace")
            real = []
            for line in r.stdout.split("\n"):
                if ("\t" in line and "device" in line and not line.startswith("List")
                        and not line.startswith("sigma")):
                    s = line.split("\t")[0]
                    if not s.startswith("emulator-"):
                        real.append(s)
            if real:
                return real[0]
        except Exception:
            pass
        _t.sleep(1)
    return None


def _get_pc_local_ips():
    """取本機所有 IPv4 (排除 loopback / link-local), 用於決定掃哪些子網"""
    import socket
    ips = []
    # 方法 1: hostname 解析
    try:
        hostname = socket.gethostname()
        for addr_info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = addr_info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except Exception:
        pass
    # 方法 2: 連外網方式取真實出口 IP (能繞過 DNS 異常)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip and local_ip not in ips:
            ips.insert(0, local_ip)
    except Exception:
        pass
    return list(dict.fromkeys(ips))  # 去重保序


def _lan_scan_for_andserver(port=10102, expected_utdid=None,
                              connect_timeout=0.4, http_timeout=2):
    """
    掃 PC 所在每個 /24 子網, 找誰在聽 10102 端口並且是真 AndServer.
    並行 64 個 socket, 256 IP 大概 2-3 秒掃完. 不需要 USB.

    多手機環境: 如果傳了 expected_utdid, 會用它認「本機對應的手機」,
    避免在 3 台手機同 WiFi 時誤連別人的.
    """
    import socket
    import concurrent.futures

    pc_ips = _get_pc_local_ips()
    if not pc_ips:
        return None

    # 收集所有候選 IP (本機所有網卡的 /24)
    candidates = set()
    for pc_ip in pc_ips:
        parts = pc_ip.split(".")
        if len(parts) != 4:
            continue
        prefix = ".".join(parts[:3])
        for i in range(1, 255):
            target = f"{prefix}.{i}"
            if target != pc_ip:
                candidates.add(target)

    if not candidates:
        return None

    # 階段 1: 並行 TCP connect 探測誰開了 10102
    def _tcp_probe(ip):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(connect_timeout)
            r = s.connect_ex((ip, port))
            s.close()
            return ip if r == 0 else None
        except Exception:
            return None

    open_ips = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip in ex.map(_tcp_probe, candidates):
            if ip:
                open_ips.append(ip)

    if not open_ips:
        return None

    # 階段 2: 驗證真 AndServer + 用 utdid 區分多手機
    valid = []  # [(ip, utdid)]
    for ip in open_ips:
        if not _test_phone_ip(ip, port, timeout=http_timeout):
            continue
        utdid = _get_phone_utdid(ip, port)
        valid.append((ip, utdid))

    if not valid:
        return None

    # 多手機: 優先選 utdid 匹配的
    if expected_utdid:
        for ip, utdid in valid:
            if utdid == expected_utdid:
                print(f"[GF-PHONE] [LAN] utdid 匹配! 鎖定本機手機 {ip}")
                return ip
        # 沒匹配的: 警告 + 不亂選
        print(f"[GF-PHONE] [LAN] [!] 掃到 {len(valid)} 台手機, 但都不是這台 PC 的:")
        for ip, utdid in valid:
            print(f"    - {ip} utdid={(utdid or '?')[:24]}...")
        print(f"    本機應該的 utdid={expected_utdid[:24]}...")
        print(f"    可能你的手機沒在跑 / 換手機了 / 在不同 WiFi")
        return None

    # 沒 expected_utdid (第一次跑或 legacy config), 警告但用第一個
    if len(valid) > 1:
        print(f"[GF-PHONE] [LAN] [!] 掃到 {len(valid)} 台手機都開了 AndServer:")
        for ip, utdid in valid:
            print(f"    - {ip} utdid={(utdid or '?')[:24]}...")
        print(f"    無法判斷哪台是本機的, 暫用第一個 ({valid[0][0]})")
        print(f"    建議: 跑「一键配置手机.bat」設置 PHONE_UTDID 認本機手機")
    return valid[0][0]


def _adb_setup_forward(adb_dev, port=10102):
    """建 USB 端口轉發 (PC 127.0.0.1:port → 手機 port). 跨網段救星."""
    import subprocess
    try:
        # 先清舊 forward 避免衝突
        subprocess.run([ADB, "-s", adb_dev, "forward", "--remove-all"],
                       capture_output=True, timeout=5)
        r = subprocess.run([ADB, "-s", adb_dev, "forward",
                            f"tcp:{port}", f"tcp:{port}"],
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0
    except Exception as e:
        print(f"[GF-PHONE] adb forward 異常: {e}")
        return False


def _is_same_subnet(ip1, ip2):
    """判斷兩個 IP 是不是同 /24 子網"""
    try:
        a = ip1.split(".")[:3]
        b = ip2.split(".")[:3]
        return a == b
    except Exception:
        return False


def _adb_ensure_idlefish_running(adb_dev):
    """如閑魚 APP 沒在跑, 用 adb monkey 啟動它. 等 8 秒讓進程起來"""
    import subprocess
    import time as _t
    try:
        r = subprocess.run(
            [ADB, "-s", adb_dev, "shell", "pidof", "com.taobao.idlefish"],
            capture_output=True, text=True, timeout=3,
            encoding="utf-8", errors="replace")
        pid = (r.stdout or "").strip()
        if pid.isdigit():
            return True  # 已在跑
        print(f"[GF-PHONE] 閑魚 APP 沒在跑, adb 自動啟動...")
        subprocess.run(
            [ADB, "-s", adb_dev, "shell", "monkey", "-p", "com.taobao.idlefish",
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, timeout=8,
            encoding="utf-8", errors="replace")
        _t.sleep(8)  # 等 APP 進程起來
        return True
    except Exception as e:
        print(f"[GF-PHONE] 啟動閑魚 APP 異常: {e}")
        return False


def _wait_andserver_ready(ip, max_wait=45, port=10102):
    """
    輪詢測試 AndServer 是否在 ip:port 響應. 邊等邊提示用戶.
    AndServer 需要閑魚登入 + 滑首頁觸發 mtop 才會起, 所以可能要 30+ 秒.
    """
    import time as _t
    print(f"[GF-PHONE] 等 AndServer 在 {ip}:{port} 啟動 (最多 {max_wait} 秒)...")
    print(f"[GF-PHONE] 重要! 手機上請: 1) 打開閑魚 2) 登入帳號 3) 滑首頁觸發 mtop")
    for i in range(max_wait // 3):
        if _test_phone_ip(ip, port, timeout=2):
            print(f"[GF-PHONE] [OK] AndServer 在 {(i+1)*3} 秒後響應!")
            return True
        if i % 3 == 0 and i > 0:
            print(f"[GF-PHONE] 等 {(i+1)*3}/{max_wait} 秒... (請手機上閑魚滑首頁)")
        _t.sleep(3)
    print(f"[GF-PHONE] {max_wait} 秒後 AndServer 仍未響應")
    return False


def _show_idlefish_login_hint():
    """彈窗提示用戶在手機閑魚登入 + 滑首頁"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showinfo(
            "請在手機上操作",
            "找到手機了, 但閑魚 APP 的 AndServer 還沒響應\n\n"
            "請現在在手機上:\n"
            "  1. 打開閑魚 APP\n"
            "  2. 登入帳號 (必須登入)\n"
            "  3. 在首頁滑動商品流 30 秒\n\n"
            "然後點【確定】, 我會再等 30 秒看 AndServer 是否啟動"
        )
        root.destroy()
        return True
    except Exception:
        return False


def _detect_hook_state(adb_dev):
    """
    檢測 LSPosed hook 是否真的載入到閑魚進程. 返回:
      'loaded'      - 看到 idlefish7920 載入到 com.taobao.idlefish, hook 正常
      'not_loaded'  - logcat 無 hook 痕跡, zygote 注入鏈斷, 必須重啟手機
      'unknown'     - logcat 讀失敗
    """
    import subprocess
    try:
        r = subprocess.run(
            [ADB, "-s", adb_dev, "shell", "logcat", "-d", "-t", "3000"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        log = r.stdout or ""
        # 最強信號: idlefish7920 模組真的載入到閑魚進程
        if "Loading legacy module com.tianya.idlefish7920" in log:
            return "loaded"
        if "AppSignSample" in log:
            return "loaded"
        # 沒看到 = hook 沒注入
        return "not_loaded"
    except Exception:
        return "unknown"


def _prompt_reboot_phone():
    """彈窗: hook 失效, 自動重啟手機嗎?"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        result = messagebox.askyesno(
            "需要重啟手機",
            "偵測到 LSPosed hook 沒注入閑魚進程\n"
            "(MIUI 殺後台或 Zygisk 注入鏈斷裂導致)\n\n"
            "唯一解法是重啟手機, 讓 zygote 重新初始化.\n\n"
            "點【是】: 我用 adb 自動重啟手機 (約 2 分鐘)\n"
            "          重啟後請手機上打開閑魚 + 登入 + 滑首頁\n\n"
            "點【否】: 跳過 (你也可以手動重啟手機後重開工具)"
        )
        root.destroy()
        return result
    except Exception:
        return False


def _adb_reboot_and_wait(adb_dev, max_wait=180):
    """adb 重啟手機 + 等開機完成"""
    import subprocess
    import time as _t
    try:
        print(f"[GF-PHONE] adb reboot {adb_dev} ...")
        subprocess.run([ADB, "-s", adb_dev, "reboot"], timeout=10,
                       capture_output=True, encoding="utf-8", errors="replace")
        _t.sleep(15)  # 等手機真的關機
        for i in range(max_wait):
            try:
                r = subprocess.run(
                    [ADB, "-s", adb_dev, "shell", "getprop", "sys.boot_completed"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace")
                if (r.stdout or "").strip() == "1":
                    print(f"[GF-PHONE] [OK] 手機開機完成 ({i+15} 秒)")
                    _t.sleep(8)  # 多等讓 LSPosed daemon + Magisk modules 起來
                    return True
            except Exception:
                pass
            if i > 0 and i % 15 == 0:
                print(f"[GF-PHONE] 等手機開機 {i+15}s...")
            _t.sleep(1)
        print(f"[GF-PHONE] [!] 手機開機等超時")
        return False
    except Exception as e:
        print(f"[GF-PHONE] adb reboot 異常: {e}")
        return False


def _prompt_plug_usb_gui():
    """彈窗提示用戶插 USB, 返回 True 表示用戶同意, False 跳過"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        result = messagebox.askyesno(
            "手機 IP 自動探測",
            f"[!] 配置的手機 IP ({PHONE_IP}) 連不上 AndServer\n\n"
            "可能原因: 手機 WiFi IP 已變化\n\n"
            "請現在用 USB 線把手機接上電腦,\n"
            "點【是】我會自動探測新 IP 並寫入配置\n\n"
            "(找到後以後就不用管了)\n\n"
            "點【否】跳過 (採集功能會失敗)"
        )
        root.destroy()
        return result
    except Exception as e:
        print(f"[GF-PHONE] 無法顯示彈窗 ({e}), 改用控制台提示")
        try:
            ans = input("[GF-PHONE] AndServer 不通, 插 USB 自動探測新 IP? [Y/n]: ").strip().lower()
            return ans != "n"
        except Exception:
            return False


def _resolve_phone_ip(initial_ip, initial_serial):
    """
    多策略 IP 解析. 90% 情況零用戶交互完成.

    新版優先順序 (USB 優先, 永遠不怕 IP 變):
      策略 0 (新): USB 插著 -> 永遠走 127.0.0.1 (跨網段也通, IP 變化也通)
      策略 1: 配置的 IP 通 -> 直接用 (備用: USB 沒插時走 WiFi)
      策略 2: USB 插著但 127 不通 -> 重啟閑魚 / 探 WiFi IP
      策略 3: 局域網掃描 10102 端口 (USB 沒插, 同 WiFi)
      策略 4: 彈窗請用戶插 USB (最後兜底)
    全失敗 -> 保留原 IP, 後續報錯讓用戶看
    """
    # ── 策略 0 (新, 最高優先): USB 插著 -> 一律走 127.0.0.1 ──
    # 理由: USB 隧道穩定, 不怕 WiFi IP 變化, 不怕跨網段, 不怕路由器隔離
    adb_ip_pre, adb_dev_pre = _adb_discover_phone_ip()
    if adb_dev_pre:
        _adb_setup_forward(adb_dev_pre)
        if _test_phone_ip("127.0.0.1", SIGN_PORT, timeout=2):
            if initial_ip != "127.0.0.1":
                print(f"[GF-PHONE] [策略 0] USB 已插, 自動切換到 127.0.0.1 (原配置 {initial_ip})")
                _save_phone_config("127.0.0.1", adb_dev_pre)
            return "127.0.0.1", adb_dev_pre
        # USB 插著但 127.0.0.1 不通 -> 多半閑魚沒跑, 走後續策略修

    # ── 策略 1: 配置 IP 通了 (USB 沒插時, 走 WiFi IP) ──
    if _test_phone_ip(initial_ip, SIGN_PORT, timeout=2):
        return initial_ip, initial_serial

    print(f"[GF-PHONE] [!] 配置的 IP {initial_ip} 沒響應, 啟動自動恢復...")

    # ── 策略 2: USB 已插 -> 優先 127.0.0.1 (跨網段救星) + 後備 WiFi IP ──
    adb_ip, adb_dev = _adb_discover_phone_ip()
    if adb_dev:
        print(f"[GF-PHONE] [策略 2] USB 已插, adb 檢測到設備 {adb_dev}")

        # 2a: 建 USB forward + 確保閑魚在跑
        _adb_setup_forward(adb_dev)
        _adb_ensure_idlefish_running(adb_dev)

        # 2b: 優先測 127.0.0.1 (USB 隧道, 跨網段也通)
        # 重要場景: PC 和手機不同 WiFi, WiFi IP 永遠不通, 只有 USB 隧道能救
        if _test_phone_ip("127.0.0.1", SIGN_PORT, timeout=3):
            # 判斷是否跨網段 (給用戶看清楚為什麼選 USB)
            pc_ips = _get_pc_local_ips()
            cross_subnet = adb_ip and pc_ips and not any(
                _is_same_subnet(adb_ip, p) for p in pc_ips)
            if cross_subnet:
                print(f"[GF-PHONE] [OK] 走 USB 隧道 (PC 在 {pc_ips[0].rsplit('.',1)[0]}.x, "
                      f"手機在 {adb_ip.rsplit('.',1)[0]}.x, 跨網段, 必須走 USB)")
            else:
                print(f"[GF-PHONE] [OK] 走 USB 隧道 (穩定不依賴 WiFi)")
            _save_phone_config("127.0.0.1", adb_dev)
            return "127.0.0.1", adb_dev

        # 2c: 127.0.0.1 不通 -> 試 adb 探到的 WiFi IP (萬一 USB forward 失敗)
        if adb_ip and _test_phone_ip(adb_ip, SIGN_PORT, timeout=3):
            print(f"[GF-PHONE] [OK] WiFi IP {adb_ip} 通了, 寫回配置")
            _save_phone_config(adb_ip, adb_dev)
            return adb_ip, adb_dev
        # adb IP 不通但 device 在 — 區分兩種情況: hook 失效 vs 沒登入滑首頁
        if adb_ip:
            print(f"[GF-PHONE] adb 探到 IP {adb_ip} 但 AndServer 沒響應")
            # 先看 hook 狀態
            hook_state = _detect_hook_state(adb_dev)
            print(f"[GF-PHONE] hook 檢測結果: {hook_state}")

            if hook_state == "not_loaded":
                # hook 沒注入閑魚, 必須重啟手機重置 zygote
                print(f"[GF-PHONE] [!] hook 沒注入, 提示用戶重啟手機")
                if _prompt_reboot_phone():
                    if _adb_reboot_and_wait(adb_dev):
                        # 重啟成功 — 啟動閑魚, 提示用戶登入滑首頁
                        _adb_ensure_idlefish_running(adb_dev)
                        _show_idlefish_login_hint()
                        # 重啟後 IP 可能變
                        new_ip2, _ = _adb_discover_phone_ip()
                        target_ip = new_ip2 or adb_ip
                        if _wait_andserver_ready(target_ip, max_wait=60):
                            print(f"[GF-PHONE] [OK] 重啟後 {target_ip} 通了")
                            _save_phone_config(target_ip, adb_dev)
                            return target_ip, adb_dev
                        print(f"[GF-PHONE] 重啟後仍不通, 可能 LSPosed scope 也失效, 跑 setup_phone.py 重配")
            else:
                # hook 已載入, 多半是閑魚沒登入或沒觸發 mtop
                _show_idlefish_login_hint()
                if _wait_andserver_ready(adb_ip, max_wait=45):
                    print(f"[GF-PHONE] [OK] 用戶操作後 {adb_ip} 通了")
                    _save_phone_config(adb_ip, adb_dev)
                    return adb_ip, adb_dev
                print(f"[GF-PHONE] 用戶操作後仍不通")

        # 最後再測一次原 IP (用戶可能手動修了)
        if _test_phone_ip(initial_ip, SIGN_PORT, timeout=2):
            print(f"[GF-PHONE] [OK] 原 IP {initial_ip} 現在通了")
            return initial_ip, initial_serial

    # ── 策略 3: 局域網掃描 (USB 沒插, 但同 WiFi). 多手機環境用 utdid 認本機 ──
    print(f"[GF-PHONE] [策略 3] 局域網掃描 (找誰開了 10102 端口)...")
    expected_utdid = globals().get("PHONE_UTDID") or _ns_utdid
    lan_ip = _lan_scan_for_andserver(expected_utdid=expected_utdid)
    if lan_ip:
        print(f"[GF-PHONE] [OK] 局域網掃到手機 IP: {lan_ip}, 寫回配置")
        _save_phone_config(lan_ip, initial_serial)
        return lan_ip, initial_serial
    print(f"[GF-PHONE] [策略 3] 局域網沒掃到 (PC 和手機可能不同 WiFi, 或閑魚沒跑)")

    # ── 策略 4: 兜底彈窗請用戶插 USB ──
    print(f"[GF-PHONE] [策略 4] 自動探測都失敗, 彈窗請用戶插 USB")
    if not _prompt_plug_usb_gui():
        print(f"[GF-PHONE] 用戶跳過, 保留原 IP {initial_ip}")
        return initial_ip, initial_serial

    print(f"[GF-PHONE] 等用戶插 USB (最多 60 秒)...")
    dev = _wait_for_adb_device(max_wait=60)
    if not dev:
        print(f"[GF-PHONE] 60 秒內沒檢測到手機")
        return initial_ip, initial_serial

    print(f"[GF-PHONE] [OK] 檢測到手機 {dev}")
    _adb_setup_forward(dev)
    _adb_ensure_idlefish_running(dev)

    # 優先 127.0.0.1 (USB 隧道穩定, 跨網段也通)
    if _test_phone_ip("127.0.0.1", SIGN_PORT, timeout=3):
        print(f"[GF-PHONE] [OK] 走 USB 隧道 127.0.0.1, 寫回配置")
        _save_phone_config("127.0.0.1", dev)
        return "127.0.0.1", dev

    # 後備: WiFi IP
    new_ip, _ = _adb_discover_phone_ip()
    if not new_ip:
        print(f"[GF-PHONE] [X] 探測 wlan0 失敗 (手機沒連 WiFi?)")
        return initial_ip, initial_serial

    if _test_phone_ip(new_ip, SIGN_PORT, timeout=3):
        print(f"[GF-PHONE] [OK] WiFi IP {new_ip} 通了, 寫回配置")
        _save_phone_config(new_ip, dev)
        return new_ip, dev
    else:
        print(f"[GF-PHONE] [!] 找到新 IP {new_ip} 但 AndServer 仍不通 (可能閑魚 hook 失效)")
        _save_phone_config(new_ip, dev)
        return new_ip, dev


# 啟動時做一次 IP 驗證 + 自動修正
PHONE_IP, DEVICE_SERIAL = _resolve_phone_ip(PHONE_IP, DEVICE_SERIAL)

BASE_URL = f"http://{PHONE_IP}:{SIGN_PORT}"
SIGN_URL = f"{BASE_URL}/sign"
REQUEST_URL = f"{BASE_URL}/request?count=3"
ACS_BASE = "https://acs.m.goofish.com/gw"

# H5 API -> APP mtop API 映射
# (H5 api name, 支援?) -> (APP mtop api, version)
API_MAP = {
    "mtop.taobao.idle.pc.detail":           ("mtop.taobao.idle.awesome.detail.unit", "1.0"),
    "mtop.idle.web.xyh.item.list":          ("mtop.taobao.idle.xyh.item.list",       "1.0"),
    "mtop.taobao.idlemtopsearch.pc.search": ("mtop.taobao.idlemtopsearch.search",    "1.0"),
    "mtop.taobao.idlehome.home.webpc.feed": ("mtop.taobao.idlehome.home.nextfresh",  "1.0"),
    "mtop.idle.user.page.head":             ("mtop.idle.user.page.head",             "1.0"),
}


def _up_encode(v):
    """模仿 tianya UrlEncodeToUpper: 全字元 URL-encode, uppercase hex.
    空格 -> +. Python quote 默認 uppercase, 直接用."""
    if v is None:
        return ""
    return urllib.parse.quote(str(v), safe="").replace("%20", "+")


# ============================================================
# Errors
# ============================================================
from goofish_api import MtopError  # 相容原工具


# ============================================================
# Client
# ============================================================
class PhoneClient:
    """Drop-in replacement of goofish_api.MtopClient."""

    def __init__(self, session=None):
        # stub for collect_details_concurrent
        class _StubCookieJar:
            def __iter__(self): return iter([])
        class _StubHTTPSession:
            def __init__(self):
                class _C:
                    jar = _StubCookieJar()
                self.cookies = _C()
        class _StubGS:
            _http = _StubHTTPSession()
            def get_session(self): return self._http
            def get_token(self): return "phone-direct-mode"
        self.gs = session if session is not None else _StubGS()

        self._tmpl = None
        self._tmpl_time = 0
        self._tmpl_lock = threading.Lock()
        # 主 session 用於 sign; 每個 thread 用 thread-local session 打 acs
        self._main_sess = self._mk_session(pool_size=32)
        self._tl = threading.local()

    @staticmethod
    def _mk_session(pool_size=16, for_acs=False):
        """session 選擇:
          for_acs=True: 打 acs.m.goofish.com — 用 curl_cffi (Chrome TLS 指紋)
          for_acs=False: 打手機 /sign — 用 requests (內網 HTTP, 不需 TLS 偽裝)
        """
        if for_acs and HAVE_CURL_CFFI:
            s = curl_req.Session(impersonate="chrome136")
            return s
        s = requests.Session()
        a = requests.adapters.HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            max_retries=0,
        )
        s.mount("http://", a)
        s.mount("https://", a)
        return s

    # --------- thread-local session for acs calls (curl_cffi Chrome TLS) ---------
    def _acs_sess(self):
        s = getattr(self._tl, "s", None)
        if s is None:
            s = self._mk_session(pool_size=4, for_acs=True)
            self._tl.s = s
        return s

    # --------- header template from phone /request ---------
    # TTL 降到 60s: tianya 在 Form1 每次 collect 前都 UpdateHeaders, 我們折衷 60s 快取
    def _get_tmpl(self, force=False):
        with self._tmpl_lock:
            if self._tmpl and not force and (time.time() - self._tmpl_time) < 60:
                return self._tmpl
            try:
                r = self._main_sess.get(REQUEST_URL, timeout=6)
                data = r.json()
                raws = data.get("req", [])
                if not raws:
                    raise MtopError("no_template", "/request 無 APP 真實 header; 手機閒魚需先打開一次 APP 讓它發個請求")
                # 取最新的 (第一個是最新)
                raw = raws[0]
                tmpl = {}
                for kv in raw.split(", "):
                    i = kv.find("=")
                    if i > 0:
                        tmpl[kv[:i]] = urllib.parse.unquote(kv[i+1:])
                for key in ("x-utdid", "x-devid", "x-appkey", "x-ttid",
                            "x-extdata", "x-app-ver", "x-bx-version",
                            "x-features", "user-agent"):
                    if key not in tmpl:
                        raise MtopError("bad_template", f"模板缺 {key}")
                self._tmpl = tmpl
                self._tmpl_time = time.time()
                return tmpl
            except MtopError:
                raise
            except Exception as e:
                raise MtopError("tmpl_err", f"取模板失敗: {e}")

    # --------- POST to phone /sign ---------
    def _sign(self, api, version, data_str, tmpl, t):
        fields = [
            ("deviceId", tmpl["x-devid"]),
            ("appKey",   tmpl["x-appkey"]),
            ("extdata",  tmpl["x-extdata"]),
            ("utdid",    tmpl["x-utdid"]),
            ("t",        str(t)),
            ("xFeatures", tmpl["x-features"]),
            ("ttid",     tmpl["x-ttid"]),
            ("api",      api),
            ("v",        version),
            ("data",     data_str),
            ("lng",      "0"),
            ("lat",      "0"),
            ("pageName", ""),
            ("pageId",   ""),
        ]
        if tmpl.get("x-sid"):
            fields.append(("sid", tmpl["x-sid"]))
        if tmpl.get("x-uid"):
            fields.append(("uid", tmpl["x-uid"]))
        body = "&".join(k + "=" + _up_encode(v) for k, v in fields)
        try:
            r = self._main_sess.post(
                SIGN_URL, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=8
            )
            return r.json()
        except Exception as e:
            raise MtopError("sign_err", f"/sign 失敗: {e}")

    # --------- GET acs.m.goofish.com ---------
    def _fetch_acs(self, app_api, version, data_str, tmpl, signed, t):
        url = f"{ACS_BASE}/{app_api}/{version}/?data={_up_encode(data_str)}"
        raw_headers = {
            "x-features":   tmpl["x-features"],
            "x-extdata":    tmpl["x-extdata"],
            "x-sgext":      signed["x-sgext"],
            "umid":         signed["x-umt"],
            "x-location":   "0,0",
            "user-agent":   tmpl["user-agent"],
            "x-ttid":       tmpl["x-ttid"],
            "x-appkey":     tmpl["x-appkey"],
            "x-mini-wua":   signed["x-mini-wua"],
            "x-c-traceid":  tmpl["x-utdid"] + str(t) + "000000000000",
            "x-app-conf-v": "0",
            "x-app-ver":    tmpl["x-app-ver"],
            "x-t":          str(t),
            "x-pv":         "6.3",
            "x-bx-version": tmpl["x-bx-version"],
            "f-refer":      "mtop",
            "x-utdid":      tmpl["x-utdid"],
            "x-umt":        signed["x-umt"],
            "x-devid":      tmpl["x-devid"],
            "x-sign":       signed["x-sign"],
        }
        if tmpl.get("x-sid"):
            raw_headers["x-sid"] = tmpl["x-sid"]
        if tmpl.get("x-uid"):
            raw_headers["x-uid"] = tmpl["x-uid"]
        # 照 tianya: 每 value URL-encode upper (KEY SUCCESS FACTOR)
        enc_headers = {k: _up_encode(v) for k, v in raw_headers.items()}
        try:
            r = self._acs_sess().get(url, headers=enc_headers, timeout=15)
            return r.json()
        except Exception as e:
            raise MtopError("acs_err", f"acs 失敗: {e}")

    # --------- public entry ---------
    def call(self, api: str, version: str, data: dict,
             referer: str = None, session=None, _retry=0) -> dict:
        """MtopClient.call 相容介面; 返回 mtop response 的 data 部分.
        自動處理: session 過期重試 1 次, 驗證碼重試 1 次"""
        if api not in API_MAP:
            raise MtopError("unsupported", f"Phone mode 未支援 {api}")
        app_api, app_ver = API_MAP[api]

        data_json = self._build_data(api, data)
        tmpl = self._get_tmpl()
        t = int(time.time())
        signed = self._sign(app_api, app_ver, data_json, tmpl, t)
        if "x-sign" not in signed:
            raise MtopError("sign_bad", f"sign 回應無 x-sign: {signed}")

        resp = self._fetch_acs(app_api, app_ver, data_json, tmpl, signed, t)
        ret = resp.get("ret", [])
        ret_str = ret[0] if ret else ""

        # 自動重試機制 (tianya Form1.cs 行為)
        if _retry < 2 and ret_str:
            if "FAIL_SYS_SESSION_EXPIRED" in ret_str:
                self._tmpl = None  # 強制重抓
                return self.call(api, version, data, referer, session, _retry+1)
            if "FAIL_SYS_USER_VALIDATE" in ret_str:
                time.sleep(0.3)  # 驗證碼緩一下再試
                return self.call(api, version, data, referer, session, _retry+1)

        if "SUCCESS" in ret_str:
            return resp.get("data", {}) or {}

        # tianya 1:1 的錯誤分類 (從 Form1.cs 逆向)
        # FAIL_SYS_USER_VALIDATE = 驗證碼 (不是永久失敗, 重試可能過)
        # FAIL_SYS_SESSION_EXPIRED = session 過期, 需要重新抓 template
        # FAIL_SYS_TRAFFIC_LIMIT / IP 限制 = 換 IP
        if "FAIL_SYS_SESSION_EXPIRED" in ret_str:
            # 強制重抓 template
            self._tmpl = None
            raise MtopError("session_expired", f"{ret_str}")
        if "FAIL_SYS_USER_VALIDATE" in ret_str:
            raise MtopError("user_validate", f"{ret_str}")  # 驗證碼, 可能重試就過
        if "FAIL_BIZ_ITEM_DEL" in ret_str or "ITEM_NOT_FOUND" in ret_str:
            raise MtopError("item_deleted", f"{ret_str}")  # 商品已刪, 不是我們錯
        if "TOKEN" in ret_str or "LOGIN" in ret_str:
            raise MtopError("token_expired", f"{ret_str}")
        if "ILEGEL" in ret_str or "ILLEGAL" in ret_str:
            raise MtopError("illegal_sign", f"{ret_str}")
        if "TRAFFIC" in ret_str or "FLOW" in ret_str or "限制" in ret_str:
            raise MtopError("rate_limit", f"{ret_str}")  # IP 被限, 要換 IP
        raise MtopError("mtop_err", f"{ret_str} - {resp.get('data',{})}")

    def _build_data(self, api: str, data: dict) -> str:
        """根據 tianya 脫殼源碼 1:1 複刻 data 結構 (非 tianya 支援的 API 直接原樣 JSON)"""
        # detail: tianya/alibabachina/detailApi.cs + DLL 的 10 欄位
        if api == "mtop.taobao.idle.pc.detail":
            item_id = str(data.get("itemId", ""))
            full = {
                "commerceAdPlanId": "",
                "extra": '{"labelIds":"36,35,9,12"}',
                "fishAdCode": "440902",
                "flowVersion": "6.0",
                "gps": "0,0",
                "isOld": False,
                "itemId": item_id,
                "latitude": "",
                "longitude": "",
                "needSimpleDetail": False,
            }
            return json.dumps(full, separators=(",", ":"))

        # 店鋪列表: tianya/alibabachina/shopApi.cs 的 9 欄位
        if api == "mtop.idle.web.xyh.item.list":
            user_id = str(data.get("userId", ""))
            page = int(data.get("pageNumber", 1))
            page_size = int(data.get("pageSize", 20))
            group_id = int(data.get("groupId", 0) or 0)
            full = {
                "city": "",
                "fishAdCode": "440902",
                "gps": "0,0",
                "latitude": "",
                "longitude": "",
                "needGroupInfo": True,
                "pageNumber": page,
                "pageSize": page_size,
                "userId": user_id,
            }
            if group_id > 0:
                full["groupId"] = group_id
                full["needGroupInfo"] = False
                if data.get("groupName"):
                    full["groupName"] = str(data["groupName"])
                if data.get("defaultGroup"):
                    full["defaultGroup"] = bool(data["defaultGroup"])
            return json.dumps(full, separators=(",", ":"))

        # 搜索: tianya/alibabachina/searchApi.cs + DLL 的完整結構
        # DLL 反編譯看不到, 但實測只帶 keyword/pageNumber/pageSize 就通
        if api == "mtop.taobao.idlemtopsearch.pc.search":
            full = {
                "keyword":    str(data.get("keyword", "")),
                "pageNumber": int(data.get("pageNumber", 1)),
                "rowsPerPage": int(data.get("pageSize", 20)),
                "fromFilter": False,
                "customDistance": "",
                "gps": "0,0",
                "latitude": "",
                "longitude": "",
                "fishAdCode": "440902",
                "bizFrom": "pc_search",
            }
            # 價格篩選 (tianya: searchFilter: priceRange:X,Y)
            if data.get("minPrice") or data.get("maxPrice"):
                full["searchFilter"] = f'priceRange:{data.get("minPrice","")},{data.get("maxPrice","")}'
            # 排序 (tianya sortField)
            if data.get("sortField"):
                for k, v in data["sortField"].items():
                    full[k] = v
            return json.dumps(full, separators=(",", ":"), ensure_ascii=False)

        # 店鋪信息: tianya/alibabachina/shopInfoApi.cs
        if api == "mtop.idle.user.page.head":
            return json.dumps({
                "fishAdCode": "440902",
                "gps": "0,0",
                "userId": str(data.get("userId", data.get("sellerId", ""))),
            }, separators=(",", ":"))

        # fallback
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def test_connection(self) -> dict:
        """GUI 啟動檢查"""
        info = {"token_valid": False, "api_ok": False, "detail": ""}
        try:
            # 1) 手機 AndServer 是否開
            r = self._main_sess.get(f"{BASE_URL}/test", timeout=4)
            if r.json().get("msg") != "ok":
                info["detail"] = "AndServer 異常"
                return info
            info["token_valid"] = True
            # 2) 模板能拿嗎
            tmpl = self._get_tmpl(force=True)
            info["api_ok"] = True
            info["detail"] = (
                f"手機 OK: utdid={tmpl.get('x-utdid','')[:20]}... "
                f"ver={tmpl.get('x-app-ver','')} (直打 acs, 無 UI 跳動)"
            )
        except MtopError as e:
            info["detail"] = f"{e.code}: {e.message}"
        except Exception as e:
            info["detail"] = f"連線失敗: {e}"
        return info


# 相容別名
MtopClient = PhoneClient


# ============================================================
# PhoneCollector — 使用 ThreadPoolExecutor 並行, 無 UI 驅動
# ============================================================
from goofish_collector import GoofishCollector
from goofish_db import normalize_item


class PhoneCollector(GoofishCollector):
    """GoofishCollector 子類 — 用 client 直接 HTTP 並行, 無 deep link"""

    PHONE_MAX_WORKERS = 16   # 提高: phone sign 可以承受更多並發; acs 也能承受

    def collect_details_concurrent(self, item_ids, max_workers=5):
        """覆寫: 直接 ThreadPool 調 self.collect_detail — 底下的 client 已是直打 acs"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        uniq, seen = [], set()
        for i in item_ids:
            s = str(i).strip()
            if s and s not in seen:
                uniq.append(s)
                seen.add(s)
        total = len(uniq)
        if total == 0:
            self._log("[批量詳情] 無可採")
            return [], 0, 0

        workers = min(max(1, int(max_workers or 1)), self.PHONE_MAX_WORKERS)
        self._log(f"[批量详情] phone-direct 模式, 直打 acs, {workers} 並行, 共 {total} 件")

        done_ids = []
        skipped = 0
        start = time.time()
        completed = 0

        def _work(iid):
            try:
                return iid, self.collect_detail(iid, quiet=True)
            except Exception as e:
                return iid, {"__error": str(e)}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_work, iid) for iid in uniq]
            for fut in as_completed(futs):
                if self._stop:
                    break
                iid, result = fut.result()
                completed += 1
                if result and result.get("itemId") and not result.get("__sold") and not result.get("__filtered"):
                    done_ids.append(iid)
                    self._detail_logs.append(f"[保存] {iid}")
                else:
                    skipped += 1
                    if result and result.get("__sold"):
                        self._detail_logs.append(f"[已售/下架] {iid}")
                    elif result and result.get("__filtered") == "price":
                        self._detail_logs.append(f"[价格过滤] {iid}")
                    elif result and result.get("__filtered") == "time":
                        self._detail_logs.append(f"[时间过滤] {iid}")
                    elif result and result.get("__error"):
                        self._detail_logs.append(f"[错误] {iid}: {result['__error'][:80]}")
                    else:
                        self._detail_logs.append(f"[错误] {iid}")
                if completed % 20 == 0 or completed == total:
                    elapsed = time.time() - start
                    speed = (completed / elapsed * 60) if elapsed > 0 else 0
                    self._progress(completed, total, f"直打 {completed}/{total} ({speed:.0f}条/分)")
                if completed % 100 == 0 or completed == total:
                    self._log(f"  [批量详情] 进度 {completed}/{total}, 成功{len(done_ids)}, 跳过{skipped}")

        self._log(f"[批量详情] 完成: {total}个商品, 保存{len(done_ids)}个, 跳过{skipped}个")
        return done_ids, len(done_ids), skipped
