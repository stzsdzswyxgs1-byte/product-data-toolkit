"""
闲鱼采集器 — 手机端一键配置 (v3: 带自动诊断 + fallback)
==================================================

和 v2 的区别:
  - 每一步都写 setup_log.txt, 出问题直接把这个文件发回来诊断
  - 永远装 LSPosed 管理器 APK (不管 LSPosed 是否已装)
  - 重启后自动验证 LSPosed 是否加载了模块
  - 如果 AndServer 没响应, 自动采集诊断信息, 并打开 LSPosed 管理器让用户手动勾选
  - 失败时自动打包日志到 setup_log.txt

需要:
  1. Android 手机 (带 Magisk root + Zygisk, Android 8+)
  2. 设置 → 开发者选项 → USB 调试 开启
  3. USB 线连电脑
"""
import subprocess
import sqlite3
import sys
import time
import os
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
ADB = str((HERE.parent / "platform-tools" / "adb.exe").resolve())
LSPOSED_ZIP = str(HERE / "LSPosed_v1.9.2_zygisk.zip")
APPSIGN_APK = str(HERE / "appsign_patched_v5.apk")
LOG_PATH = HERE / "setup_log.txt"

# ============================================================
# 日志系统: 屏幕 + 文件 双写
# ============================================================
_LOG_FH = None

def log_init():
    global _LOG_FH
    _LOG_FH = open(LOG_PATH, "w", encoding="utf-8")
    _LOG_FH.write(f"=== 闲鱼采集器手机端配置日志 {datetime.now()} ===\n")
    _LOG_FH.flush()

def log(msg, to_screen=True):
    """写入日志文件 + (可选) 打屏幕"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    if _LOG_FH:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass
    if to_screen:
        print(msg)

def log_only(msg):
    """只写文件, 不上屏"""
    log(msg, to_screen=False)


def run(cmd, input_=None, timeout=30, log_cmd=True):
    """执行命令并返回 (code, stdout, stderr), 全部写日志文件"""
    if log_cmd:
        if isinstance(cmd, list):
            log_only(f"$ {' '.join(cmd[-3:]) if len(cmd) > 3 else ' '.join(cmd)}")
        else:
            log_only(f"$ {cmd[:200]}")
    try:
        p = subprocess.run(
            cmd, input=input_, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if log_cmd:
            if out: log_only(f"  out: {out[:500]}")
            if err: log_only(f"  err: {err[:300]}")
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        log_only(f"  TIMEOUT")
        return -1, "", "timeout"


def wait_boot(max_wait=150):
    log("  等待手机开机... ", True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        _, out, _ = run([ADB, "wait-for-device", "shell", "getprop sys.boot_completed"],
                        timeout=10, log_cmd=False)
        if out.strip() == "1":
            print("OK", flush=True)
            time.sleep(3)
            return True
        print(".", end="", flush=True)
        time.sleep(3)
    print("  [超时]")
    return False


def step(n, text):
    log(f"\n========== [{n}] {text} ==========")


# ============================================================
# 诊断: AndServer 起不来时收集一切信息写 setup_log.txt
# ============================================================
def collect_diagnostic(dev, ip):
    """收集手机状态, 写到 setup_log.txt"""
    log("\n" + "=" * 60, True)
    log("  开始自动诊断 (所有信息会写入 setup_log.txt)", True)
    log("=" * 60, True)

    diag_items = [
        ("手机基本信息",
         f"su -c 'getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; getprop ro.miui.ui.version.name'"),
        ("Magisk 版本",
         f"su -c 'magisk -V; magisk --sqlite \"select * from settings\"'"),
        ("Magisk 模块列表",
         f"su -c 'ls -la /data/adb/modules/'"),
        ("LSPosed 目录结构",
         f"su -c 'ls -la /data/adb/lspd/ 2>/dev/null; ls -la /data/adb/lspd/config/ 2>/dev/null'"),
        ("LSPosed DB - modules 表",
         f"su -c 'sqlite3 /data/adb/lspd/config/modules_config.db \"SELECT mid,module_pkg_name,enabled FROM modules;\" 2>&1'"),
        ("LSPosed DB - scope 表",
         f"su -c 'sqlite3 /data/adb/lspd/config/modules_config.db \"SELECT * FROM scope;\" 2>&1'"),
        ("LSPosed 自己的 log (最关键 - 这里看 daemon 崩溃原因)",
         f"su -c 'ls -la /data/adb/lspd/log/ 2>/dev/null; echo ---; cat /data/adb/lspd/log/*.log 2>/dev/null | tail -100'"),
        ("LSPosed 旧 log",
         f"su -c 'ls -la /data/adb/lspd/log.old/ 2>/dev/null; cat /data/adb/lspd/log.old/*.log 2>/dev/null | tail -60'"),
        ("appsign 模块包路径",
         "pm path com.tianya.idlefish7920"),
        ("appsign 模块版本",
         "dumpsys package com.tianya.idlefish7920 | grep -E 'versionName|versionCode' | head -3"),
        ("LSPosed 管理器包路径",
         "pm path org.lsposed.manager"),
        ("闲鱼 APP 版本",
         "dumpsys package com.taobao.idlefish | grep -E 'versionName|versionCode' | head -3"),
        ("闲鱼 APP 是否在跑",
         f"su -c 'ps -A | grep idlefish | grep -v grep'"),
        ("10102 端口谁在监听",
         f"su -c 'netstat -tlnp 2>/dev/null | grep 10102; ss -tlnp 2>/dev/null | grep 10102'"),
        ("LSPosed daemon 是否在跑",
         f"su -c 'ps -A | grep -E \"lspd|daemon\" | grep -v grep'"),
        ("logcat LSPosed 相关",
         f"su -c 'logcat -d -t 300 | grep -iE \"LSPosed|idlefish7920|appsign|AppSignSample\" | tail -40 2>&1'"),
        ("logcat 闲鱼进程 hook 相关",
         f"su -c 'logcat -d -t 500 | grep -iE \"com.taobao.idlefish.*Loaded|hook|xposed\" | tail -30 2>&1'"),
        ("logcat 错误/崩溃",
         f"su -c 'logcat -d -t 1000 -b crash | tail -40'"),
    ]

    for name, cmd in diag_items:
        log(f"\n--- {name} ---", True)
        _, out, err = run([ADB, "-s", dev, "shell", cmd], timeout=15)
        combined = out or err or "(空)"
        # 诊断结果上屏 + 写日志
        for line in combined.split("\n")[:20]:
            log(f"  {line}")

    log("\n" + "=" * 60, True)
    log(f"  诊断完成. 日志已保存: {LOG_PATH}", True)
    log("  请把这个 setup_log.txt 发给开发者", True)
    log("=" * 60, True)


# ============================================================
# 自动修复尝试: LSPosed 把模块 scope 加到闲鱼
# ============================================================
def auto_fix_lsposed_scope(dev, apk_path, verbose=True):
    """多种途径尝试把 scope 写入 LSPosed. 写完检查是否生效"""
    log_only("\n[自动修复] 尝试重写 LSPosed DB...")
    if verbose:
        log("  自动修复: 重写 LSPosed scope 并 kill daemon")

    # 1) 强制重建 DB (删除后重写)
    run([ADB, "-s", dev, "shell",
         "su -c \"mkdir -p /data/adb/lspd/config && chmod 700 /data/adb/lspd/config\""])

    local_db = os.path.join(os.environ.get("TEMP", "."), f"_lsp_fix_{int(time.time())}.db")
    # 先试拉现有 DB (如果有)
    _, pull_out, _ = run([ADB, "-s", dev, "shell",
                          "su -c \"[ -f /data/adb/lspd/config/modules_config.db ] && cp /data/adb/lspd/config/modules_config.db /sdcard/lsp_fix.db && chmod 666 /sdcard/lsp_fix.db && echo EXIST || echo MISSING\""])
    if "EXIST" in pull_out:
        run([ADB, "-s", dev, "pull", "/sdcard/lsp_fix.db", local_db])

    # 写 DB
    conn = sqlite3.connect(local_db)
    c = conn.cursor()

    # 修复:如果之前的 bug 留下了坏的 configs 表 schema (缺 `group` 列), 清掉重建
    # LSPosed 期望: configs(module_pkg_name, user_id, `group`, key, data)
    # ← `group` 是 SQL 保留字, 必须 backtick 引用
    try:
        cols = c.execute("PRAGMA table_info(configs)").fetchall()
        col_names = {col[1] for col in cols}
        if cols and "group" not in col_names:
            log_only(f"[auto_fix] 发现坏的 configs 表 (cols={col_names}), DROP 重建")
            c.execute("DROP TABLE configs")
    except Exception as _e:
        log_only(f"[auto_fix] 检查 configs 失败: {_e}")

    for sql in [
        "CREATE TABLE IF NOT EXISTS modules (mid integer PRIMARY KEY AUTOINCREMENT, module_pkg_name text NOT NULL UNIQUE, apk_path text NOT NULL, enabled BOOLEAN DEFAULT 0 CHECK (enabled IN (0,1)))",
        "CREATE TABLE IF NOT EXISTS scope (mid integer, app_pkg_name text NOT NULL, user_id integer NOT NULL, PRIMARY KEY (mid, app_pkg_name, user_id))",
        # configs 表用 LSPosed 期望的 schema (backtick 引用 `group` 保留字)
        "CREATE TABLE IF NOT EXISTS configs (module_pkg_name TEXT NOT NULL, user_id INTEGER NOT NULL, `group` TEXT NOT NULL, `key` TEXT NOT NULL, data BLOB, PRIMARY KEY (module_pkg_name, user_id, `group`, `key`))",
    ]:
        try: c.execute(sql)
        except Exception as _e: log_only(f"[auto_fix] sql 失败: {sql[:60]} → {_e}")
    c.execute("INSERT OR REPLACE INTO modules (module_pkg_name, apk_path, enabled) VALUES (?, ?, 1)",
              ("com.tianya.idlefish7920", apk_path))
    mid = c.execute("SELECT mid FROM modules WHERE module_pkg_name=?",
                    ("com.tianya.idlefish7920",)).fetchone()[0]
    c.execute("DELETE FROM scope WHERE mid=?", (mid,))
    c.execute("INSERT INTO scope (mid, app_pkg_name, user_id) VALUES (?, ?, 0)",
              (mid, "com.taobao.idlefish"))
    c.execute("INSERT INTO scope (mid, app_pkg_name, user_id) VALUES (?, ?, 0)",
              (mid, "android"))
    conn.commit()
    conn.close()

    run([ADB, "-s", dev, "push", local_db, "/sdcard/lsp_fix.db"])
    run([ADB, "-s", dev, "shell",
         "su -c \"cp /sdcard/lsp_fix.db /data/adb/lspd/config/modules_config.db "
         "&& rm -f /data/adb/lspd/config/modules_config.db-wal /data/adb/lspd/config/modules_config.db-shm "
         "&& chmod 600 /data/adb/lspd/config/modules_config.db "
         "&& chown root:root /data/adb/lspd/config/modules_config.db\""])
    try: os.remove(local_db)
    except: pass

    # 验证 DB 真的写进去了 — 用 PC 本地 Python sqlite3 (手机 shell 可能没 sqlite3 命令)
    verify_db = os.path.join(os.environ.get("TEMP", "."), f"_lsp_verify_{int(time.time())}.db")
    run([ADB, "-s", dev, "shell",
         "su -c \"cp /data/adb/lspd/config/modules_config.db /sdcard/lsp_v.db && chmod 666 /sdcard/lsp_v.db\""], log_cmd=False)
    _, _, _ = run([ADB, "-s", dev, "pull", "/sdcard/lsp_v.db", verify_db], log_cmd=False)
    scope_count = -1
    mod_enabled = -1
    if os.path.exists(verify_db):
        try:
            _vc = sqlite3.connect(verify_db)
            scope_count = _vc.execute(
                "SELECT COUNT(*) FROM scope WHERE app_pkg_name='com.taobao.idlefish'").fetchone()[0]
            row = _vc.execute(
                "SELECT enabled FROM modules WHERE module_pkg_name='com.tianya.idlefish7920'").fetchone()
            mod_enabled = row[0] if row else -1
            _vc.close()
            os.remove(verify_db)
        except Exception as _e:
            log_only(f"[验证] 读 DB 失败: {_e}")
    if verbose:
        log(f"  [验证] 闲鱼 scope 条数={scope_count}, 模块 enabled={mod_enabled}")
    return (scope_count > 0 and mod_enabled == 1)


# ============================================================
# V2 方案: 插入 scope 到 LSPosed 已建好的 DB
#   关键点:
#   - 不 CREATE TABLE (避免 Room schema hash 不匹配被销毁)
#   - 只 INSERT OR REPLACE (保留 LSPosed 的 Room 元数据)
#   - 插入后 killall lspd, daemon 自动重生并重载 DB
#   - force-stop 闲鱼, 下次 fork 时被 Zygisk+LSPosed 注入
# ============================================================
def insert_scope_into_lsposed_db(dev, apk_path, max_retries=10):
    """
    往 LSPosed 已建好的 DB 插入 module + scope. 不碰 schema.
    关键点:
      - LSPosed 用 WAL, 必须连 .db-wal / .db-shm 一起处理
      - daemon 懒初始化 Room, 可能要启动 LSPosed 管理器触发 schema 创建
    """
    db_path = "/data/adb/lspd/config/modules_config.db"

    # 等 LSPosed daemon 建好 DB. 如果表一直空, 启动 LSPosed 管理器触发 Room 初始化
    for i in range(max_retries):
        _, chk, _ = run([ADB, "-s", dev, "shell",
            f"su -c '[ -f {db_path} ] && echo EXIST || echo MISSING'"], log_cmd=False)
        if "EXIST" not in chk:
            log_only(f"[insert_scope] DB 文件还没建 ({i+1}/{max_retries}), 等 3 秒...")
            time.sleep(3)
            continue

        # 先 WAL checkpoint, 把 -wal 的内容 flush 到主 .db (手机上 sqlite3 通常不存在, 用其他手段)
        # 策略: 强拉 .db + .db-wal + .db-shm 三个到 PC, 让 Python sqlite3 读 WAL
        run([ADB, "-s", dev, "shell",
             f"su -c 'cp {db_path} /sdcard/lsp_ins.db; "
             f"cp {db_path}-wal /sdcard/lsp_ins.db-wal 2>/dev/null; "
             f"cp {db_path}-shm /sdcard/lsp_ins.db-shm 2>/dev/null; "
             f"chmod 666 /sdcard/lsp_ins.db* 2>/dev/null'"], log_cmd=False)
        local_db = os.path.join(os.environ.get("TEMP", "."), f"_lsp_v2_{int(time.time())}.db")
        run([ADB, "-s", dev, "pull", "/sdcard/lsp_ins.db", local_db], log_cmd=False)
        run([ADB, "-s", dev, "pull", "/sdcard/lsp_ins.db-wal", local_db + "-wal"], log_cmd=False)
        run([ADB, "-s", dev, "pull", "/sdcard/lsp_ins.db-shm", local_db + "-shm"], log_cmd=False)

        if not os.path.exists(local_db):
            log_only(f"[insert_scope] pull 失败, 重试 ({i+1}/{max_retries})")
            time.sleep(3)
            continue

        # 检查 schema
        try:
            conn = sqlite3.connect(local_db)
            tables_found = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            log_only(f"[insert_scope] round {i+1}, tables: {tables_found}")
            if "modules" in tables_found and "scope" in tables_found:
                break  # schema 准备好了
        except Exception as _e:
            log_only(f"[insert_scope] 读 schema 失败: {_e}")

        # 清本轮文件, 下轮重拉
        for suf in ("", "-wal", "-shm"):
            try: os.remove(local_db + suf)
            except: pass

        # round 3 如果还是空, 主动启动 LSPosed 管理器触发 Room 建表
        if i == 2:
            log("  [触发] DB 还空, 启动 LSPosed 管理器让它建表...")
            run([ADB, "-s", dev, "shell",
                 "monkey -p org.lsposed.manager -c android.intent.category.LAUNCHER 1"],
                log_cmd=False)
            time.sleep(8)
            run([ADB, "-s", dev, "shell", "am force-stop org.lsposed.manager"], log_cmd=False)

        time.sleep(3)
    else:
        log("  ✗ LSPosed DB 一直没建好. daemon 可能挂了, 看 /data/adb/lspd/log/")
        return False

    # INSERT (不 CREATE, 尊重 LSPosed 已有 schema)
    try:
        conn = sqlite3.connect(local_db)
        conn.execute("PRAGMA foreign_keys = ON")  # 遵守 LSPosed FK
        c = conn.cursor()

        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        log_only(f"[insert_scope] DB 里的表: {tables}")
        if "modules" not in tables or "scope" not in tables:
            log(f"  ✗ DB 缺关键表 ({tables}), LSPosed daemon 可能没正确初始化")
            conn.close()
            return False

        # 读 modules 表列, 兼容可能的列名变化
        mod_cols = {col[1] for col in c.execute("PRAGMA table_info(modules)")}
        log_only(f"[insert_scope] modules 列: {mod_cols}")

        c.execute("INSERT OR REPLACE INTO modules (module_pkg_name, apk_path, enabled) VALUES (?, ?, 1)",
                  ("com.tianya.idlefish7920", apk_path))
        row = c.execute("SELECT mid FROM modules WHERE module_pkg_name=?",
                        ("com.tianya.idlefish7920",)).fetchone()
        if not row:
            log("  ✗ INSERT module 后读不到 mid")
            conn.close()
            return False
        mid = row[0]

        # 清该 module 的旧 scope, 再写新 scope
        c.execute("DELETE FROM scope WHERE mid=?", (mid,))
        for target in ["com.taobao.idlefish", "android"]:
            try:
                c.execute("INSERT INTO scope (mid, app_pkg_name, user_id) VALUES (?, ?, 0)",
                          (mid, target))
            except sqlite3.IntegrityError as e:
                log_only(f"[insert_scope] insert {target} 失败 (可能 FK 约束): {e}")

        conn.commit()

        # 验证
        scope_cnt = c.execute("SELECT COUNT(*) FROM scope WHERE mid=?", (mid,)).fetchone()[0]
        log(f"  [DB] 模块 mid={mid}, enabled=1, scope 条数={scope_cnt}")

        # WAL checkpoint: 把我们 INSERT 的内容 flush 进主 .db (不然 push 回去会丢)
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception as _e:
            log_only(f"[insert_scope] wal_checkpoint 警告: {_e}")
        conn.close()

        if scope_cnt < 1:
            log("  ✗ scope 插入后条数仍为 0")
            return False
    except Exception as e:
        log(f"  ✗ DB 操作失败: {type(e).__name__}: {e}")
        try: conn.close()
        except: pass
        return False

    # 推回 (只推主 .db, -wal/-shm 在手机上会被删掉以强制重读)
    run([ADB, "-s", dev, "push", local_db, "/sdcard/lsp_ins.db"], log_cmd=False)
    run([ADB, "-s", dev, "shell",
         f"su -c 'cp /sdcard/lsp_ins.db {db_path} "
         f"&& rm -f {db_path}-wal {db_path}-shm "
         f"&& chmod 600 {db_path} && chown root:root {db_path}'"])
    # 清 PC 端临时文件 (.db + .db-wal + .db-shm)
    for suf in ("", "-wal", "-shm"):
        try: os.remove(local_db + suf)
        except: pass

    # kill lspd 让 daemon 重载 DB (Magisk 会自动重生)
    run([ADB, "-s", dev, "shell", "su -c 'killall lspd 2>/dev/null; killall -9 lspd 2>/dev/null'"], log_cmd=False)
    time.sleep(3)

    # force-stop 闲鱼, 下次 launch 会被新 scope 命中
    run([ADB, "-s", dev, "shell", "am force-stop com.taobao.idlefish"], log_cmd=False)

    log("  ✓ scope 已插入, lspd 已重启, 闲鱼已强退 (下次启动会被 hook)")
    return True


# ============================================================
# MAIN
# ============================================================
def main():
    log_init()
    print("=" * 60)
    print("  闲鱼采集器 — 手机端一键配置 v3 (带自动诊断)")
    print("=" * 60)
    log(f"setup 日志: {LOG_PATH}", False)

    # 0) ADB 设备
    step(0, "检测 ADB 设备")
    code, out, _ = run([ADB, "devices"])
    lines = [l for l in out.split("\n") if "\t" in l and "device" in l]
    devices = [l.split("\t")[0] for l in lines if not l.startswith("sigma")]
    if not devices:
        log("✗ 没检测到手机! 检查 USB + USB调试 + 解锁弹窗")
        sys.exit(1)
    dev = devices[0]
    log(f"✓ 手机: {dev}")

    # 1) Root
    step(1, "检测 Root")
    print("  [重要] 手机上会弹 '允许 shell 获取 root 权限?', 请点[允许]")
    got_root = False
    for attempt in range(20):
        _, out, _ = run([ADB, "-s", dev, "shell", "su -c id"], timeout=15, log_cmd=False)
        if "uid=0" in out:
            got_root = True
            break
        _, out2, _ = run([ADB, "-s", dev, "shell", "su 0 id"], timeout=10, log_cmd=False)
        if "uid=0" in out2:
            got_root = True
            break
        if attempt == 0:
            log("  等你在手机上点 [允许] ...")
        elif attempt % 3 == 0:
            log(f"  仍在等 ({(attempt+1)*3}/60 秒)")
        time.sleep(3)
    if not got_root:
        log("\n✗ 60 秒拿不到 root. 到 Magisk 管理器 → 超级用户 → Shell → 允许")
        sys.exit(1)
    log("✓ Root OK")

    _, magisk_ver, _ = run([ADB, "-s", dev, "shell", "su -c \"magisk -V 2>/dev/null\""])
    if magisk_ver: log(f"  Magisk: {magisk_ver}")

    # 2) Zygisk
    step(2, "检测 Zygisk")
    _, out, _ = run([ADB, "-s", dev, "shell",
                     "su -c \"magisk --sqlite 'select * from settings'\""])
    if "zygisk|value=1" not in out:
        log("✗ Zygisk 没启用! 打开 Magisk → 设置 → Zygisk → 打开 → 重启手机")
        sys.exit(1)
    log("✓ Zygisk 启用")

    # 3) LSPosed
    step(3, "安装 LSPosed Xposed 框架")
    _, modules, _ = run([ADB, "-s", dev, "shell",
                         "su -c \"ls /data/adb/modules/ 2>/dev/null\""])
    if "zygisk_lsposed" in modules:
        log("✓ LSPosed 已安装")
    else:
        log("  推送 LSPosed zip + Magisk 模块方式安装...")
        run([ADB, "-s", dev, "push", LSPOSED_ZIP, "/sdcard/LSPosed.zip"])
        code, out, err = run([ADB, "-s", dev, "shell",
                              "su -c \"magisk --install-module /sdcard/LSPosed.zip\""],
                             timeout=120)
        if "Done" not in out and "Welcome to LSPosed" not in out:
            log(f"✗ LSPosed 安装失败: {out[-500:]}")
            sys.exit(1)
        log("✓ LSPosed 模块已安装")

    # 3b) LSPosed 管理器 APK — 装 or 延后装
    # 坑: 首次装 LSPosed 时 manager.apk 在 /data/adb/modules_update/ (Magisk 两阶段提交),
    #     要重启后才会搬到 /data/adb/modules/. 所以这里两条路径都 try,
    #     都失败也不退出 — 步骤 7 重启后有 safety net 补装.
    log("  确保 LSPosed 管理器 APK 已装...")
    _, lsp_mgr, _ = run([ADB, "-s", dev, "shell", "pm path org.lsposed.manager"])
    if "package:" in lsp_mgr:
        log("  ✓ LSPosed 管理器已在")
    else:
        _, cp_out, _ = run([ADB, "-s", dev, "shell",
             "su -c \"cp /data/adb/modules/zygisk_lsposed/manager.apk /data/local/tmp/lsp-manager.apk 2>/dev/null "
             "|| cp /data/adb/modules_update/zygisk_lsposed/manager.apk /data/local/tmp/lsp-manager.apk 2>/dev/null; "
             "[ -f /data/local/tmp/lsp-manager.apk ] && chmod 644 /data/local/tmp/lsp-manager.apk && echo CP_OK || echo CP_FAIL\""])
        if "CP_OK" not in cp_out:
            log("  ⚠ manager.apk 没找到 (首次装, 需重启后补装) — 继续流程")
        else:
            _, out, _ = run([ADB, "-s", dev, "shell",
                             "su -c \"pm install -r /data/local/tmp/lsp-manager.apk\""], timeout=60)
            if "Success" in out:
                log("  ✓ LSPosed 管理器已装 (手机桌面会有图标)")
            else:
                log(f"  ⚠ LSPosed 管理器 pm install 失败, 重启后再试: {out[:150]}")

    # 4) 闲鱼 APP
    step(4, "检查 闲鱼 APP")
    _, out, _ = run([ADB, "-s", dev, "shell",
                     "dumpsys package com.taobao.idlefish | grep versionName"])
    if "versionName" not in out:
        log("✗ 没装闲鱼 APP!")
        sys.exit(1)
    ver = out.split("versionName=")[1][:12].strip()
    log(f"✓ 闲鱼已装: {ver}")

    # 5) appsign 模块
    step(5, "安装 闲鱼签名 Xposed 模块")
    run([ADB, "-s", dev, "push", APPSIGN_APK, "/data/local/tmp/appsign.apk"])
    # 先卸旧包 (可能签名不同导致 -r 失败). 忽略 "not installed" 错误.
    _, uninst_out, uninst_err = run([ADB, "-s", dev, "shell",
         "su -c \"pm uninstall com.tianya.idlefish7920\""], timeout=60)
    log_only(f"  [uninstall] out={uninst_out!r} err={uninst_err!r}")
    time.sleep(1)
    _, out, err = run([ADB, "-s", dev, "shell",
                     "su -c \"pm install -r /data/local/tmp/appsign.apk\""], timeout=90)
    log_only(f"  [install root] out={out!r} err={err!r}")
    if "Success" not in out:
        log("  root 安装失败, 试普通 pm install (手机会弹[安装], 请按[安装])")
        _, out, err = run([ADB, "-s", dev, "shell",
                         "pm install -r /data/local/tmp/appsign.apk"], timeout=90)
        log_only(f"  [install noroot] out={out!r} err={err!r}")
    if "Success" not in out:
        # 再试 --bypass-low-target-sdk-block (Android 14+) 和 -g 授权
        _, out, err = run([ADB, "-s", dev, "shell",
                         "su -c \"pm install -r -g /data/local/tmp/appsign.apk\""], timeout=90)
        log_only(f"  [install -g] out={out!r} err={err!r}")
    if "Success" not in out:
        log(f"✗ 安装失败")
        log(f"  stdout: {out[:400] or '(空)'}")
        log(f"  stderr: {err[:400] or '(空)'}")
        log("  常见原因:")
        log("    1. MIUI 純淨模式: 設定 → 隱私 → 純淨模式 → 關")
        log("    2. Play Protect: Google Play → 設定 → Play 保护 → 关")
        log("    3. 未知来源: 設定 → 應用管理 → 允許未知來源")
        log("    4. 存储满: adb shell 'df /data' 看看")
        sys.exit(1)
    _, apk_path, _ = run([ADB, "-s", dev, "shell", "pm path com.tianya.idlefish7920"])
    apk_path = apk_path.replace("package:", "").strip()
    log(f"  ✓ 模块已装: {apk_path}")

    # 6) 清掉旧的坏 DB (不再预写 schema — LSPosed v1.9.2 Room 会 reject 我们的 schema 触发 FK migrate 失败)
    step(6, "清理旧 LSPosed DB (让 daemon 重启后自建正确 schema)")
    run([ADB, "-s", dev, "shell",
         "su -c \"rm -f /data/adb/lspd/config/modules_config.db "
         "/data/adb/lspd/config/modules_config.db-wal "
         "/data/adb/lspd/config/modules_config.db-shm 2>/dev/null; "
         "mkdir -p /data/adb/lspd/config && chmod 700 /data/adb/lspd/config\""])
    log("  ✓ 旧 DB 已清, 重启后 LSPosed daemon 会用正确 schema 重建")

    # 7) 重启
    step(7, "重启手机 (90 秒左右, 只这一次)")
    run([ADB, "-s", dev, "reboot"])
    time.sleep(12)
    wait_boot(max_wait=150)
    log("  ✓ 手机已开机")
    time.sleep(5)  # 等 LSPosed daemon 完全启动并创建 DB

    # 7.4) 把 scope 写入 LSPosed 已建好的 DB (不创建表, 不碰 Room 元数据)
    log("  写入 scope 到 LSPosed 已建好的 DB...")
    ok_insert = insert_scope_into_lsposed_db(dev, apk_path)
    if not ok_insert:
        log("  ⚠ scope 插入失败, 可能需要手动在 LSPosed 管理器里配置")

    # 7.5) Safety net: 重启后补装 LSPosed 管理器 APK (首次装 3b 会失败, 这里兜底)
    # 注意: 此步骤必须在第 2 次重启前 — 重启后 manager APK 在 /data/adb/modules/ 已固定
    _, lsp_mgr_post, _ = run([ADB, "-s", dev, "shell", "pm path org.lsposed.manager"], log_cmd=False)
    if "package:" not in lsp_mgr_post:
        log("  [重启后补装] LSPosed 管理器未装, 补装中...")
        _, cp_out, _ = run([ADB, "-s", dev, "shell",
            "su -c \"cp /data/adb/modules/zygisk_lsposed/manager.apk /data/local/tmp/lsp-manager.apk "
            "&& chmod 644 /data/local/tmp/lsp-manager.apk && echo CP_OK\""])
        if "CP_OK" in cp_out:
            _, inst_out, _ = run([ADB, "-s", dev, "shell",
                "su -c \"pm install -r /data/local/tmp/lsp-manager.apk\""], timeout=60)
            if "Success" in inst_out:
                log("  ✓ LSPosed 管理器已补装 (手机桌面会有 LSPosed 图标)")
            else:
                log(f"  ✗ 管理器补装失败: {inst_out[:200]}")
        else:
            log("  ✗ 找不到 manager.apk — LSPosed 模块本身可能没装好")
    else:
        log("  ✓ LSPosed 管理器已就位")

    # 7.6) 第 2 次重启 — 只为让 LSPosed daemon 重载新 scope
    #      (killall lspd 不保证重生, 正规重启最可靠)
    if ok_insert:
        step(7.6, "第 2 次重启 (让 LSPosed 生效, 约 90 秒)")
        run([ADB, "-s", dev, "reboot"])
        time.sleep(12)
        wait_boot(max_wait=150)
        log("  ✓ 手机已开机 (scope 已生效)")
        time.sleep(5)

    # 8) 打开闲鱼 + 建 USB forward + 轮询 AndServer (USB 隧道優先, 不依賴 WiFi)
    step(8, "打开闲鱼 APP + 建 USB 隧道 → 等待 AndServer 启动")
    run([ADB, "-s", dev, "shell", "monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1"])
    log("  ✓ 闲鱼 APP 已启动")

    # 建 USB forward (PC 127.0.0.1:10102 → 手机 10102), 走 USB 不怕 WiFi 變
    run([ADB, "-s", dev, "forward", "--remove-all"], log_cmd=False)
    run([ADB, "-s", dev, "forward", "tcp:10102", "tcp:10102"])
    log("  ✓ USB 隧道已建立 (PC 127.0.0.1:10102 → 手机 10102)")

    # 順便記 WiFi IP 備用 (USB 拔了的場景)
    _, ip_out, _ = run([ADB, "-s", dev, "shell", "ip -4 addr show wlan0"])
    wifi_ip = ""
    for line in ip_out.split("\n"):
        if "inet " in line:
            wifi_ip = line.split("inet ")[1].split("/")[0].strip()
            break
    if wifi_ip:
        log(f"  手机 WiFi IP: {wifi_ip} (備用, 主路徑走 USB)")
    log("  请在手机上: 登入闲鱼账号 + 滑动首页商品流 (触发 mtop 请求)")

    # 優先測 127.0.0.1 (USB 隧道, 永遠通); 順便也測 WiFi IP (確認 hook 真的工作)
    import urllib.request, json
    ok = False
    targets = ["127.0.0.1"]
    if wifi_ip and wifi_ip != "127.0.0.1":
        targets.append(wifi_ip)
    for i in range(60):  # 最多 180 秒
        for target in targets:
            try:
                r = urllib.request.urlopen(f"http://{target}:10102/test", timeout=3)
                j = json.loads(r.read())
                if j.get("msg") == "ok":
                    log(f"\n  ✓ AndServer 响应 (via {target}): {j}")
                    ok = True
                    break
            except Exception:
                pass
        if ok:
            break
        if i % 5 == 0:
            log(f"  等 AndServer ... {i*3}秒", False)
            print(f"  等 AndServer (手机上登入+滑首页) ... {i*3}秒", flush=True)
        time.sleep(3)
    # 為了下面 collect_diagnostic 兼容
    ip = wifi_ip or "127.0.0.1"

    if not ok:
        log("\n  ⚠️ AndServer 3 分钟还没响应. 开始自动诊断...")
        collect_diagnostic(dev, ip)

        # 自动 fallback 1: 用新的 V2 方式重试插入 scope
        log("\n  [尝试修复 1] 重新插入 scope 到 LSPosed DB")
        insert_scope_into_lsposed_db(dev, apk_path)

        # 自动 fallback 2: 强退闲鱼 APP + 重启
        log("  [尝试修复 2] 强退闲鱼 APP, 重新打开")
        run([ADB, "-s", dev, "shell", "am force-stop com.taobao.idlefish"])
        time.sleep(2)
        run([ADB, "-s", dev, "shell",
             "monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1"])

        # 再试 60 秒
        log("  再等 60 秒 AndServer...")
        for i in range(20):
            try:
                r = urllib.request.urlopen(f"http://{ip}:10102/test", timeout=3)
                j = json.loads(r.read())
                if j.get("msg") == "ok":
                    log(f"  ✓ AndServer 响应 (修复生效!): {j}")
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(3)

    if not ok:
        # 最终 fallback: 打开 LSPosed 管理器让用户手动勾选
        log("\n  自动修复失败 — 把决定权交给用户")
        log("  手机上正在打开 LSPosed 管理器, 请手动操作:")
        log("    1. 左下角 [模块] → 找 'idlefish7920' → 开关打开 (变绿)")
        log("    2. 点模块进去 → [作用范围] → 勾 '闲鱼' → 返回")
        log("    3. 右上角三点 → [重启] → 等手机重启")
        log("    4. 开机后手机上打开闲鱼 APP, 滑几下首页")
        log(f"    5. PC 浏览器测 http://{ip}:10102/test 是否返回 ok")
        run([ADB, "-s", dev, "shell",
             "monkey -p org.lsposed.manager -c android.intent.category.LAUNCHER 1"])
        log(f"\n  诊断信息已写入: {LOG_PATH}")
        log("  请把这个文件发给开发者")

    # 9) phone_config.py 总是写 (就算 AndServer 没起来, IP 还是正确的 — 方便后续手动修好再用)
    step(9, "写入 PC 端 phone_config.py")
    pc_folder = None
    for child in HERE.parent.iterdir():
        if child.is_dir() and (child / "app.py").exists() and (child / "goofish_phone.py").exists():
            pc_folder = child
            break
    if pc_folder is None:
        try:
            for child in HERE.parent.parent.iterdir():
                if child.is_dir() and (child / "app.py").exists():
                    pc_folder = child
                    break
        except Exception:
            pass

    # 默認用 127.0.0.1 (USB 隧道, 跨網段也通, IP 變化也不影響)
    use_ip = "127.0.0.1"
    if pc_folder is None:
        log("  ⚠️ 找不到 PC 端目录, 手动在 app.py 同目录创建 phone_config.py:")
        log(f'    DEVICE_SERIAL = "{dev}"')
        log(f'    PHONE_IP = "{use_ip}"  # USB 隧道, 永遠通')
    else:
        pc_config_path = pc_folder / "phone_config.py"
        with open(pc_config_path, "w", encoding="utf-8") as f:
            f.write(f'"""自动生成 by setup_phone.py — USB 隧道模式 (推薦)"""\n')
            f.write(f'DEVICE_SERIAL = "{dev}"\n')
            f.write(f'# PHONE_IP=127.0.0.1 表示走 USB 隧道, 永遠通, 不依賴 WiFi IP\n')
            f.write(f'# 前提: USB 線一直插著\n')
            f.write(f'PHONE_IP = "{use_ip}"\n')
            if ip and ip != "127.0.0.1":
                f.write(f'PHONE_WIFI_IP = "{ip}"  # 備用 WiFi IP (USB 拔了走這個)\n')
        log(f"  ✓ 写入: {pc_config_path}")
        log(f"  ✓ 默認走 USB 隧道 (127.0.0.1) - 不怕 WiFi IP 變化")

    # 完成
    print("\n" + "=" * 60)
    if ok:
        print("  ✓✓✓ 手机端配置完成!")
    else:
        print("  ⚠ AndServer 没起来, 请看上面的手动步骤")
    print("=" * 60)
    print(f"  手机:        {dev}")
    print(f"  连接方式:    USB 隧道 (推薦, 不怕 IP 變)")
    print(f"  AndServer:   http://127.0.0.1:10102/test")
    if ip and ip != "127.0.0.1":
        print(f"  WiFi IP 备用: {ip}")
    print(f"  日志:        {LOG_PATH}")
    print(f"\n  ★ 重要: 手機 USB 線一直插著, 不要拔!")
    print()
    if ok:
        print("  下一步: 到 PC 端, 双击「闲鱼采集器.bat」")
    else:
        print(f"  下一步: 按上面步骤在 LSPosed 管理器手动勾选, 或把 {LOG_PATH.name} 发给开发者")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n\n用户取消")
    except Exception as e:
        log(f"\n\n错误: {type(e).__name__}: {e}")
        import traceback
        log_only(traceback.format_exc())
        traceback.print_exc()
    finally:
        if _LOG_FH:
            try: _LOG_FH.close()
            except: pass
    input("\n按 Enter 关闭 ... ")
