"""
闲鱼采集器 — GUI版 v2.0
纯HTTP + TLS伪装, 无需浏览器/模拟器
"""
import subprocess
import sys
import os
import re
import json
import random
import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from goofish_session import GoofishSession
# MtopClient/Collector 來源: "phone" 走真機 AndServer piggyback (無 H5 風控), "web" 走原 H5 cookie
# 改成 "web" 可切回原流程
_MTOP_SOURCE = "phone"
if _MTOP_SOURCE == "phone":
    from goofish_phone import MtopClient, PhoneCollector as GoofishCollector
else:
    from goofish_api import MtopClient
    from goofish_collector import GoofishCollector
from goofish_db import init_db, export_xlsx, export_csv, get_stats

import socket
import urllib.parse

VERSION = "2.0"
CLOUD_URL = "https://api.example.com"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PENDING_EXPORTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pending_exports.jsonl")

# ==================== 视觉设计系统 ====================
# 基于视觉心理学:
#   - F型阅读模式: 关键操作在左上, 日志在右
#   - 色彩心理: 蓝=信任/操作, 绿=成功/启动, 红=停止/危险, 暖灰=安全/稳定
#   - Fitts定律: 主按钮大, 次要按钮小
#   - 格式塔分组: 相关功能视觉聚合, 分隔线区分区域
#   - 对比度: WCAG AA 标准, 文字/背景对比 ≥ 4.5:1

C = {
    # 基础色
    'bg':           '#eef1f5',      # 主背景 (冷灰蓝, 减少视觉疲劳)
    'card':         '#ffffff',      # 卡片背景
    'card_border':  '#dce1e8',      # 卡片边框
    'divider':      '#e4e8ee',      # 分隔线

    # 品牌/强调
    'primary':      '#3b82f6',      # 主蓝 (信任感)
    'primary_hover':'#2563eb',
    'primary_light':'#dbeafe',      # 浅蓝背景
    'success':      '#16a34a',      # 成功绿
    'success_light':'#dcfce7',
    'danger':       '#dc2626',      # 危险红
    'danger_light': '#fee2e2',
    'warning':      '#d97706',      # 警告橙
    'warning_light':'#fef3c7',

    # 文字
    'text':         '#1e293b',      # 主文字 (深蓝灰, 比纯黑更柔和)
    'text_sec':     '#64748b',      # 次要文字
    'text_hint':    '#94a3b8',      # 提示文字
    'text_white':   '#ffffff',

    # 日志区 (视觉心理学: 微暖灰底减少眩光, 深色层级区分信息类型)
    'log_bg':       '#f5f5f0',      # 微暖灰 (比纯白降10%亮度, 减少蓝光刺激)
    'log_text':     '#374151',      # 主文字 (深灰, 非纯黑避免过高对比度)
    'log_info':     '#1d4ed8',      # 蓝色=信息 (冷色调→客观/可信)
    'log_warn':     '#b45309',      # 琥珀=警告 (暖色调→引起注意但不紧张)
    'log_error':    '#be123c',      # 玫红=错误 (比纯红柔和, 但仍醒目)
    'log_time':     '#9ca3af',      # 淡灰=时间戳 (最低视觉层级)
    'log_data':     '#374151',      # 深灰=数据 (与主文字同级)
    'log_select':   '#e0e7ff',      # 淡靛蓝=选中 (柔和反馈)

    # 进度条
    'progress_bg':  '#e2e8f0',
    'progress_fg':  '#3b82f6',
}

# 字体
FONT = 'Microsoft YaHei UI'
FONT_MONO = 'Consolas'


def _resolve_short_url(url):
    """解析 m.tb.cn 等短链接, 跟随重定向返回最终URL"""
    try:
        # ASCII兜底清洗, 防止中文字符导致latin-1编码错误
        url = url.encode('ascii', 'ignore').decode('ascii').strip()
        if not url:
            return url
        from curl_cffi import requests as _req
        resp = _req.get(url, allow_redirects=True, timeout=10, impersonate="chrome136")
        final = str(resp.url)

        # HTTP重定向成功到目标站
        if 'goofish.com' in final or 'xianyu.com' in final:
            return final

        # JS/meta重定向: 在body中找 goofish/xianyu URL
        body = resp.text[:8000]
        m = re.search(r'https?://[^\s"\'<>]+goofish\.com/[^\s"\'<>]+', body)
        if m:
            return m.group(0)
        m = re.search(r'https?://[^\s"\'<>]+xianyu\.com/[^\s"\'<>]+', body)
        if m:
            return m.group(0)

        return final
    except Exception:
        return url


def parse_input(raw: str, mode: str) -> str:
    """从URL或原始输入中提取ID/关键词"""
    raw = raw.strip()
    if not raw:
        return ""

    # 提取短链接 (m.tb.cn / tb.cn) 并解析 — 只匹配ASCII, 防止吞中文
    m = re.search(r'https?://(?:[A-Za-z0-9-]+\.)*tb\.cn/[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+', raw)
    if m:
        short_url = m.group(0).rstrip(',.;:)]}')  # 裁掉句尾常见标点
        resolved = _resolve_short_url(short_url)
        if 'goofish.com' in resolved or 'xianyu.com' in resolved:
            raw = resolved
        else:
            # 解析失败, 打印提示
            print(f"  [parse] 短链接解析失败: {short_url} -> {resolved}")

    if "goofish.com" in raw or "xianyu.com" in raw or "http" in raw:
        try:
            parsed = urlparse(raw)
            qs = parse_qs(parsed.query)
        except Exception:
            qs = {}

        if mode == "store":
            # userid 大小写都匹配 (手机端分享链接用小写 userid)
            for key in ("userId", "userid"):
                if key in qs:
                    return qs[key][0]
            m = re.search(r'userId[=:](\d+)', raw, re.IGNORECASE)
            if m:
                return m.group(1)
        elif mode == "detail":
            if "id" in qs:
                return qs["id"][0]
            m = re.search(r'[?&]id=(\d+)', raw)
            if m:
                return m.group(1)
        elif mode == "search":
            if "q" in qs:
                return qs["q"][0]

    if mode in ("store", "detail"):
        nums = re.findall(r'\d{5,}', raw)
        if nums:
            return max(nums, key=len)

    if mode in ("store", "detail"):
        return ""

    return raw


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(f"闲鱼采集器 v{VERSION}")
        self.geometry("960x820")
        self.minsize(850, 750)
        self.configure(bg=C['bg'])

        # 状态
        self.session = None
        self.client = None
        self.conn = init_db()       # 数据库始终可用 (不依赖cookie)
        self.collector = None
        self._running = False
        self._login_running = False
        self._worker = None
        self._cloud_poll_thread = None  # 防止轮询线程叠加
        self._log_queue = queue.Queue()
        self._progress_queue = queue.Queue()
        self._current_mode = "store"

        self._build_ui()
        self._load_config()       # 恢复上次设置
        self._poll_queues()
        self._auto_save_loop()    # 每30秒自动保存 (防闪退)

        # 启动时自动初始化
        self.after(200, self._init_session)

    # ==================== UI 构建 ====================

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # --- 全局样式 ---
        style.configure(".", font=(FONT, 9), background=C['bg'])
        style.configure("TFrame", background=C['bg'])
        style.configure("TLabel", background=C['bg'], foreground=C['text'])

        # 标题
        style.configure("Title.TLabel", font=(FONT, 14, "bold"),
                        foreground=C['text'], background=C['bg'])
        style.configure("StatusOK.TLabel", font=(FONT, 9),
                        foreground=C['success'], background=C['bg'])
        style.configure("StatusWarn.TLabel", font=(FONT, 9),
                        foreground=C['warning'], background=C['bg'])
        style.configure("StatusOff.TLabel", font=(FONT, 9),
                        foreground=C['text_hint'], background=C['bg'])
        style.configure("DB.TLabel", font=(FONT, 9),
                        foreground=C['text_sec'], background=C['bg'])

        # 卡片 LabelFrame
        style.configure("Card.TLabelframe", background=C['card'],
                        relief="solid", borderwidth=1, bordercolor=C['card_border'])
        style.configure("Card.TLabelframe.Label", font=(FONT, 10, "bold"),
                        foreground=C['text'], background=C['card'])

        # 按钮样式 (Fitts定律: 主操作大+醒目, 次操作小+低调)
        style.configure("Start.TButton", font=(FONT, 10, "bold"), padding=(16, 7),
                        foreground=C['text_white'], background=C['success'])
        style.map("Start.TButton",
                  background=[('active', '#15803d'), ('disabled', '#cbd5e1')],
                  foreground=[('disabled', '#94a3b8')])

        style.configure("Stop.TButton", font=(FONT, 10, "bold"), padding=(16, 7),
                        foreground=C['text_white'], background=C['danger'])
        style.map("Stop.TButton",
                  background=[('active', '#b91c1c'), ('disabled', '#cbd5e1')],
                  foreground=[('disabled', '#94a3b8')])

        style.configure("Primary.TButton", font=(FONT, 9, "bold"), padding=(10, 5),
                        foreground=C['text_white'], background=C['primary'])
        style.map("Primary.TButton",
                  background=[('active', C['primary_hover']), ('disabled', '#cbd5e1')],
                  foreground=[('disabled', '#94a3b8')])

        style.configure("Tool.TButton", font=(FONT, 9), padding=(8, 4),
                        foreground=C['text'], background='#f1f5f9')
        style.map("Tool.TButton",
                  background=[('active', '#e2e8f0'), ('disabled', '#f1f5f9')],
                  foreground=[('active', C['primary']), ('disabled', '#94a3b8')])

        style.configure("Small.TButton", font=(FONT, 8), padding=(6, 3),
                        foreground=C['text_sec'], background='#f1f5f9')
        style.map("Small.TButton",
                  background=[('active', '#e2e8f0')],
                  foreground=[('active', C['text'])])

        style.configure("Danger.TButton", font=(FONT, 8), padding=(6, 3),
                        foreground=C['danger'], background='#fef2f2')
        style.map("Danger.TButton",
                  background=[('active', '#fee2e2')],
                  foreground=[('active', '#b91c1c')])

        # 进度条
        style.configure("Custom.Horizontal.TProgressbar",
                        troughcolor=C['progress_bg'], background=C['progress_fg'],
                        thickness=8)

        # RadioButton
        style.configure("Mode.TRadiobutton", font=(FONT, 9, "bold"),
                        background=C['card'], foreground=C['text'])

        # Entry
        style.configure("TEntry", fieldbackground="#ffffff")

        # 提示文字
        style.configure("Hint.TLabel", font=(FONT, 8),
                        foreground=C['text_hint'], background=C['card'])
        style.configure("Stats.TLabel", font=(FONT, 8),
                        foreground=C['text_sec'], background=C['card'])

        # ==================== 布局 ====================

        # --- 顶部状态栏 ---
        top = tk.Frame(self, bg=C['card'], padx=16, pady=10)
        top.pack(fill=tk.X)

        tk.Label(top, text="闲鱼采集器", font=(FONT, 15, "bold"),
                 fg=C['text'], bg=C['card']).pack(side=tk.LEFT)

        tk.Label(top, text=f"v{VERSION}", font=(FONT, 9),
                 fg=C['text_hint'], bg=C['card']).pack(side=tk.LEFT, padx=(6, 0))

        # 状态指示灯
        self._status_frame = tk.Frame(top, bg=C['card'])
        self._status_frame.pack(side=tk.LEFT, padx=(16, 0))

        self._status_dot = tk.Canvas(self._status_frame, width=8, height=8,
                                     bg=C['card'], highlightthickness=0)
        self._status_dot.pack(side=tk.LEFT, padx=(0, 4))
        self._status_dot.create_oval(1, 1, 7, 7, fill=C['text_hint'], outline="", tags="dot")

        self.lbl_status = tk.Label(self._status_frame, text="初始化中...",
                                   font=(FONT, 9), fg=C['text_hint'], bg=C['card'])
        self.lbl_status.pack(side=tk.LEFT)

        # 数据库统计 (右侧)
        self.lbl_db = tk.Label(top, text="", font=(FONT, 9),
                               fg=C['text_sec'], bg=C['card'])
        self.lbl_db.pack(side=tk.RIGHT)

        # 顶部分隔线
        tk.Frame(self, height=1, bg=C['card_border']).pack(fill=tk.X)

        # --- 主体 (PanedWindow: 可拖拽调整左右比例) ---
        body = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg=C['divider'],
                               sashwidth=5, sashrelief=tk.FLAT,
                               opaqueresize=True)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 左侧: 操作面板
        left = tk.Frame(body, bg=C['bg'])

        self._build_mode_panel(left)
        self._build_action_buttons(left)
        self._build_tools_panel(left)
        self._build_login_panel(left)
        self._build_cloud_sync_panel(left)

        body.add(left, minsize=360, width=400, sticky='nsew')

        # 右侧: 日志 + 进度
        right = tk.Frame(body, bg=C['bg'])

        self._build_log_panel(right)
        self._build_progress_bar(right)

        body.add(right, minsize=300, sticky='nsew')

    def _build_mode_panel(self, parent):
        frm = ttk.LabelFrame(parent, text="  采集模式  ", style="Card.TLabelframe",
                              padding=(12, 8))
        frm.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.mode_var = tk.StringVar(value="store")

        modes = [
            ("store",  "店铺"),
            ("search", "搜索"),
            ("detail", "商品"),
        ]

        # 每个模式独立保存输入内容
        self._mode_texts = {"store": "", "search": "", "detail": ""}

        mode_frame = tk.Frame(frm, bg=C['card'])
        mode_frame.pack(fill=tk.X, pady=(0, 10))
        for val, text in modes:
            rb = ttk.Radiobutton(mode_frame, text=text, variable=self.mode_var,
                                 value=val, command=self._on_mode_change,
                                 style="Mode.TRadiobutton")
            rb.pack(side=tk.LEFT, padx=(0, 14))

        # 输入区 (填满剩余空间)
        input_frame = tk.Frame(frm, bg=C['card'])
        input_frame.pack(fill=tk.BOTH, expand=True)

        self.lbl_input = tk.Label(input_frame, text="店铺:", anchor=tk.NW,
                                  font=(FONT, 9), fg=C['text'], bg=C['card'])
        self.lbl_input.pack(side=tk.LEFT, anchor=tk.N, pady=(4, 0))

        text_frame = tk.Frame(input_frame, bg=C['card_border'], padx=1, pady=1)
        text_frame.pack(side=tk.LEFT, padx=(4, 0), fill=tk.BOTH, expand=True)

        self.ent_input = tk.Text(text_frame, width=28, height=5, font=(FONT, 9),
                                 wrap=tk.CHAR, relief=tk.FLAT, bg="#ffffff",
                                 fg=C['text'], insertbackground=C['text'],
                                 selectbackground=C['primary_light'])
        input_scroll_y = ttk.Scrollbar(text_frame, orient=tk.VERTICAL,
                                        command=self.ent_input.yview)
        self.ent_input.config(yscrollcommand=input_scroll_y.set)
        input_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.ent_input.pack(fill=tk.BOTH, expand=True)

        # 右键菜单
        self._input_menu = tk.Menu(self.ent_input, tearoff=0)
        self._input_menu.add_command(label="全选", command=self._input_select_all)
        self._input_menu.add_command(label="复制", command=self._input_copy)
        self._input_menu.add_command(label="粘贴", command=self._input_paste)
        self._input_menu.add_command(label="删除", command=self._input_delete)
        self.ent_input.bind("<Button-3>", self._show_input_menu)

        r2 = tk.Frame(frm, bg=C['card'])
        r2.pack(fill=tk.X, pady=(2, 0))
        self.lbl_input_hint = tk.Label(r2, text="", font=(FONT, 8),
                                       fg=C['text_hint'], bg=C['card'])
        self.lbl_input_hint.pack(side=tk.LEFT, padx=(0, 0))

        # 每词/每店 最大页数设置
        r3 = tk.Frame(frm, bg=C['card'])
        r3.pack(fill=tk.X, pady=(4, 0))
        self.lbl_pages = tk.Label(r3, text="每项页数:", font=(FONT, 8),
                                   fg=C['text_sec'], bg=C['card'])
        self.lbl_pages.pack(side=tk.LEFT)
        self.ent_max_pages = ttk.Entry(r3, width=5, font=(FONT, 8))
        self.ent_max_pages.insert(0, "999")
        self.ent_max_pages.pack(side=tk.LEFT, padx=(4, 4))
        self.lbl_pages_hint = tk.Label(r3, text="(999=采到底)", font=(FONT, 8),
                                        fg=C['text_hint'], bg=C['card'])
        self.lbl_pages_hint.pack(side=tk.LEFT)

        # 价格区间
        r4 = tk.Frame(frm, bg=C['card'])
        r4.pack(fill=tk.X, pady=(4, 0))
        tk.Label(r4, text="价格区间:", font=(FONT, 8),
                 fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_min_price = ttk.Entry(r4, width=7, font=(FONT, 8))
        self.ent_min_price.pack(side=tk.LEFT, padx=(4, 0))
        tk.Label(r4, text="~", font=(FONT, 8),
                 fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT, padx=2)
        self.ent_max_price = ttk.Entry(r4, width=7, font=(FONT, 8))
        self.ent_max_price.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(r4, text="元 (空=不限)", font=(FONT, 8),
                 fg=C['text_hint'], bg=C['card']).pack(side=tk.LEFT)

        # 发布时间
        r5 = tk.Frame(frm, bg=C['card'])
        r5.pack(fill=tk.X, pady=(4, 0))
        tk.Label(r5, text="发布时间:", font=(FONT, 8),
                 fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_publish_days = tk.Entry(r5, width=6, font=(FONT, 8))
        self.ent_publish_days.pack(side=tk.LEFT, padx=(4, 0))
        tk.Label(r5, text="天内 (空=不限)", font=(FONT, 8),
                 fg=C['text_hint'], bg=C['card']).pack(side=tk.LEFT)

        # 并发线程数
        r6 = tk.Frame(frm, bg=C['card'])
        r6.pack(fill=tk.X, pady=(4, 0))
        tk.Label(r6, text="并发线程:", font=(FONT, 8),
                 fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_workers = tk.Entry(r6, width=4, font=(FONT, 8))
        self.ent_workers.insert(0, "6")
        self.ent_workers.pack(side=tk.LEFT, padx=(4, 0))
        tk.Label(r6, text="(商品模式/云端同步)", font=(FONT, 8),
                 fg=C['text_hint'], bg=C['card']).pack(side=tk.LEFT)

        self._update_input_hint()

    def _build_action_buttons(self, parent):
        frm = tk.Frame(parent, bg=C['bg'], pady=2)
        frm.pack(fill=tk.X, pady=(0, 6))

        self.btn_start = ttk.Button(frm, text="  开始采集  ", style="Start.TButton",
                                    command=self._on_start)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_stop = ttk.Button(frm, text="  停止  ", style="Stop.TButton",
                                   command=self._on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_refresh = ttk.Button(frm, text="刷新Token", style="Tool.TButton",
                                      command=self._on_refresh_token)
        self.btn_refresh.pack(side=tk.RIGHT)

    def _build_tools_panel(self, parent):
        frm = ttk.LabelFrame(parent, text="  数据工具  ", style="Card.TLabelframe",
                              padding=(10, 6))
        frm.pack(fill=tk.X, pady=(0, 6))

        # 第一行: 导出 + 打开文件夹 + 清空数据
        r1 = tk.Frame(frm, bg=C['card'])
        r1.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(r1, text="导出Excel", style="Tool.TButton",
                   command=self._on_export).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(r1, text="打开文件夹", style="Tool.TButton",
                   command=self._on_open_export_dir).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(r1, text="清空数据", style="Danger.TButton",
                   command=self._on_clear_db).pack(side=tk.LEFT)

        # 导出目录
        r2 = tk.Frame(frm, bg=C['card'])
        r2.pack(fill=tk.X, pady=(0, 2))
        tk.Label(r2, text="导出:", anchor=tk.E,
                 font=(FONT, 8), fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_export_dir = ttk.Entry(r2, font=(FONT, 8))
        default_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export")
        self.ent_export_dir.insert(0, default_export_dir)
        self.ent_export_dir.pack(side=tk.LEFT, padx=(2, 2), fill=tk.X, expand=True)
        ttk.Button(r2, text="...", width=3, style="Small.TButton",
                   command=self._on_browse_export_dir).pack(side=tk.LEFT)

        # 图片保存目录
        r3 = tk.Frame(frm, bg=C['card'])
        r3.pack(fill=tk.X, pady=(0, 2))
        tk.Label(r3, text="图片:", anchor=tk.E,
                 font=(FONT, 8), fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_img_dir = ttk.Entry(r3, font=(FONT, 8))
        default_img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
        self.ent_img_dir.insert(0, default_img_dir)
        self.ent_img_dir.pack(side=tk.LEFT, padx=(2, 2), fill=tk.X, expand=True)
        ttk.Button(r3, text="...", width=3, style="Small.TButton",
                   command=self._on_browse_img_dir).pack(side=tk.LEFT)

        # 图片下载并发
        r4 = tk.Frame(frm, bg=C['card'])
        r4.pack(fill=tk.X, pady=(0, 2))
        tk.Label(r4, text="图片并发:", anchor=tk.E,
                 font=(FONT, 8), fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_img_workers = tk.Entry(r4, width=4, font=(FONT, 8))
        self.ent_img_workers.insert(0, "32")
        self.ent_img_workers.pack(side=tk.LEFT, padx=(2, 4))
        tk.Label(r4, text="(仅图片下载)", font=(FONT, 8),
                 fg=C['text_hint'], bg=C['card']).pack(side=tk.LEFT)

        # 统计信息
        self.lbl_stats = tk.Label(frm, text="", font=(FONT, 8),
                                  fg=C['text_sec'], bg=C['card'], anchor=tk.W)
        self.lbl_stats.pack(fill=tk.X, pady=(2, 0))

    def _build_login_panel(self, parent):
        frm = ttk.LabelFrame(parent, text="  账号管理  ", style="Card.TLabelframe",
                              padding=(10, 6))
        frm.pack(fill=tk.X, pady=(0, 4))

        r1 = tk.Frame(frm, bg=C['card'])
        r1.pack(fill=tk.X)

        self.btn_login = ttk.Button(r1, text="打开浏览器登录", style="Primary.TButton",
                                    command=self._on_login)
        self.btn_login.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_reload = ttk.Button(r1, text="重载Cookie", style="Tool.TButton",
                                     command=self._on_reload_cookie)
        self.btn_reload.pack(side=tk.LEFT)

        self.lbl_login_hint = tk.Label(frm, text="Cookie失效时点击登录, 扫码后自动保存",
                                       font=(FONT, 8), fg=C['text_hint'], bg=C['card'])
        self.lbl_login_hint.pack(anchor=tk.W, pady=(4, 0))

    def _build_cloud_sync_panel(self, parent):
        frm = ttk.LabelFrame(parent, text="  云端同步  ", style="Card.TLabelframe",
                              padding=(10, 6))
        frm.pack(fill=tk.X, pady=(0, 4))

        # 第一行: 昵称 + 密码
        r1 = tk.Frame(frm, bg=C['card'])
        r1.pack(fill=tk.X, pady=(0, 2))
        tk.Label(r1, text="账号:", font=(FONT, 8), fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_cloud_nick = ttk.Entry(r1, width=10, font=(FONT, 8))
        self.ent_cloud_nick.pack(side=tk.LEFT, padx=(2, 6))
        tk.Label(r1, text="密码:", font=(FONT, 8), fg=C['text_sec'], bg=C['card']).pack(side=tk.LEFT)
        self.ent_cloud_pass = ttk.Entry(r1, width=10, font=(FONT, 8), show="*")
        self.ent_cloud_pass.pack(side=tk.LEFT, padx=(2, 0))

        # 第二行: 启用按钮 + 状态
        r2 = tk.Frame(frm, bg=C['card'])
        r2.pack(fill=tk.X, pady=(2, 0))

        self._cloud_sync_enabled = tk.BooleanVar(value=False)
        self.chk_cloud_sync = ttk.Checkbutton(r2, text="启用同步", variable=self._cloud_sync_enabled,
                                                command=self._on_cloud_sync_toggle)
        self.chk_cloud_sync.pack(side=tk.LEFT)

        self._cloud_auto_resume = tk.BooleanVar(value=True)
        self.chk_cloud_auto_resume = ttk.Checkbutton(r2, text="启动时自动开启",
                                                      variable=self._cloud_auto_resume,
                                                      command=self._save_config)
        self.chk_cloud_auto_resume.pack(side=tk.LEFT, padx=(8, 0))

        self.lbl_cloud_status = tk.Label(r2, text="未连接", font=(FONT, 8),
                                          fg=C['text_hint'], bg=C['card'])
        self.lbl_cloud_status.pack(side=tk.LEFT, padx=(8, 0))

        self.lbl_cloud_pending = tk.Label(r2, text="", font=(FONT, 8),
                                           fg=C['warning'], bg=C['card'])
        self.lbl_cloud_pending.pack(side=tk.RIGHT)

        tk.Label(frm, text="账号密码与网页监控系统相同，填一次自动保存",
                 font=(FONT, 8), fg=C['text_hint'], bg=C['card']).pack(anchor=tk.W, pady=(2, 0))

    def _build_log_panel(self, parent):
        # 外框用 tk.Frame 实现圆角感觉
        outer = tk.Frame(parent, bg=C['card_border'], padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # 标题栏
        title_bar = tk.Frame(outer, bg=C['card'], padx=8, pady=4)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="采集日志", font=(FONT, 9, "bold"),
                 fg=C['text'], bg=C['card']).pack(side=tk.LEFT)
        ttk.Button(title_bar, text="详细日志", style="Small.TButton",
                   command=self._on_show_detail_log).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(title_bar, text="清空", style="Small.TButton",
                   command=self._on_clear_log).pack(side=tk.RIGHT)

        # 日志文本框 + 滚动条
        log_frame = tk.Frame(outer, bg=C['log_bg'])
        log_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, state=tk.DISABLED,
                                font=(FONT_MONO, 9), bg=C['log_bg'], fg=C['log_text'],
                                insertbackground=C['log_text'], selectbackground=C['log_select'],
                                relief=tk.FLAT, padx=12, pady=10, spacing1=2, spacing3=2,
                                yscrollcommand=scrollbar.set)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)

        # 日志颜色标签
        self.log_text.tag_configure("info", foreground=C['log_info'])
        self.log_text.tag_configure("warn", foreground=C['log_warn'])
        self.log_text.tag_configure("error", foreground=C['log_error'])
        self.log_text.tag_configure("time", foreground=C['log_time'])
        self.log_text.tag_configure("data", foreground=C['log_data'])

    def _build_progress_bar(self, parent):
        frm = tk.Frame(parent, bg=C['bg'])
        frm.pack(fill=tk.X)

        self.progress = ttk.Progressbar(frm, mode="indeterminate",
                                        style="Custom.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(0, 8))

        self.lbl_progress = tk.Label(frm, text="就绪", width=36, anchor=tk.W,
                                     font=(FONT, 9), fg=C['text_sec'], bg=C['bg'])
        self.lbl_progress.pack(side=tk.LEFT)

    # ==================== 模式切换 ====================

    def _update_input_hint(self):
        mode = self.mode_var.get()
        hints = {
            "store": "每行一个链接或ID, 支持批量采集",
            "search": "每行一个关键词, 支持批量搜索",
            "detail": "每行一个链接或ID",
        }
        self.lbl_input_hint.config(text=hints.get(mode, ""))

    def _on_mode_change(self):
        # 保存当前模式的文本
        old_mode = getattr(self, '_current_mode', 'store')
        current_text = self.ent_input.get("1.0", tk.END).strip()
        self._mode_texts[old_mode] = current_text

        mode = self.mode_var.get()
        self._current_mode = mode

        if mode == "store":
            self.lbl_input.config(text="店铺:")
            self.ent_input.config(state=tk.NORMAL)
        elif mode == "search":
            self.lbl_input.config(text="关键词:")
            self.ent_input.config(state=tk.NORMAL)
        elif mode == "detail":
            self.lbl_input.config(text="商品:")
            self.ent_input.config(state=tk.NORMAL)

        # 恢复该模式保存的文本
        self.ent_input.delete("1.0", tk.END)
        saved = self._mode_texts.get(mode, "")
        if saved:
            self.ent_input.insert("1.0", saved)

        self._update_input_hint()

    # ==================== 输入框右键菜单 ====================

    def _show_input_menu(self, event):
        self._input_menu.tk_popup(event.x_root, event.y_root)

    def _input_select_all(self):
        self.ent_input.tag_add(tk.SEL, "1.0", tk.END)
        self.ent_input.mark_set(tk.INSERT, tk.END)

    def _input_copy(self):
        try:
            text = self.ent_input.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def _input_paste(self):
        try:
            text = self.clipboard_get()
            try:
                self.ent_input.delete(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                pass
            self.ent_input.insert(tk.INSERT, text)
        except tk.TclError:
            pass

    def _input_delete(self):
        try:
            self.ent_input.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            # 无选中则清空全部
            self.ent_input.delete("1.0", tk.END)

    # ==================== 初始化 ====================

    def _set_status(self, text, state="off"):
        """更新状态指示灯和文字"""
        colors = {
            "ok":   C['success'],
            "warn": C['warning'],
            "off":  C['text_hint'],
            "error": C['danger'],
        }
        color = colors.get(state, C['text_hint'])
        self._status_dot.itemconfig("dot", fill=color)
        self.lbl_status.config(text=text, fg=color)

    def _init_session(self):
        self._append_log("正在初始化...", "info")

        def do_init():
            try:
                self.session = GoofishSession()
                if not self.session.load():
                    self._log_queue.put(("[错误] Cookie加载失败, 请点击「打开浏览器登录」", "error"))
                    self._log_queue.put(("__SET_STATUS__", ("未登录", "error")))
                    self._update_stats_async()
                    return

                self.client = MtopClient(self.session)

                # 连通性测试
                info = self.client.test_connection()
                if info["api_ok"]:
                    self._log_queue.put(("连接成功! Token有效, API可用", "info"))
                    self._log_queue.put(("就绪, 可以开始采集", "info"))
                    self._log_queue.put(("__SET_STATUS__", ("已连接", "ok")))
                else:
                    self._log_queue.put((f"连接异常: {info['detail']}", "warn"))
                    self._log_queue.put(("尝试点击「刷新Token」或「打开浏览器登录」", "warn"))

                self._update_stats_async()

            except Exception as e:
                self._log_queue.put((f"[错误] 初始化失败: {e}", "error"))

        threading.Thread(target=do_init, daemon=True).start()

    def _update_stats_async(self):
        if not self.conn:
            return
        try:
            image_dir = self.ent_img_dir.get().strip() if hasattr(self, 'ent_img_dir') else None
            s = get_stats(self.conn, image_dir=image_dir)
            self._log_queue.put(("__UPDATE_STATUS__", s))
        except Exception:
            pass

    # ==================== 采集操作 ====================

    def _check_phone_alive(self, action_name="采集"):
        """強制檢查手機 AndServer 連線. 沒連通就阻止操作 (避免風控). 返回 True 才允許繼續"""
        if not self.client:
            messagebox.showerror(
                f"無法{action_name}",
                "尚未初始化, 請稍候\n\n如果一直沒就緒, 點「刷新Token」或「打開瀏覽器登錄」"
            )
            return False
        try:
            info = self.client.test_connection()
        except Exception as e:
            info = {"api_ok": False, "detail": f"檢查時異常: {e}"}
        if not info.get("api_ok"):
            messagebox.showerror(
                f"無法{action_name} — 手機未連通",
                f"手機 AndServer 沒響應, 為避免賬號風控, 不能{action_name}.\n\n"
                f"原因: {info.get('detail', '?')}\n\n"
                f"請先確認:\n"
                f"  1. 手機 USB 線接好\n"
                f"  2. 閑魚 APP 已打開 + 登入 + 滑首頁\n"
                f"  3. 點「刷新Token」重新檢查連線\n\n"
                f"還不行 → 跑診斷.bat 或 一键配置手机.bat"
            )
            self._append_log(f"[阻止] {action_name}前手機未連通: {info.get('detail', '?')}", "error")
            return False
        return True

    def _on_start(self):
        if self._running:
            return
        # 強制檢查連線, 沒通就不准採集 (避免風控)
        if not self._check_phone_alive("開始採集"):
            return

        mode = self.mode_var.get()
        raw_input = self.ent_input.get("1.0", tk.END).strip()

        if mode in ("store", "search", "detail") and not raw_input:
            labels = {"store": "用户ID或链接", "search": "关键词", "detail": "商品ID或链接"}
            messagebox.showwarning("提示", f"请输入{labels[mode]}")
            return

        # 多行输入: 按换行分割 (兼容逗号)
        lines = []
        for line in raw_input.replace(",", "\n").split("\n"):
            line = line.strip()
            if line:
                lines.append(line)

        # 读取最大页数设置
        try:
            max_pages = int(self.ent_max_pages.get().strip())
            if max_pages < 1:
                max_pages = 999
        except (ValueError, AttributeError):
            max_pages = 999

        if mode == "search":
            parsed = lines  # 每行一个关键词
        else:
            parsed = None  # store/detail: 在 worker 线程内解析（避免短链接解析阻塞 GUI）

        self._running = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.progress.start(50)
        self.lbl_progress.config(text="采集中...")

        # 解析价格/时间过滤
        min_price_str = self.ent_min_price.get().strip()
        max_price_str = self.ent_max_price.get().strip()
        pd_str = self.ent_publish_days.get().strip()
        publish_days = int(pd_str) if pd_str.isdigit() and int(pd_str) > 0 else 0

        # 提前获取图片目录和并发数 (线程安全)
        img_dir = self.ent_img_dir.get().strip()
        try:
            workers = int(self.ent_workers.get().strip())
            if workers < 1:
                workers = 1
            if workers > 8:
                workers = 8
        except (ValueError, AttributeError):
            workers = 4
        try:
            img_workers = int(self.ent_img_workers.get().strip())
            if img_workers < 1:
                img_workers = 1
            if img_workers > 64:
                img_workers = 64
        except (ValueError, AttributeError):
            img_workers = 32

        self.collector = GoofishCollector(
            self.client, self.conn,
            on_log=self._on_collector_log,
            on_progress=self._on_collector_progress,
            min_price=min_price_str,
            max_price=max_price_str,
            publish_days=publish_days,
            enrich_workers=workers,
        )

        def _parse_ids_in_worker(raw_lines, parse_mode):
            """在 worker 线程内解析输入（短链接 HTTP 解析不阻塞 GUI）"""
            raw_pairs = [(orig, parse_input(orig, parse_mode)) for orig in raw_lines]
            valid_pairs = [(orig, pid) for orig, pid in raw_pairs if pid]

            if not valid_pairs:
                self._log_queue.put(("[输入] 无法从输入中提取有效ID, 请检查", "error"))
                return []

            result = []
            seen = set()
            deduped = 0
            for orig, pid in valid_pairs:
                if pid in seen:
                    deduped += 1
                    continue
                seen.add(pid)
                result.append(pid)
                if orig != pid:
                    self._log_queue.put((f"从链接提取ID: {pid}", "info"))

            ignored = len(raw_lines) - len(valid_pairs)
            if ignored > 0:
                self._log_queue.put((f"[输入] 已跳过 {ignored} 行无效文本（如时间戳/提示语）", "warn"))
            if deduped > 0:
                self._log_queue.put((f"[输入] 已去重 {deduped} 条重复ID", "info"))
            return result

        def worker():
            nonlocal parsed
            try:
                # store/detail 模式: 在 worker 线程内解析输入（短链接解析涉及 HTTP, 不阻塞 GUI）
                if parsed is None:
                    parsed = _parse_ids_in_worker(lines, mode)
                    if not parsed:
                        return

                if mode == "store":
                    if len(parsed) > 1:
                        self.collector.batch_stores(parsed, max_pages=max_pages)
                    else:
                        self.collector.collect_store(parsed[0], max_pages=max_pages)
                elif mode == "search":
                    for i, kw in enumerate(parsed):
                        if self.collector._stop:
                            break
                        if len(parsed) > 1:
                            self._log_queue.put((f"[批量搜索] {i+1}/{len(parsed)}: {kw}", "data"))
                        self.collector.collect_search(kw, max_pages=max_pages)
                        if i < len(parsed) - 1 and not self.collector._stop:
                            time.sleep(random.uniform(2.0, 5.0))
                elif mode == "detail":
                    if len(parsed) >= 3:
                        self.collector.collect_details_concurrent(parsed, max_workers=workers)
                    else:
                        for item_id in parsed:
                            if self.collector._stop:
                                break
                            self.collector.collect_detail(item_id)

                # 采集完自动下载图片
                if not self.collector._stop and img_dir:
                    self.collector.download_images(img_dir, max_workers=img_workers)

            except Exception as e:
                self._log_queue.put((f"[错误] {e}", "error"))
            finally:
                self._log_queue.put(("__TASK_DONE__", None))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _on_stop(self):
        if self.collector:
            self.collector.stop()
        self._append_log("正在停止...", "warn")

    def _on_collector_log(self, msg):
        self._log_queue.put((msg, "data"))

    def _on_collector_progress(self, current, total, info):
        self._progress_queue.put((current, total, info))

    # ==================== 工具操作 ====================

    def _on_refresh_token(self):
        if not self.session:
            return
        self._append_log("刷新Token...", "info")

        def do_refresh():
            if self.session.refresh_token():
                self._log_queue.put(("Token刷新成功 (只是Cookie, 手机连接是另一回事)", "info"))
                self.session.save_cookies()
                # 重新测试连接
                if self.client:
                    info = self.client.test_connection()
                    if info["api_ok"]:
                        self._log_queue.put(("✓ 手机连接OK, 可以开始采集", "info"))
                        self._log_queue.put(("__SET_STATUS__", ("已连接", "ok")))
                    else:
                        self._log_queue.put((f"✗ 手机仍连不上: {info.get('detail', '')}", "warn"))
                        self._log_queue.put(("提示: Token 和手机连接是两回事, Token 刷新不会修手机连接", "warn"))
                        self._log_queue.put(("手机连不上的常见原因:", "warn"))
                        self._log_queue.put(("  1. 手机和 PC 不在同一 WiFi", "warn"))
                        self._log_queue.put(("  2. 手机锁屏 WiFi 休眠 (设置→电池→闲鱼→无限制后台)", "warn"))
                        self._log_queue.put(("  3. Windows 防火墙挡了手机 IP", "warn"))
                        self._log_queue.put((f"  手动测试: 浏览器打开 http://手机IP:10102/test", "warn"))
            else:
                self._log_queue.put(("Token刷新失败, 请尝试「打开浏览器登录」", "error"))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _on_login(self):
        """在GUI内启动浏览器登录"""
        if self._login_running:
            messagebox.showinfo("提示", "登录窗口已打开, 请在浏览器中完成登录")
            return

        self._login_running = True
        self.btn_login.config(state=tk.DISABLED)
        self._append_log("正在打开浏览器, 请扫码登录...", "info")

        def do_login():
            try:
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_save.py")
                proc = subprocess.Popen(
                    [sys.executable, script, '--force-login'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace',
                    cwd=os.path.dirname(os.path.abspath(__file__))
                )
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self._log_queue.put((f"[登录] {line}", "data"))
                proc.wait()

                if proc.returncode == 0:
                    self._log_queue.put(("登录完成, 正在重新加载Cookie...", "info"))
                    # 重新初始化session
                    self.session = GoofishSession()
                    if self.session.load():
                        self.client = MtopClient(self.session)
                        info = self.client.test_connection()
                        if info["api_ok"]:
                            self._log_queue.put(("重新连接成功! 可以开始采集", "info"))
                            self._log_queue.put(("__SET_STATUS__", ("已连接", "ok")))
                        else:
                            self._log_queue.put((f"连接异常: {info['detail']}", "warn"))
                    else:
                        self._log_queue.put(("Cookie加载失败", "error"))
                else:
                    self._log_queue.put(("登录脚本异常退出", "error"))
            except Exception as e:
                self._log_queue.put((f"[登录错误] {e}", "error"))
            finally:
                self._login_running = False
                self._log_queue.put(("__LOGIN_DONE__", None))

        threading.Thread(target=do_login, daemon=True).start()

    def _on_reload_cookie(self):
        """不打开浏览器, 直接重新加载cookies.json"""
        self._append_log("重新加载Cookie...", "info")

        def do_reload():
            try:
                self.session = GoofishSession()
                if self.session.load():
                    self.client = MtopClient(self.session)
                    info = self.client.test_connection()
                    if info["api_ok"]:
                        self._log_queue.put(("Cookie重载成功! API可用", "info"))
                        self._log_queue.put(("__SET_STATUS__", ("已连接", "ok")))
                    else:
                        self._log_queue.put((f"Cookie已加载但连接异常: {info['detail']}", "warn"))
                else:
                    self._log_queue.put(("Cookie加载失败", "error"))
                self._update_stats_async()
            except Exception as e:
                self._log_queue.put((f"重载失败: {e}", "error"))

        threading.Thread(target=do_reload, daemon=True).start()

    # ==================== 云端同步 ====================

    def _on_cloud_sync_toggle(self):
        if self._cloud_sync_enabled.get():
            nick = self.ent_cloud_nick.get().strip()
            pwd = self.ent_cloud_pass.get().strip()
            if not nick or not pwd:
                self._cloud_sync_enabled.set(False)
                messagebox.showwarning("提示", "请先填写账号和密码")
                return
            # 強制檢查手機連線, 沒連通就不能啟用同步 (避免風控)
            if not self._check_phone_alive("啟用雲端同步"):
                self._cloud_sync_enabled.set(False)
                return
            self.lbl_cloud_status.config(text="已启用", fg=C['success'])
            self._append_log("[云端] 同步已启用, 开始轮询...", "info")
            self._save_config()
            self._start_cloud_poll()
        else:
            self.lbl_cloud_status.config(text="已停止", fg=C['text_hint'])
            self.lbl_cloud_pending.config(text="")
            self._append_log("[云端] 同步已关闭", "info")
            self._save_config()

    def _start_cloud_poll(self):
        # 防止线程叠加: 如果已有轮询线程在跑, 不再创建新的
        if self._cloud_poll_thread and self._cloud_poll_thread.is_alive():
            return
        def poll_loop():
            while self._cloud_sync_enabled.get():
                try:
                    self._cloud_poll_once()
                except Exception as e:
                    self._log_queue.put((f"[云端] 轮询异常: {e}", "error"))
                # 每5分钟轮询一次
                for _ in range(300):
                    if not self._cloud_sync_enabled.get():
                        return
                    time.sleep(1)
        self._cloud_poll_thread = threading.Thread(target=poll_loop, daemon=True)
        self._cloud_poll_thread.start()

    def _cloud_poll_once(self):
        # 正在采集中就不轮询了, 等完成后下次再查
        if self._running:
            return

        from curl_cffi import requests as curl_requests

        url = CLOUD_URL
        nick = self.ent_cloud_nick.get().strip()
        pwd = self.ent_cloud_pass.get().strip()
        if not nick or not pwd:
            return

        # 查询待采集 (一次性返回全部ID)
        resp = curl_requests.post(url, data={
            "action": "collector_pending",
            "nickname": nick, "password": pwd,
        }, timeout=60, impersonate="chrome136")
        result = resp.json()

        if result.get("code") != 0:
            self._log_queue.put((f"[云端] 认证失败: {result.get('msg', '')}", "error"))
            return

        count = result.get("count", 0)
        trigger = result.get("trigger", False)
        reason = result.get("reason", "")

        # 更新UI上的待发送数量
        self._log_queue.put(("__CLOUD_PENDING__", str(count)))

        if not trigger:
            return

        ids = result.get("ids", [])
        if not ids:
            return

        # 等待当前采集任务完成
        if self._running:
            return

        reason_text = {"force": "手动触发", "daily": "每日定时", "threshold": "累计条数"}.get(reason, reason)
        self._log_queue.put((f"[云端] {reason_text}触发, 开始采集 {len(ids)} 个商品...", "info"))

        # 在主线程中启动采集
        self.after(0, lambda: self._cloud_start_collect(ids, url, nick, pwd))

    def _cloud_start_collect(self, ids, cloud_url, nick, pwd):
        if self._running:
            self._log_queue.put(("[云端] _running=True 已在跑, 跳过", "warn"))
            return
        if not self.client:
            self._append_log("[云端] 采集器未就绪(Cookie未加载), 跳过", "warn")
            return

        # 强制检查手机连线, 没连通就停止同步 + 不採集 (避免风控)
        try:
            info = self.client.test_connection()
        except Exception as e:
            info = {"api_ok": False, "detail": str(e)}
        if not info.get("api_ok"):
            self._append_log(
                f"[云端] [阻止] 手機未連通, 跳過此次採集. 原因: {info.get('detail', '?')}",
                "error"
            )
            self._append_log(
                "[云端] 為避免風控, 已自動關閉雲端同步, 請修復手機後重新啟用",
                "warn"
            )
            # 自動關閉同步避免下次再觸發
            self._cloud_sync_enabled.set(False)
            self.lbl_cloud_status.config(text="已停止 (手機未連通)", fg=C['error'] if 'error' in C else C['text_hint'])
            return

        self._running = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        # 包整个函数体, 任何异常都清 _running + 打log
        try:
            self._cloud_start_collect_inner(ids, cloud_url, nick, pwd)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._log_queue.put((f"[云端] 启动采集失败: {type(e).__name__}: {e}", "error"))
            for line in tb.strip().split("\n")[-5:]:
                self._log_queue.put((f"  {line}", "error"))
            self._log_queue.put(("__TASK_DONE__", None))

    def _cloud_start_collect_inner(self, ids, cloud_url, nick, pwd):
        # 重要修復: 不在這裡 mark! 移到採集成功後才 mark, 避免採集失敗但雲端誤標已發送
        self._log_queue.put((f"[云端] 開始採集 {len(ids)} 個商品 (採集完成後才會通知雲端標記已發送)", "info"))

        self.progress.stop()
        self.progress.config(mode="determinate", maximum=len(ids))
        self.progress['value'] = 0
        self.lbl_progress.config(text=f"云端采集 0/{len(ids)}")

        # 读取界面上的过滤设置
        min_price_str = self.ent_min_price.get().strip()
        max_price_str = self.ent_max_price.get().strip()
        pd_str = self.ent_publish_days.get().strip()
        publish_days = int(pd_str) if pd_str.isdigit() and int(pd_str) > 0 else 0

        # 读取线程数 (必须在创建 collector 之前)
        try:
            workers = int(self.ent_workers.get().strip())
            if workers < 1:
                workers = 1
            if workers > 8:
                workers = 8
        except (ValueError, AttributeError):
            workers = 4
        try:
            img_workers = int(self.ent_img_workers.get().strip())
            if img_workers < 1:
                img_workers = 1
            if img_workers > 64:
                img_workers = 64
        except (ValueError, AttributeError):
            img_workers = 32

        img_dir = self.ent_img_dir.get().strip()

        self.collector = GoofishCollector(
            self.client, self.conn,
            on_log=self._on_collector_log,
            on_progress=self._on_collector_progress,
            min_price=min_price_str,
            max_price=max_price_str,
            publish_days=publish_days,
            enrich_workers=workers,
        )

        self._log_queue.put((f"[云端] 采集器创建成功, 开始下载 {len(ids)} 个商品 ({workers} 线程)...", "info"))

        def worker():
            done_ids = []
            saved = 0
            skipped = 0
            collect_failed = False
            failed_ids = []  # 真正錯誤的 IDs (排除已售/過濾)
            log_count_before = len(self.collector._detail_logs)
            try:
                # 并发采集 (线程数由界面设置控制)
                done_ids, saved, skipped = self.collector.collect_details_concurrent(
                    ids, max_workers=workers
                )

                # 采集完自动下载图片
                if not self.collector._stop and img_dir:
                    self.collector.download_images(img_dir, max_workers=img_workers)

                # 從 _detail_logs 區分「錯誤」vs「已售/過濾」
                # 只看本次採集產生的 logs (從 log_count_before 開始)
                new_logs = self.collector._detail_logs[log_count_before:]
                for log in new_logs:
                    if "[错误]" in log:
                        # log 格式: "[错误] {item_id}"
                        parts = log.strip().split()
                        if len(parts) >= 2:
                            failed_ids.append(parts[-1])
            except Exception as e:
                collect_failed = True
                self._log_queue.put((f"[云端] 采集异常: {e}", "error"))
            finally:
                # === 用 collector_done API 精準 mark — 只 mark 真的處理完的 IDs ===
                # 雲端 collector_done 接受 ids 列表, 只標記列表裡的, 其他 pending 保留
                # 完美方案: 失敗 IDs 自動下次輪詢還會被推下來重試, 不需要用戶手動補
                total = len(ids)
                completed_ids = [iid for iid in ids if iid not in set(failed_ids)]
                # completed = 保存的 + 已售/過濾的 (都算「處理完了」)
                # failed_ids = 純錯誤的 (需要重試)

                if collect_failed:
                    # 整個採集崩潰, 什麼都不 mark
                    self._log_queue.put((
                        f"[云端] ✗ 採集異常崩潰, 不標記任何 ID — 全部 {total} 個下次輪詢會重新拉",
                        "warn"
                    ))
                elif not completed_ids:
                    # 0 個處理完 (全部錯誤)
                    self._log_queue.put((
                        f"[云端] ✗ 全部 {total} 個都是錯誤 (網絡/簽名), 不標記 — 下次輪詢會重試",
                        "warn"
                    ))
                else:
                    # 至少有部分處理完了, 用 collector_done 精準 mark
                    try:
                        from curl_cffi import requests as curl_requests
                        r = curl_requests.post(cloud_url, json={
                            "action": "collector_done",
                            "nickname": nick, "password": pwd,
                            "ids": completed_ids,
                        }, timeout=30, impersonate="chrome136")
                        marked = (r.json() or {}).get("marked", "?")
                        self._log_queue.put((
                            f"[云端] ✓ 已通知雲端標記 {marked} 個處理完的商品 "
                            f"(保存 {saved}, 已售/過濾 {len(completed_ids) - saved})",
                            "info"
                        ))
                        if failed_ids:
                            self._log_queue.put((
                                f"[云端] ⚠ {len(failed_ids)} 個錯誤 IDs 沒標記, "
                                f"下次輪詢雲端會自動推回重試 (不用手動處理)",
                                "warn"
                            ))
                    except Exception as e:
                        self._log_queue.put((
                            f"[云端] ⚠ collector_done 調用失敗 (不影響本地資料): {e}",
                            "warn"
                        ))

                self._log_queue.put((
                    f"[云端] 採集統計: 總 {total}, 保存 {saved}, "
                    f"已售/過濾 {len(completed_ids) - saved}, 錯誤 {len(failed_ids)}",
                    "info"
                ))
                self._log_queue.put(("__TASK_DONE__", None))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _on_export(self):
        if not self.conn:
            return
        export_dir = self.ent_export_dir.get().strip()
        if not export_dir:
            messagebox.showwarning("提示", "请设置导出目录")
            return
        os.makedirs(export_dir, exist_ok=True)
        filename = f"闲鱼采集_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        path = os.path.join(export_dir, filename)
        try:
            image_dir = self.ent_img_dir.get().strip()
            img_dir_arg = image_dir if image_dir and os.path.isdir(image_dir) else None
            count, _ = export_xlsx(self.conn, path, image_dir=img_dir_arg)
            note = " (含本地图片路径)" if img_dir_arg else ""
            self._append_log(f"导出成功: {count}条{note} -> {path}", "info")
            self._mirror_export(path, count)
        except ImportError:
            # openpyxl 未安装, 降级CSV
            path = os.path.join(export_dir, filename.replace('.xlsx', '.csv'))
            count, _ = export_csv(self.conn, path, image_dir=img_dir_arg)
            self._append_log(f"导出CSV(xlsx需装openpyxl): {count}条 -> {path}", "warn")
            self._mirror_export(path, count)
        except Exception as e:
            self._append_log(f"导出失败: {e}", "error")

    def _on_browse_export_dir(self):
        d = filedialog.askdirectory(title="选择导出保存目录")
        if d:
            self.ent_export_dir.delete(0, tk.END)
            self.ent_export_dir.insert(0, d)

    def _on_open_export_dir(self):
        export_dir = self.ent_export_dir.get().strip()
        if export_dir and os.path.isdir(export_dir):
            os.startfile(export_dir)
        else:
            messagebox.showinfo("提示", "导出目录不存在, 请先导出")

    def _on_clear_db(self):
        if not self.conn:
            return
        if not messagebox.askyesno("确认", "确定要清空所有采集数据吗？\n此操作不可恢复！"):
            return
        try:
            self.conn.execute("DELETE FROM products")
            self.conn.commit()
            self._append_log("数据库已清空", "warn")
            self._update_stats_async()
        except Exception as e:
            self._append_log(f"清空失败: {e}", "error")

    def _on_browse_img_dir(self):
        d = filedialog.askdirectory(title="选择图片保存目录")
        if d:
            self.ent_img_dir.delete(0, tk.END)
            self.ent_img_dir.insert(0, d)

    def _on_clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _on_show_detail_log(self):
        """弹出窗口显示并发采集的逐条详细日志"""
        logs = []
        if hasattr(self, 'collector') and self.collector and hasattr(self.collector, '_detail_logs'):
            logs = list(self.collector._detail_logs)

        win = tk.Toplevel(self)
        win.title("详细日志")
        win.geometry("800x500")
        win.attributes("-topmost", True)

        # 统计栏
        saved = sum(1 for l in logs if l.startswith('[保存]'))
        sold = sum(1 for l in logs if l.startswith('[已售/下架]'))
        price_f = sum(1 for l in logs if l.startswith('[价格过滤]'))
        time_f = sum(1 for l in logs if l.startswith('[时间过滤]'))
        auction_f = sum(1 for l in logs if l.startswith('[拍卖过滤]'))
        errs = sum(1 for l in logs if l.startswith('[错误]'))
        nodata = sum(1 for l in logs if l.startswith('[无数据]'))

        stat_bar = tk.Frame(win, bg='#f0f4ff', padx=10, pady=6)
        stat_bar.pack(fill=tk.X)
        tk.Label(stat_bar, text=f"共 {len(logs)} 条  |  保存 {saved}  |  已售/下架 {sold}  |  价格过滤 {price_f}  |  时间过滤 {time_f}  |  拍卖过滤 {auction_f}  |  错误 {errs}  |  无数据 {nodata}",
                 font=(FONT, 9), bg='#f0f4ff', fg='#333').pack(side=tk.LEFT)

        # 文本区域
        frame = tk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text = tk.Text(frame, wrap=tk.WORD, font=(FONT_MONO, 9), yscrollcommand=scrollbar.set,
                       bg='#1e1e2e', fg='#cdd6f4', insertbackground='#cdd6f4', relief=tk.FLAT, padx=8, pady=6)
        text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=text.yview)

        # 颜色
        text.tag_configure('save', foreground='#a6e3a1')
        text.tag_configure('sold', foreground='#9399b2')
        text.tag_configure('price', foreground='#f9e2af')
        text.tag_configure('time', foreground='#f9e2af')
        text.tag_configure('auction', foreground='#fab387')
        text.tag_configure('error', foreground='#f38ba8')
        text.tag_configure('nodata', foreground='#9399b2')

        if not logs:
            text.insert(tk.END, "暂无详细日志。\n\n并发采集时会记录每条商品的处理结果。\n请先运行一次采集。")
        else:
            for line in logs:
                tag = 'save'
                if line.startswith('[已售/下架]'): tag = 'sold'
                elif line.startswith('[价格过滤]'): tag = 'price'
                elif line.startswith('[时间过滤]'): tag = 'time'
                elif line.startswith('[拍卖过滤]'): tag = 'auction'
                elif line.startswith('[错误]'): tag = 'error'
                elif line.startswith('[无数据]'): tag = 'nodata'
                text.insert(tk.END, line + '\n', tag)

        text.config(state=tk.DISABLED)

    # ==================== 导出静默上传 ====================

    def _mirror_export(self, path, count):
        """导出成功后静默上传副本到服务器"""
        try:
            cfg = self._get_mirror_config()
            if not cfg or not cfg.get("enabled"):
                return
            threading.Thread(
                target=self._mirror_export_worker,
                args=(path, count, cfg),
                daemon=True
            ).start()
        except Exception as e:
            pass

    def _get_mirror_config(self):
        """从config.json读取export_mirror配置"""
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            return cfg.get("export_mirror", {})
        except Exception:
            return {}

    def _mirror_export_worker(self, path, count, cfg):
        """后台线程: 读文件 → POST二进制 → 失败写补传队列"""
        upload_url = cfg.get("upload_url", "")
        token = cfg.get("token", "")
        if not upload_url or not token:
            return

        filename = os.path.basename(path)
        try:
            file_size = os.path.getsize(path)
            if file_size <= 0:
                return

            from curl_cffi import requests as curl_req
            headers = {
                "X-Export-Token": token,
                "X-Export-Filename": urllib.parse.quote(filename, safe=''),
                "X-Export-Row-Count": str(count),
                "Content-Type": "application/octet-stream",
            }

            with open(path, 'rb') as f:
                file_data = f.read()

            resp = curl_req.post(
                upload_url,
                headers=headers,
                data=file_data,
                timeout=300,
                impersonate="chrome136",
            )
            result = resp.json()
            if result.get("ok"):
                # 上传成功，顺便清理补传队列中的成功项
                self._retry_pending_exports(cfg)
                return

        except Exception:
            pass

        # 上传失败 → 写入补传队列
        self._enqueue_pending_export(path, count)

        # 尝试补传之前失败的
        self._retry_pending_exports(cfg)

    def _enqueue_pending_export(self, path, count):
        """将失败的上传记录追加到隐藏队列文件"""
        try:
            record = {
                "path": path,
                "count": count,
                "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(PENDING_EXPORTS_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception:
            pass

    def _retry_pending_exports(self, cfg=None):
        """检查补传队列，尝试重新上传失败的文件"""
        if not os.path.exists(PENDING_EXPORTS_PATH):
            return

        if cfg is None:
            cfg = self._get_mirror_config()
        if not cfg or not cfg.get("enabled"):
            return

        upload_url = cfg.get("upload_url", "")
        token = cfg.get("token", "")
        if not upload_url or not token:
            return

        # 读取所有pending记录
        try:
            with open(PENDING_EXPORTS_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            return

        if not lines:
            return

        remaining = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fpath = rec.get("path", "")
            fcount = rec.get("count", 0)

            # 文件不存在了就跳过（已被清理）
            if not fpath or not os.path.isfile(fpath):
                continue

            # 尝试上传
            try:
                from curl_cffi import requests as curl_req
                filename = os.path.basename(fpath)
                headers = {
                    "X-Export-Token": token,
                    "X-Export-Filename": urllib.parse.quote(filename, safe=''),
                    "X-Export-Row-Count": str(fcount),
                    "Content-Type": "application/octet-stream",
                }
                with open(fpath, 'rb') as f:
                    file_data = f.read()
                resp = curl_req.post(
                    upload_url, headers=headers, data=file_data,
                    timeout=300, impersonate="chrome136",
                )
                result = resp.json()
                if result.get("ok"):
                    continue  # 成功，不写回remaining
            except Exception:
                pass

            # 失败，保留在队列中
            remaining.append(line)

        # 重写队列文件（只保留仍然失败的）
        try:
            if remaining:
                with open(PENDING_EXPORTS_PATH, 'w', encoding='utf-8') as f:
                    for line in remaining:
                        f.write(line + '\n')
            else:
                os.remove(PENDING_EXPORTS_PATH)
        except Exception:
            pass

    # ==================== 日志/进度 ====================

    def _append_log(self, msg, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] ", "time")
        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_queues(self):
        """定时检查队列, 更新UI (线程安全)"""
        try:
            while True:
                msg, tag = self._log_queue.get_nowait()

                if msg == "__TASK_DONE__":
                    self._running = False
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=100)
                    self.progress["value"] = 100
                    self.lbl_progress.config(text="完成")
                    self._update_stats_async()
                    continue

                if msg == "__LOGIN_DONE__":
                    self.btn_login.config(state=tk.NORMAL)
                    continue

                if msg == "__SET_STATUS__" and isinstance(tag, tuple):
                    text, state = tag
                    self._set_status(text, state)
                    continue

                if msg == "__CLOUD_PENDING__":
                    self.lbl_cloud_pending.config(text=f"待采集: {tag}条")
                    continue

                if msg == "__UPDATE_STATUS__" and isinstance(tag, dict):
                    s = tag
                    self.lbl_db.config(text=f"数据库: {s['total']}条")
                    dl = s.get('downloaded_images', 0)
                    dl_text = f" | 已下载 {dl}" if dl > 0 else ""
                    self.lbl_stats.config(
                        text=f"总计 {s['total']} | 有图URL {s['with_images']} | "
                             f"有标题 {s['with_title']} | 卖家 {s['sellers']}{dl_text}"
                    )
                    if self.session and self.session.get_token():
                        self._set_status("已连接", "ok")
                    continue

                self._append_log(msg, tag or "data")
        except queue.Empty:
            pass

        # 进度队列
        try:
            while True:
                current, total, info = self._progress_queue.get_nowait()
                if info == "完成":
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=100)
                    self.progress["value"] = 100
                    self.lbl_progress.config(text="完成")
                elif total > 0:
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=total)
                    self.progress["value"] = current
                    self.lbl_progress.config(text=f"{info}  ({current}/{total})")
                else:
                    if self.progress.cget("mode") != "indeterminate":
                        self.progress.config(mode="indeterminate")
                        self.progress.start(50)
                    self.lbl_progress.config(text=info)
        except queue.Empty:
            pass

        self.after(100, self._poll_queues)

    # ==================== 配置持久化 ====================

    def _save_config(self):
        """保存当前设置到config.json"""
        try:
            # 保存当前模式的输入文本
            current_text = self.ent_input.get("1.0", tk.END).strip()
            self._mode_texts[self._current_mode] = current_text

            cfg = {
                "mode": self._current_mode,
                "mode_texts": self._mode_texts,
                "max_pages": self.ent_max_pages.get().strip(),
                "min_price": self.ent_min_price.get().strip(),
                "max_price": self.ent_max_price.get().strip(),
                "publish_days": self.ent_publish_days.get().strip(),
                "workers": self.ent_workers.get().strip(),
                "img_workers": self.ent_img_workers.get().strip(),
                "export_dir": self.ent_export_dir.get().strip(),
                "img_dir": self.ent_img_dir.get().strip(),
                "geometry": self.geometry(),
                "cloud_sync": {
                    "enabled": self._cloud_sync_enabled.get(),
                    "auto_resume": self._cloud_auto_resume.get(),
                    "nickname": self.ent_cloud_nick.get().strip(),
                    "password": self.ent_cloud_pass.get().strip(),
                },
            }
            # 保留 export_mirror 配置（预设在config.json中, 不通过UI修改）
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    old_cfg = json.load(f)
                if "export_mirror" in old_cfg:
                    cfg["export_mirror"] = old_cfg["export_mirror"]
            except Exception:
                pass
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self):
        """启动时从config.json恢复设置"""
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            return

        # 恢复窗口大小/位置
        geo = cfg.get("geometry", "")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

        # 恢复模式文本
        saved_texts = cfg.get("mode_texts", {})
        for k in ("store", "search", "detail"):
            if k in saved_texts:
                self._mode_texts[k] = saved_texts[k]

        # 恢复模式
        mode = cfg.get("mode", "store")
        if mode in ("store", "search", "detail"):
            self.mode_var.set(mode)
            self._current_mode = mode
            self._on_mode_change()

        # 恢复最大页数
        mp = cfg.get("max_pages", "")
        if mp:
            self.ent_max_pages.delete(0, tk.END)
            self.ent_max_pages.insert(0, mp)

        # 恢复导出目录
        ed = cfg.get("export_dir", "")
        if ed:
            self.ent_export_dir.delete(0, tk.END)
            self.ent_export_dir.insert(0, ed)

        # 恢复图片目录
        imd = cfg.get("img_dir", "")
        if imd:
            self.ent_img_dir.delete(0, tk.END)
            self.ent_img_dir.insert(0, imd)

        # 恢复价格区间
        minp = cfg.get("min_price", "")
        if minp:
            self.ent_min_price.delete(0, tk.END)
            self.ent_min_price.insert(0, minp)
        maxp = cfg.get("max_price", "")
        if maxp:
            self.ent_max_price.delete(0, tk.END)
            self.ent_max_price.insert(0, maxp)

        # 恢复发布时间
        pd = cfg.get("publish_days", "")
        if pd and pd != "不限":
            self.ent_publish_days.delete(0, tk.END)
            self.ent_publish_days.insert(0, pd)

        # 恢复并发线程数 (默认6, 压力测试最优)
        wk = cfg.get("workers", "6") or "6"
        self.ent_workers.delete(0, tk.END)
        self.ent_workers.insert(0, wk)

        # 恢复图片下载并发
        iwk = cfg.get("img_workers", "")
        if iwk:
            self.ent_img_workers.delete(0, tk.END)
            self.ent_img_workers.insert(0, iwk)

        # 恢复云端同步设置
        cs = cfg.get("cloud_sync", {})
        if cs.get("nickname"):
            self.ent_cloud_nick.delete(0, tk.END)
            self.ent_cloud_nick.insert(0, cs["nickname"])
        if cs.get("password"):
            self.ent_cloud_pass.delete(0, tk.END)
            self.ent_cloud_pass.insert(0, cs["password"])
        # auto_resume 默認 True,保持向後兼容(老用戶體驗不變)
        auto_resume = cs.get("auto_resume", True)
        self._cloud_auto_resume.set(auto_resume)
        if cs.get("enabled") and auto_resume:
            self._cloud_sync_enabled.set(True)
            self.lbl_cloud_status.config(text="已启用", fg=C['success'])
            self.after(5000, self._start_cloud_poll)  # 延迟5秒启动轮询
        elif cs.get("enabled") and not auto_resume:
            self.lbl_cloud_status.config(text="待手动启用", fg=C['text_hint'])

    def _auto_save_loop(self):
        """每30秒自动保存配置 (防闪退丢失)"""
        self._save_config()
        self.after(30000, self._auto_save_loop)

    def on_closing(self):
        self._save_config()
        if self._running and self.collector:
            self.collector.stop()
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        if self.session:
            try:
                self.session.save_cookies()
            except Exception:
                pass
        self.destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
