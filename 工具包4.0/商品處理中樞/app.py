# -*- coding: utf-8 -*-
"""
商品處理中樞 — GUI主程序
統一處理煤爐(Mercari)和鹹魚(Goofish)採集數據,
輸出符合Yahoo拍賣自動刊登格式的Excel
"""
import copy
import json
import os
import sys
import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, colorchooser
from pathlib import Path

BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

import pandas as pd
from pipeline import Pipeline, load_config, save_config

VERSION = "1.0"
FONT = 'Microsoft YaHei UI'


# ★ 4.0.47: 簡單 tooltip helper (Steps 改 2 列 grid 後 tip 文字放 tooltip 顯示)
def _attach_tooltip(widget, text: str):
    if not text:
        return
    tip = {'win': None}
    def show(event=None):
        if tip['win'] is not None:
            return
        try:
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            t = tk.Toplevel(widget)
            t.wm_overrideredirect(True)
            t.wm_geometry(f'+{x}+{y}')
            tk.Label(t, text=text, background='#FFFFE0', foreground='#333',
                     relief='solid', borderwidth=1,
                     font=(FONT, 9), padx=6, pady=3,
                     justify=tk.LEFT, wraplength=400).pack()
            tip['win'] = t
        except Exception:
            tip['win'] = None
    def hide(event=None):
        if tip['win'] is not None:
            try: tip['win'].destroy()
            except Exception: pass
            tip['win'] = None
    widget.bind('<Enter>', show)
    widget.bind('<Leave>', hide)
    widget.bind('<ButtonPress>', hide)

# 貴功能 (跑前要 quota 檢查 + 扣費), 沒額度時 UI 自動鎖死這些 checkbox
EXPENSIVE_STEP_KEYS = ('v65_stage_e', 'image_dewatermark', 'image_opt')


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"商品處理中樞 v{VERSION}")
        # ★ 4.0.47: 加大視窗, 一次顯示所有按鈕跟功能, 不再被截斷
        self.root.geometry("1320x920")
        self.root.minsize(1100, 830)

        # 載入配置
        self.config = load_config()
        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.pipeline = None
        self.last_result = None

        # 變量
        self.source_var = tk.StringVar(value=self.config.get('source_type', 'mercari'))
        self.file_var = tk.StringVar(value=self.config.get('last_input_file', ''))
        self.mode_var = tk.StringVar(value=self.config.get('process_mode', 'raw'))
        self.out_dir_var = tk.StringVar(value=self.config.get('output_dir', ''))
        self.out_name_var = tk.StringVar(value=self.config.get('output_name', 'test'))
        self.conflict_var = tk.StringVar(value=self.config.get('output_conflict', 'rename'))
        self.status_var = tk.StringVar(value='就緒')
        self.progress_var = tk.DoubleVar(value=0)

        # 步驟開關變量
        self.step_vars = {}
        for key, val in self.config.get('steps', {}).items():
            self.step_vars[key] = tk.BooleanVar(value=val.get('enabled', False))

        # 說明模板選擇
        self.template_var = tk.StringVar(
            value=self.config.get('desc_templates', {}).get('selected', ''))

        # ★ TG ID + 配額狀態
        self.tg_id_var = tk.StringVar(value=str(self.config.get('tg_id', '')))
        self.quota_status_var = tk.StringVar(value='配額: (查詢中)')

        # ★ 4.0.48: 三合一貴功能 var (合成 Stage E + 去水印 + 首圖優化, GUI 上只一個 checkbox)
        # 內部 step_vars 仍維持 3 個 key (config/pipeline 不變), 此 var 是 GUI derived
        # 初始化值: 任一啟用就 True (從 config 讀回的 3 個 var OR 在一起)
        _exp_initial = any(
            self.step_vars.get(k, tk.BooleanVar()).get() for k in EXPENSIVE_STEP_KEYS
        )
        self.expensive_var = tk.BooleanVar(value=_exp_initial)
        # 點此 var → 三個內部 var 同步
        def _sync_expensive(*args):
            val = self.expensive_var.get()
            for k in EXPENSIVE_STEP_KEYS:
                if k in self.step_vars:
                    self.step_vars[k].set(val)
        self.expensive_var.trace_add('write', _sync_expensive)

        self._build_ui()
        self._update_step_states()
        self._poll_log()
        # ★ 啟動後查一次配額狀態
        self.root.after(800, self._refresh_quota)

        # ★ closeEvent 攔截 (防誤關)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # ★ 啟動偵測 _checkpoint.json (上次中斷的 run)
        self.root.after(500, self._check_checkpoint_on_startup)
        # ★ 4.0.66 + 4.0.70: 啟動後 + 每 10 分鐘 check 雲端新版.
        #   start.bat 只在啟動時拉. 啟動後雲端推新版 → 不重啟用不到. 此 thread 在標題列提醒.
        #   4.0.70: 縮短啟動延遲 3s→500ms (用戶反饋沒看到提示, 可能截圖在 3s 前;
        #           另寫 debug 到 log_queue 讓 check 進度透明)
        self.root.after(500, self._check_new_version_async)

    def _check_checkpoint_on_startup(self):
        """啟動時偵測 output_dir/_checkpoint.json, 對話框問繼續/重新開始"""
        out_dir = self.out_dir_var.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            return
        try:
            from checkpoint_manager import CheckpointManager
            if not CheckpointManager.has_checkpoint(out_dir):
                return
            ckpt = CheckpointManager.load_existing(out_dir)
            sm = ckpt.summary()
            done = ', '.join(sm['completed_stages']) or '(無)'
            cur = sm['current_stage'] or '?'
            paused_info = ''
            if sm.get('paused'):
                paused_info = f"\n暫停原因: {sm.get('paused_reason', '?')}\n暫停時間: {sm.get('paused_at', '?')}"
            msg = (f"發現未完成的 run!\n\n"
                   f"已完成 stages: {done}\n"
                   f"中斷時 stage: {cur} (進度 {sm['done_in_current']} 件)\n"
                   f"上次存檔: {sm.get('last_save', '?')}{paused_info}\n\n"
                   f"按「是」= 從中斷點繼續 (推薦)\n"
                   f"按「否」= 重新開始 (刪除 checkpoint)")
            ans = messagebox.askyesnocancel("發現中斷的 run", msg)
            if ans is None:
                return  # 取消, 不動作
            if ans:
                # 繼續 → 顯示 resume 按鈕
                self.btn_resume.configure(state=tk.NORMAL)
                self.status_var.set(f'⏸ 上次中斷, 點「▶ 繼續中斷」恢復')
                self._log_msg(f"\n[Resume 待命] {msg}")
            else:
                # 重新開始 → 刪除 checkpoint
                ckpt.cleanup()
                self.status_var.set('已刪除舊 checkpoint, 可重新開始')
                self._log_msg(f"\n[Checkpoint 已刪除] 可開始新跑")
        except Exception as e:
            self._log_msg(f"[Checkpoint 偵測] 失敗: {e}")

    def _on_close(self):
        """關閉視窗攔截: 跑中/暫停中需確認"""
        running = self.worker_thread and self.worker_thread.is_alive()
        if running:
            ans = messagebox.askyesno(
                "正在處理中",
                "處理還沒結束, 確定關閉?\n\n"
                "跑中關閉會丟失最近 ~20 件進度 (從 last save 算)\n"
                "(已寫入 _checkpoint.* 的部分仍在, 下次可繼續)")
            if not ans:
                return
        elif self.btn_resume['state'] == 'normal':  # 暫停待 resume 中
            ans = messagebox.askyesno(
                "暫停中",
                "進度已寫入 _checkpoint.*, 關閉是安全的\n"
                "下次開啟可選「繼續」恢復\n\n確定關閉?")
            if not ans:
                return
        # 持久化最新 UI 狀態 (尤其是 tg_id, 否則下次開又要重填)
        try:
            self._sync_config_from_ui()
            save_config(self.config)
        except Exception:
            pass
        self.root.destroy()

    # ══════════════════ UI佈局 ══════════════════

    def _build_ui(self):
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ── 左側面板 (可滾動) — 解決小螢幕看不到 開始按鈕 的問題 ──
        left_container = ttk.Frame(main, width=440)
        main.add(left_container, weight=0)

        # Canvas + Scrollbar 包住實際內容
        left_canvas = tk.Canvas(left_container, borderwidth=0, highlightthickness=0,
                                background=self.root.cget('background'))
        left_vsb = ttk.Scrollbar(left_container, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_vsb.set)
        left_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 真正放控件的 Frame 嵌進 Canvas
        left = ttk.Frame(left_canvas)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")

        # 內 frame 高度變動 → 更新 scroll region
        def _on_left_config(e):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
        left.bind('<Configure>', _on_left_config)

        # canvas 寬度變動 → 內 frame 寬度跟上
        def _on_canvas_config(e):
            left_canvas.itemconfig(left_window, width=e.width)
        left_canvas.bind('<Configure>', _on_canvas_config)

        # 滑鼠滾輪 (只在 canvas 上時觸發, 不要劫整個 app)
        def _on_mousewheel(e):
            left_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        def _bind_wheel(e):
            left_canvas.bind_all('<MouseWheel>', _on_mousewheel)
        def _unbind_wheel(e):
            left_canvas.unbind_all('<MouseWheel>')
        left_canvas.bind('<Enter>', _bind_wheel)
        left_canvas.bind('<Leave>', _unbind_wheel)

        self._build_left(left)

        # ── 右側面板 ──
        right = ttk.Frame(main)
        main.add(right, weight=1)
        self._build_right(right)

    def _build_left(self, parent):
        # ─ 來源識別 (自動) ─
        src_frame = ttk.Frame(parent)
        src_frame.pack(fill=tk.X, pady=(0, 6))
        self._source_label = ttk.Label(src_frame, text="來源: 請選擇文件", font=(FONT, 10, 'bold'))
        self._source_label.pack(anchor=tk.W)

        # ─ 處理模式 ─
        mode_frame = ttk.LabelFrame(parent, text="處理模式", padding=8)
        mode_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Radiobutton(mode_frame, text="原始數據 — 從頭處理 (採集導出的export)",
                        variable=self.mode_var, value='raw').pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame, text="已處理數據 — 保留現有值, 只跑勾選的步驟",
                        variable=self.mode_var, value='processed').pack(anchor=tk.W)

        # ─ 文件選擇 ─
        file_frame = ttk.LabelFrame(parent, text="輸入文件", padding=8)
        file_frame.pack(fill=tk.X, pady=(0, 6))

        f_row = ttk.Frame(file_frame)
        f_row.pack(fill=tk.X)
        ttk.Entry(f_row, textvariable=self.file_var, width=38).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_row, text="瀏覽", command=self._browse_file, width=6).pack(side=tk.LEFT, padx=(4, 0))

        # ─ 輸出設定 ─
        out_frame = ttk.LabelFrame(parent, text="輸出設定", padding=8)
        out_frame.pack(fill=tk.X, pady=(0, 6))

        # 輸出目錄
        ttk.Label(out_frame, text="輸出目錄:").pack(anchor=tk.W)
        od_row = ttk.Frame(out_frame)
        od_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Entry(od_row, textvariable=self.out_dir_var, width=34).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(od_row, text="瀏覽", command=self._browse_out_dir, width=6).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(out_frame, text="空=輸入文件同目錄下的 output/", foreground='gray',
                  font=(FONT, 8)).pack(anchor=tk.W)

        # 文件名 + 衝突處理
        name_row = ttk.Frame(out_frame)
        name_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(name_row, text="文件名:").pack(side=tk.LEFT)
        ttk.Entry(name_row, textvariable=self.out_name_var, width=15).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(name_row, text=".xlsx", foreground='gray').pack(side=tk.LEFT)

        ttk.Label(name_row, text="   同名:").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(name_row, text="自動加序號", variable=self.conflict_var,
                        value='rename').pack(side=tk.LEFT)
        ttk.Radiobutton(name_row, text="追加", variable=self.conflict_var,
                        value='append').pack(side=tk.LEFT)
        ttk.Radiobutton(name_row, text="加時間戳", variable=self.conflict_var,
                        value='timestamp').pack(side=tk.LEFT)

        ttk.Label(out_frame, text="合格→{名}.xlsx  不合格→{名}_不合格.xlsx  未分類→{名}_未分類.xlsx",
                  foreground='gray', font=(FONT, 8)).pack(anchor=tk.W, pady=(2, 0))

        # ─ TG ID + 配額狀態 (跑貴功能必填) ─
        tg_frame = ttk.LabelFrame(parent, text="TG ID (跑貴功能必填)", padding=8)
        tg_frame.pack(fill=tk.X, pady=(0, 6))
        tg_row = ttk.Frame(tg_frame)
        tg_row.pack(fill=tk.X)
        ttk.Label(tg_row, text="TG ID:").pack(side=tk.LEFT)
        ttk.Entry(tg_row, textvariable=self.tg_id_var, width=15).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(tg_row, text="查配額", command=self._refresh_quota, width=8).pack(side=tk.LEFT)
        self._quota_label = ttk.Label(tg_frame, textvariable=self.quota_status_var,
                                       foreground='#0066CC', font=(FONT, 9))
        self._quota_label.pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(tg_frame,
                  text="空著 = 鎖死 Stage E / 去水印 / 首圖優化 (省 API). 每日 10:00 重置.",
                  foreground='gray', font=(FONT, 8)).pack(anchor=tk.W)
        # tg_id 變更時重查配額 + 更新鎖定狀態
        self.tg_id_var.trace_add('write', lambda *a: self._on_tg_id_change())

        # ─ 處理步驟 ─
        steps_frame = ttk.LabelFrame(parent, text="處理步驟 (勾選啟用)", padding=8)
        steps_frame.pack(fill=tk.X, pady=(0, 6))

        # ★ 4.0.48: 三合一. _expensive 是合成虛擬 key (用 self.expensive_var), 內部仍 3 個 step_vars
        step_info = [
            ('category',  '分類映射',                 '標紅分類先過濾'),
            ('price',     '價格轉換',                 '過高過低先過濾'),
            ('translate', '翻譯 (日→繁, 需API)',     '煤爐專用, 只翻譯合格商品'),
            ('replace',   '替換詞',                   '替換詞.xlsx'),
            ('price_clean','價格/數字清洗',            '刪除價格數字和促銷文字'),
            ('keyword',   '關鍵詞過濾',               '關鍵詞.xlsx (LLM 前提早攔截)'),
            ('seo_v64_image', '★ V65 多模態 SEO 改寫', 'Stage A-D: 清洗+主副詞+標題+標籤. 4 stage 整合處理'),
            ('_expensive', '★ 圖片智能優化 (3in1, 需配額)',
             'Stage E reject 判定 + K0 去水印救援 + 首圖 AI 重畫. 三個一起 on/off, 沒配額自動鎖.'),
            ('defaults',  '默認值填充',               '所在地/商品狀況/付款方式等'),
            ('dedup',     '標題去重',                 '相同標題加(2)(3)後綴'),
            ('desc_append', '說明模板',               '批量添加到說明列末尾'),
        ]

        # 新增 step 在舊 config 沒對應 key 時的預設值 (對真實 step_vars key)
        STEP_DEFAULT_TRUE = {'v65_stage_e'}  # 舊 config 升級兼容: 沒這 key 時預設 True

        # ★ 4.0.47: 改 2 列 grid 排, 13 個 step 高度從 ~325px 砍到 ~165px (省一半)
        # tip 文字移到 hover tooltip, 不擠在主畫面
        # steps_frame 內 grid: 兩個 column 都可拉伸
        steps_frame.columnconfigure(0, weight=1, uniform='steps')
        steps_frame.columnconfigure(1, weight=1, uniform='steps')

        self._step_checkbuttons = {}
        for i, (key, label, tip) in enumerate(step_info):
            # ★ 4.0.48: _expensive 是合成 GUI key, 用 self.expensive_var 不是 step_vars
            if key == '_expensive':
                var = self.expensive_var
            else:
                var = self.step_vars.get(key)
                if var is None:
                    default = key in STEP_DEFAULT_TRUE
                    var = tk.BooleanVar(value=default)
                    self.step_vars[key] = var
            row = i // 2
            col = i % 2
            cb = ttk.Checkbutton(steps_frame, text=label, variable=var)
            cb.grid(row=row, column=col, sticky=tk.W, padx=(0, 12), pady=2)
            if tip:
                _attach_tooltip(cb, tip)
            self._step_checkbuttons[key] = cb

        # 計算下個可用 row (給說明模板選擇器用)
        _last_step_row = (len(step_info) - 1) // 2 + 1

        # ─ 說明模板選擇器 (steps_frame 改 grid 後, 模板列跨 2 column) ─
        tpl_row = ttk.Frame(steps_frame)
        tpl_row.grid(row=_last_step_row, column=0, columnspan=2,
                     sticky=tk.W, pady=(4, 2), padx=(24, 0))
        ttk.Label(tpl_row, text="使用模板:", font=(FONT, 9)).pack(side=tk.LEFT)
        self.template_combo = ttk.Combobox(
            tpl_row, textvariable=self.template_var,
            state='readonly', width=18, font=(FONT, 9))
        self.template_combo.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(tpl_row, text="管理模板",
                   command=lambda: self._open_settings(tab_index=4),
                   width=8).pack(side=tk.LEFT)
        self._refresh_template_combo()

        # ─ 操作按鈕 ─
        # ★ 4.0.28: 縮短文字 + 減 padx, 5 個按鈕在預設窗寬都顯示完整
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=(6, 6))

        self.btn_start = ttk.Button(btn_frame, text="▶ 開始",
                                     command=self._start_processing, width=8)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 2))

        self.btn_stop = ttk.Button(btn_frame, text="■ 停止",
                                    command=self._stop_processing, state=tk.DISABLED, width=8)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 2))

        # ★ Resume 按鈕 (paused 後顯示)
        self.btn_resume = ttk.Button(btn_frame, text="▶ 繼續中斷",
                                     command=self._resume_processing, state=tk.DISABLED, width=10)
        self.btn_resume.pack(side=tk.LEFT, padx=(0, 2))

        ttk.Button(btn_frame, text="⚙ 設定", width=6,
                   command=self._open_settings).pack(side=tk.LEFT, padx=(0, 2))

        ttk.Button(btn_frame, text="📂 輸出", width=6,
                   command=self._open_output_dir).pack(side=tk.LEFT, padx=(0, 2))

        ttk.Button(btn_frame, text="📋 打開詳細日誌",
                   command=self._open_detail_log).pack(side=tk.LEFT)

        # ─ 進度條 ─
        ttk.Progressbar(parent, variable=self.progress_var,
                        maximum=100).pack(fill=tk.X, pady=(0, 6))

        # ─ 統計面板 ─
        self.stats_frame = ttk.LabelFrame(parent, text="處理統計", padding=8)
        self.stats_frame.pack(fill=tk.X)

        self.stat_labels = {}
        for i, (key, label) in enumerate([
            ('input', '輸入'), ('good', '合格'), ('bad', '不合格'),
            ('uncat', '未分類'), ('time', '耗時'),
        ]):
            ttk.Label(self.stats_frame, text=f"{label}:",
                      font=(FONT, 9)).grid(row=i // 3, column=(i % 3) * 2, sticky=tk.W, padx=(0, 4))
            lbl = ttk.Label(self.stats_frame, text="—", font=(FONT, 9, 'bold'))
            lbl.grid(row=i // 3, column=(i % 3) * 2 + 1, sticky=tk.W, padx=(0, 12))
            self.stat_labels[key] = lbl

        # ─ 狀態欄 ─
        ttk.Label(parent, textvariable=self.status_var,
                  foreground='gray').pack(fill=tk.X, pady=(4, 0))

    def _build_right(self, parent):
        # ─ 日誌區 ─
        log_frame = ttk.LabelFrame(parent, text="處理日誌", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=('Consolas', 9),
            bg='#f8f8f5', fg='#333', state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 日誌tag
        self.log_text.tag_configure('info', foreground='#1d4ed8')
        self.log_text.tag_configure('warn', foreground='#b45309')
        self.log_text.tag_configure('error', foreground='#be123c')
        self.log_text.tag_configure('ok', foreground='#16a34a')

        # ─ 日誌操作 ─
        log_btn = ttk.Frame(parent)
        log_btn.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(log_btn, text="清除日誌", command=self._clear_log).pack(side=tk.RIGHT)

    # ══════════════════ 來源切換 ══════════════════

    def _update_step_states(self):
        """根據來源自動調整步驟開關"""
        src = self.source_var.get()
        translate_cb = self._step_checkbuttons.get('translate')

        if src == 'mercari':
            # 煤爐: 自動開啟翻譯
            if self.config.get('steps', {}).get('translate', {}).get('auto_for_mercari', True):
                self.step_vars['translate'].set(True)
            if translate_cb:
                translate_cb.configure(state=tk.NORMAL)
        else:
            # 鹹魚: 翻譯關閉且灰顯
            self.step_vars['translate'].set(False)
            if translate_cb:
                translate_cb.configure(state=tk.DISABLED)

    # ══════════════════ 文件選擇 ══════════════════

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="選擇採集導出的Excel文件",
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")],
        )
        if path:
            self.file_var.set(path)
            self._auto_detect_source(path)

    def _auto_detect_source(self, path):
        """根據商品條碼自動識別來源: 連結=煤爐, 純數字=鹹魚"""
        try:
            df = pd.read_excel(path, engine='openpyxl', nrows=10)
            barcode_col = None
            for col in ['商品條碼', '連結']:
                if col in df.columns:
                    barcode_col = col
                    break
            if not barcode_col:
                self._source_label.configure(text="來源: 無法識別", foreground='red')
                return

            samples = df[barcode_col].dropna()
            if len(samples) == 0:
                self._source_label.configure(text="來源: 無法識別 (空數據)", foreground='red')
                return

            sample = str(samples.iloc[0]).strip()
            if sample.startswith('http'):
                self.source_var.set('mercari')
                self._source_label.configure(text="來源: 煤爐 (Mercari) — 日文商品", foreground='#0066CC')
            else:
                self.source_var.set('goofish')
                self._source_label.configure(text="來源: 鹹魚 (Goofish) — 繁中商品", foreground='#008800')
            self._update_step_states()
        except Exception as e:
            self._source_label.configure(text=f"來源: 識別失敗 ({e})", foreground='red')

    def _browse_out_dir(self):
        path = filedialog.askdirectory(title="選擇輸出目錄")
        if path:
            self.out_dir_var.set(path)

    # ══════════════════ 處理控制 ══════════════════

    def _start_processing(self):
        fpath = self.file_var.get().strip()
        if not fpath or not os.path.isfile(fpath):
            messagebox.showwarning("提示", "請先選擇有效的輸入文件")
            return

        # ★ 防呆: 偵測到未完成 ckpt 時警告 (避免誤按開始處理覆寫已跑進度)
        out_dir = self.out_dir_var.get().strip()
        if out_dir:
            try:
                from checkpoint_manager import CheckpointManager
                if CheckpointManager.has_checkpoint(out_dir):
                    ckpt = CheckpointManager.load_existing(out_dir)
                    sm = ckpt.summary()
                    if sm.get('completed_stages') or sm.get('current_stage'):
                        ans = messagebox.askyesno(
                            "⚠️ 警告: 將覆寫已跑進度",
                            f"偵測到未完成的 checkpoint!\n\n"
                            f"已完成 stages: {', '.join(sm['completed_stages']) or '(無)'}\n"
                            f"中斷時 stage: {sm.get('current_stage') or '(無)'}\n"
                            f"中斷時進度: {sm.get('done_in_current')} 件\n\n"
                            f"按「是」= 確定覆寫, 從頭重跑 (之前進度全丟!)\n"
                            f"按「否」= 取消, 改用「▶ 繼續中斷」按鈕")
                        if not ans:
                            return  # 用戶取消, 不開始
            except Exception:
                pass

        # 檢查說明模板設置
        if self.step_vars.get('desc_append', tk.BooleanVar(value=False)).get():
            templates = self.config.get('desc_templates', {}).get('templates', [])
            selected_tpl = self.template_var.get().strip()
            if not templates:
                messagebox.showwarning("提示",
                    "已勾選「說明模板」但尚未設置任何模板\n請先到「管理模板」中新增模板")
                return
            if not selected_tpl:
                messagebox.showwarning("提示",
                    "已勾選「說明模板」但尚未選擇模板\n請在下拉選單中選擇要使用的模板")
                return
            # 確認所選模板仍存在
            tpl_names = [t['name'] for t in templates]
            if selected_tpl not in tpl_names:
                messagebox.showwarning("提示",
                    f"所選模板「{selected_tpl}」已不存在\n請重新選擇模板")
                return

        # 自動識別來源 (每次處理前都重新檢測)
        self._auto_detect_source(fpath)

        # 保存配置
        self._sync_config_from_ui()
        save_config(self.config)

        # 清空日誌和統計
        self._clear_log()
        for lbl in self.stat_labels.values():
            lbl.configure(text='—')
        self.progress_var.set(0)

        # 切換按鈕狀態
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status_var.set('處理中...')

        # 後台線程 (resume=False 表示新跑)
        self.worker_thread = threading.Thread(target=self._worker, args=(fpath, False), daemon=True)
        self.worker_thread.start()
        # 3 秒後自動刷新配額狀態 (Pipeline 開始就 consume 完, 讓狀態列同步顯示「已扣」)
        self.root.after(3000, self._refresh_quota)

    def _stop_processing(self):
        if self.pipeline:
            self.pipeline.stop()
        self.status_var.set('正在停止...')

    def _resume_processing(self):
        """從 _checkpoint.* 繼續上次中斷的 run"""
        fpath = self.file_var.get().strip()
        if not fpath or not os.path.isfile(fpath):
            messagebox.showwarning("提示", "請先選擇與上次中斷相同的輸入文件")
            return
        # 先 ping API 確認通了再繼續
        try:
            from checkpoint_manager import APIMonitor
            mon = APIMonitor()
            ok, msg = mon.ping()
            if not ok:
                messagebox.showwarning("API 仍失效", f"{msg}\n\n請等 API 恢復後再點繼續")
                return
            self._log_msg(f"[Ping] {msg}")
            # ★ ping 通了 → 通知中介解除 hub stop 封鎖
            ok2, info = mon.notify_middleware_resume()
            if ok2:
                self._log_msg(f"[中介 resume] 已通知 (was_blocked={info.get('was_blocked', '?')})")
            else:
                self._log_msg(f"[中介 resume] 通知失敗 (不影響繼續): {info.get('error', '?')}")
        except Exception as e:
            messagebox.showwarning("Ping 失敗", f"無法測試 API: {e}")
            return
        # 切按鈕
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.btn_resume.configure(state=tk.DISABLED)
        self.status_var.set('Resume 中...')
        self.worker_thread = threading.Thread(target=self._worker, args=(fpath, True), daemon=True)
        self.worker_thread.start()

    def _worker(self, input_path: str, resume: bool = False):
        """後台處理線程. resume=True 從 checkpoint 接續"""
        try:
            source = self.source_var.get()
            self.pipeline = Pipeline(self.config, log_fn=self._log_msg)

            def progress_fn(name, current, total):
                pct = (current / total * 100) if total > 0 else 0
                self.root.after(0, lambda p=pct: self.progress_var.set(p))
                self.root.after(0, lambda n=name, c=current, t=total:
                                self.status_var.set(f'處理中: {n} ({c}/{t})'))

            result = self.pipeline.run(input_path, source, progress_fn=progress_fn, resume=resume)
            self.last_result = result

            # 更新統計
            self.root.after(0, self._update_stats, result)
            if result.get('paused'):
                # ★ 暫停: 顯示 resume 按鈕 + 警告
                reason = result.get('paused_reason', 'API 失效')
                self.root.after(0, lambda r=reason: self.status_var.set(f'⏸ 暫停: {r}'))
                self.root.after(0, lambda: self.btn_resume.configure(state=tk.NORMAL))
                self.root.after(0, lambda r=reason: messagebox.showwarning(
                    "已暫停",
                    f"{r}\n\n進度已寫入 _checkpoint.* (output_dir)\n"
                    f"★ 不要關閉軟件 (關了也能恢復, 但建議等 API 恢復後按「▶ 繼續中斷」)"))
            else:
                self.root.after(0, lambda: self.status_var.set('處理完成!'))
                self.root.after(0, lambda: self.btn_resume.configure(state=tk.DISABLED))

        except InterruptedError:
            self._log_msg("\n[中止] 用戶停止了處理")
            self._maybe_refund_quota('user_stop')
            self.root.after(0, lambda: self.status_var.set('已停止'))
        except Exception as e:
            self._log_msg(f"\n[嚴重錯誤] {e}")
            import traceback
            self._log_msg(traceback.format_exc())
            self._maybe_refund_quota(f'error: {type(e).__name__}')
            # ★ 4.0.27: lambda capture e by default arg (Python 3 except 結束 e 出 scope, lambda 抓不到)
            err_msg = str(e)
            self.root.after(0, lambda em=err_msg: self.status_var.set(f'錯誤: {em}'))
        finally:
            self.pipeline = None
            self.root.after(0, lambda: self.btn_start.configure(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_stop.configure(state=tk.DISABLED))
            # 跑完 (成功 / 中止 / 異常) 都刷新配額狀態, UI 顯示真實扣費結果
            self.root.after(500, self._refresh_quota)

    def _maybe_refund_quota(self, cause: str):
        """abort/error 時退費: 只有在貴功能還沒開跑前才退 (跑到一半 API 已花, 不退)"""
        try:
            p = self.pipeline
            if not p:
                return
            qc = getattr(p, '_quota_client', None)
            consumed = int(getattr(p, '_quota_consumed', 0) or 0)
            expensive_started = bool(getattr(p, '_expensive_started', False))
            if not qc or consumed <= 0:
                return
            if expensive_started:
                self._log_msg(f"[配額] 中止原因: {cause} — 但貴功能已開跑, API 已花, 不退費 (扣的 {consumed} 件保留)")
                return
            res = qc.refund(consumed)
            if res:
                rem = res.get('remaining', '?')
                self._log_msg(f"[配額] 中止原因: {cause} — 已退回 {consumed} 件 (剩 {rem})")
            else:
                self._log_msg(f"[配額] 中止原因: {cause} — 退費失敗 (網路?), 已扣的 {consumed} 件未退")
            # 即時刷新 UI
            self.root.after(0, self._refresh_quota)
        except Exception as e:
            self._log_msg(f"[配額] 退費邏輯異常 (不影響中止): {e}")

    def _update_stats(self, result):
        self.stat_labels['input'].configure(text=str(result.get('stats', {}).get('input_rows', 0)))
        self.stat_labels['good'].configure(text=str(result.get('good_count', 0)))
        self.stat_labels['bad'].configure(text=str(result.get('bad_count', 0)))
        self.stat_labels['uncat'].configure(text=str(result.get('uncat_count', 0)))
        self.stat_labels['time'].configure(text=f"{result.get('elapsed', 0):.1f}s")

    # ══════════════════ 日誌 ══════════════════

    def _log_msg(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)

                # 選擇tag
                tag = None
                if '[錯誤]' in msg or '[嚴重錯誤]' in msg:
                    tag = 'error'
                elif '[警告]' in msg or '[跳過]' in msg:
                    tag = 'warn'
                elif '✓' in msg or '完成' in msg:
                    tag = 'ok'
                elif '[Step' in msg:
                    tag = 'info'

                self.log_text.insert(tk.END, msg + '\n', tag)
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
            except queue.Empty:
                break
        self.root.after(100, self._poll_log)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ══════════════════ 設定視窗 ══════════════════

    def _open_settings(self, tab_index=0):
        win = tk.Toplevel(self.root)
        win.title("設定")
        win.geometry("700x750")
        win.transient(self.root)

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ─ Tab1: 文件路徑 ─
        tab_paths = ttk.Frame(nb, padding=12)
        nb.add(tab_paths, text="文件路徑")

        self._path_vars = {}
        self._path_status_labels = {}
        path_items = [
            ('mapping_xlsx', '煤爐映射表', False),
            ('goofish_mapping_xlsx', '鹹魚映射表', False),
            ('replace_xlsx', '替換詞表', False),
            ('number_removal_xlsx', '只刪數字規則', False),
            ('full_removal_xlsx', '全刪規則', False),
            ('keyword_xlsx', '關鍵詞表', False),
            ('seo_tool_dir', 'SEO翻譯工具目錄', True),
        ]

        for i, (key, label, is_dir) in enumerate(path_items):
            row = i * 2  # 每項佔2行: 標籤+輸入框 和 狀態
            ttk.Label(tab_paths, text=f"{label}:", font=(FONT, 10)).grid(
                row=row, column=0, sticky=tk.W, pady=(8, 0))
            var = tk.StringVar(value=self.config.get('paths', {}).get(key, ''))
            self._path_vars[key] = var
            ttk.Entry(tab_paths, textvariable=var, width=55).grid(
                row=row, column=1, sticky=tk.EW, padx=(8, 4), pady=(8, 0))
            ttk.Button(tab_paths, text="瀏覽",
                       command=lambda v=var, d=is_dir: self._browse_path(v, d)
                       ).grid(row=row, column=2, pady=(8, 0))
            # 狀態標籤 (綠色=已設置 / 紅色=未設置)
            status_lbl = ttk.Label(tab_paths, text='', font=(FONT, 8))
            status_lbl.grid(row=row + 1, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))
            self._path_status_labels[key] = (var, status_lbl, is_dir)
            # 路徑變更時自動更新狀態
            var.trace_add('write', lambda *a, k=key: self._update_path_status(k))

        tab_paths.columnconfigure(1, weight=1)
        # 初始化所有狀態
        for key in self._path_status_labels:
            self._update_path_status(key)

        # ─ Tab2: 價格設定 ─
        tab_price = ttk.Frame(nb, padding=12)
        nb.add(tab_price, text="價格設定")

        price_cfg = self.config.get('price', {})

        row = 0
        ttk.Label(tab_price, text="煤爐除數 (日元÷此數=基礎價):").grid(row=row, column=0, sticky=tk.W)
        self._divisor_var = tk.StringVar(value=str(price_cfg.get('mercari_divisor', 20)))
        ttk.Entry(tab_price, textvariable=self._divisor_var, width=8).grid(row=row, column=1, sticky=tk.W, padx=8)

        row += 1
        ttk.Label(tab_price, text="價格下限:").grid(row=row, column=0, sticky=tk.W, pady=(8, 0))
        self._min_price_var = tk.StringVar(value=str(price_cfg.get('min_price', 15)))
        ttk.Entry(tab_price, textvariable=self._min_price_var, width=8).grid(row=row, column=1, sticky=tk.W, padx=8, pady=(8, 0))
        ttk.Label(tab_price, text="價格上限:").grid(row=row, column=2, sticky=tk.W, pady=(8, 0))
        self._max_price_var = tk.StringVar(value=str(price_cfg.get('max_price', 80000)))
        ttk.Entry(tab_price, textvariable=self._max_price_var, width=8).grid(row=row, column=3, sticky=tk.W, pady=(8, 0))

        row += 1
        ttk.Label(tab_price, text="兜底價 (低於最低檔的固定TWD價格):").grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        self._floor_var = tk.StringVar(value=str(price_cfg.get('floor_price', 700)))
        ttk.Entry(tab_price, textvariable=self._floor_var, width=8).grid(row=row, column=2, sticky=tk.W, pady=(8, 0))

        row += 1
        ttk.Separator(tab_price, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=12)

        row += 1
        ttk.Label(tab_price, text="梯度倍率表:", font=(FONT, 10, 'bold')).grid(row=row, column=0, columnspan=4, sticky=tk.W)

        row += 1
        for c, h in enumerate(['基礎價下限', '基礎價上限', '倍率']):
            ttk.Label(tab_price, text=h, font=(FONT, 9, 'bold')).grid(row=row, column=c, padx=4)

        self._tier_vars = []
        tiers = price_cfg.get('tiers', [])
        for i, tier in enumerate(tiers):
            row += 1
            v_min = tk.StringVar(value=str(tier.get('min', 0)))
            v_max = tk.StringVar(value=str(tier.get('max', 0)))
            v_mul = tk.StringVar(value=str(tier.get('multiplier', 10)))
            ttk.Entry(tab_price, textvariable=v_min, width=10).grid(row=row, column=0, padx=4, pady=2)
            ttk.Entry(tab_price, textvariable=v_max, width=10).grid(row=row, column=1, padx=4, pady=2)
            ttk.Entry(tab_price, textvariable=v_mul, width=10).grid(row=row, column=2, padx=4, pady=2)
            self._tier_vars.append((v_min, v_max, v_mul))

        # ─ Tab3: 默認值 ─
        tab_defs = ttk.Frame(nb, padding=12)
        nb.add(tab_defs, text="默認值")

        self._def_vars = {}
        for src_type, src_label in [('mercari', '煤爐'), ('goofish', '鹹魚')]:
            lf = ttk.LabelFrame(tab_defs, text=f"{src_label} 默認值", padding=8)
            lf.pack(fill=tk.X, pady=(0, 8))

            defs = self.config.get('defaults', {}).get(src_type, {})
            self._def_vars[src_type] = {}

            fields = [
                ('location', '所在地'),
                ('condition', '商品狀況'),
                ('product_type', '商品類型'),
                ('shipping', '交貨方式'),
                ('ship_date', '出貨日期'),
                ('listing_type', '上架類型'),
                ('quantity', '數量'),
            ]
            for i, (key, label) in enumerate(fields):
                ttk.Label(lf, text=f"{label}:").grid(row=i, column=0, sticky=tk.W, pady=2)
                var = tk.StringVar(value=str(defs.get(key, '')))
                self._def_vars[src_type][key] = var
                ttk.Entry(lf, textvariable=var, width=40).grid(
                    row=i, column=1, sticky=tk.EW, padx=(8, 0), pady=2)

            # 特殊提示
            if src_type == 'mercari':
                ttk.Label(lf, text="商品狀況填 'mapping' = 根據商品簡述自動映射",
                          foreground='gray', font=(FONT, 8)).grid(
                    row=len(fields), column=0, columnspan=2, sticky=tk.W)

            lf.columnconfigure(1, weight=1)

        # 付款方式 (共用)
        pay_frame = ttk.LabelFrame(tab_defs, text="付款方式 (兩源共用)", padding=8)
        pay_frame.pack(fill=tk.X)
        self._payment_var = tk.StringVar(
            value=self.config.get('defaults', {}).get('mercari', {}).get('payment', ''))
        ttk.Entry(pay_frame, textvariable=self._payment_var).pack(fill=tk.X)
        ttk.Label(pay_frame, text="多個付款方式用 | 分隔",
                  foreground='gray', font=(FONT, 8)).pack(anchor=tk.W)

        # API設定已移至 SEO翻譯工具/translator_config.json, 不再需要此Tab

        # ─ Tab4: LaMa 去水印 (CPU/GPU 自適應) ─
        tab_lama = ttk.Frame(nb, padding=12)
        nb.add(tab_lama, text="LaMa 去水印")

        lama_cfg = self.config.get('lama', {}) or {}

        ttk.Label(tab_lama, text="設定 AI 去水印 (LaMa) 用 GPU 還是 CPU。",
                  font=(FONT, 10, 'bold')).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(tab_lama,
                  text="GPU 快 10-20 倍, 但需要 NVIDIA 顯卡 + CUDA. 沒顯卡或舊卡請用 CPU.",
                  foreground='gray', font=(FONT, 9)).pack(anchor=tk.W, pady=(0, 12))

        # Device 選擇
        dev_frame = ttk.LabelFrame(tab_lama, text="運算裝置 (device)", padding=8)
        dev_frame.pack(fill=tk.X, pady=(0, 8))
        self._lama_device_var = tk.StringVar(value=lama_cfg.get('device', 'auto'))
        for val, label in [
            ('auto', 'auto — 自動偵測 (有 GPU 用 GPU, 沒有走 CPU) ★ 推薦'),
            ('cuda', 'cuda — 強制 GPU (沒 GPU 會自動退回 CPU)'),
            ('cpu',  'cpu  — 強制 CPU (相容垃圾顯卡 / 完全沒顯卡)'),
        ]:
            ttk.Radiobutton(dev_frame, text=label, variable=self._lama_device_var, value=val).pack(anchor=tk.W, pady=1)

        # Pool size 選擇
        pool_frame = ttk.LabelFrame(tab_lama, text="並發數 (pool_size, 速度 vs 顯存)", padding=8)
        pool_frame.pack(fill=tk.X, pady=(0, 8))

        pool_val = lama_cfg.get('pool_size')
        self._lama_pool_var = tk.StringVar(value='auto' if pool_val is None else str(pool_val))
        for val, label in [
            ('auto', 'auto — 依 GPU VRAM 自動決定 (24GB→28 / 16GB→20 / 12GB→16 / 8GB→10 / 6GB→6 / 4GB→3)  ★ 推薦'),
            ('1', '1 — 最低 (~1GB VRAM 或 極慢 CPU)'),
            ('2', '2 — 低 (~2GB VRAM 或 一般 CPU)'),
            ('4', '4 — 中低 (~3GB VRAM)'),
            ('8', '8 — 中 (~5GB VRAM, 4070 12GB 等)'),
            ('12', '12 — 中高 (~7GB VRAM, 12GB 卡保守)'),
            ('16', '16 — 高 (~9GB VRAM, 12GB 卡推薦)'),
            ('20', '20 — 很高 (~11GB VRAM, 16GB 卡推薦)'),
            ('28', '28 — 極限 (~16GB VRAM, 24GB 卡如 4090)'),
        ]:
            ttk.Radiobutton(pool_frame, text=label, variable=self._lama_pool_var, value=val).pack(anchor=tk.W, pady=1)

        # 提示
        info = ttk.Label(tab_lama,
            text=("💡 越大 pool = LaMa 並行越多 = 越吃顯存。設太激進 OOM 會自動降回安全值 (4.0.59+)。\n"
                  "    一個 LaMa instance idle ~525MB, inference peak ~825MB。\n"
                  "    保存後在主介面跑 [去水印救援], log 會顯示偵測到的 GPU 型號 + VRAM 跟實際 pool size."),
            foreground='#0066CC', font=(FONT, 9), justify=tk.LEFT)
        info.pack(anchor=tk.W, pady=(8, 0))

        # 環境診斷按鈕
        ttk.Separator(tab_lama, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        diag_row = ttk.Frame(tab_lama)
        diag_row.pack(fill=tk.X)
        ttk.Label(diag_row, text="🔧 環境診斷:",
                  font=(FONT, 10, 'bold')).pack(side=tk.LEFT)
        ttk.Button(diag_row, text="檢查 Python / GPU / 套件",
                   command=self._diagnose_env).pack(side=tk.LEFT, padx=8)

        # ─ Tab5: 說明模板 ─
        tab_tpl = ttk.Frame(nb, padding=12)
        nb.add(tab_tpl, text="說明模板")
        self._build_template_tab(tab_tpl)

        # 選擇指定 tab
        if 0 <= tab_index < nb.index('end'):
            nb.select(tab_index)

        # ─ 底部按鈕 ─
        btn_bar = ttk.Frame(win)
        btn_bar.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(btn_bar, text="保存設定",
                   command=lambda: self._save_settings(win)).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_bar, text="取消",
                   command=win.destroy).pack(side=tk.RIGHT)

    def _diagnose_env(self):
        """檢查環境: Python / 套件 / GPU / 配額後端 / 中樞檔案"""
        import sys, importlib, os
        lines = ['🔧 環境診斷報告', '=' * 40, '']

        # Python
        py_ver = '.'.join(map(str, sys.version_info[:3]))
        py_ok = sys.version_info >= (3, 10)
        lines.append(f"{'✓' if py_ok else '✗'} Python {py_ver} {'(OK, >=3.10)' if py_ok else '(太舊! 需 3.10+)'}")

        # 必要套件
        required = ['pandas', 'openpyxl', 'PIL', 'requests', 'cv2', 'torch']
        for pkg in required:
            try:
                m = importlib.import_module(pkg)
                ver = getattr(m, '__version__', '?')
                lines.append(f"  ✓ {pkg} {ver}")
            except ImportError:
                lines.append(f"  ✗ {pkg} 未裝!")

        # LaMa
        try:
            from simple_lama_inpainting import SimpleLama
            lines.append(f"  ✓ simple_lama_inpainting (去水印)")
        except ImportError:
            lines.append(f"  ✗ simple_lama_inpainting 未裝!")

        # GPU 偵測
        lines.append('')
        lines.append('━ GPU/CPU ━')
        try:
            import torch
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                lines.append(f"  ✓ NVIDIA GPU: {props.name}")
                lines.append(f"     VRAM: {props.total_memory / (1024**3):.1f} GB")
                lines.append(f"     CUDA: {torch.version.cuda}")
                # LaMa pool 預測
                from processors.image_dewatermark import _detect_lama_config
                d, p, _ = _detect_lama_config('auto')
                lines.append(f"     LaMa 自動配置: device={d}, pool={p}")
            else:
                lines.append(f"  ⚠ 沒偵測到 NVIDIA GPU")
                lines.append(f"     LaMa 會走 CPU 模式 (慢 10-20x)")
        except Exception as e:
            lines.append(f"  ✗ torch 異常: {e}")

        # 配額後端連線
        lines.append('')
        lines.append('━ 配額後端 ━')
        try:
            from quota_client import QuotaClient
            qc = QuotaClient(self.config)
            if not qc.is_active():
                lines.append(f"  ⊘ 配額已關閉 (mode=off)")
            elif not qc.is_configured():
                lines.append(f"  ⚠ 未填 TG ID / 配置不完整")
            else:
                status = qc.check()
                if status:
                    lines.append(f"  ✓ 連線 OK")
                    lines.append(f"     用量: {status.get('used')}/{status.get('limit')}, 剩 {status.get('remaining')}")
                else:
                    lines.append(f"  ✗ 連線失敗")
        except Exception as e:
            lines.append(f"  ✗ {e}")

        # 中樞資料檔
        lines.append('')
        lines.append('━ 設定檔/資料檔 ━')
        paths = self.config.get('paths', {}) or {}
        important = [
            ('mapping_xlsx', '煤爐映射表'),
            ('goofish_mapping_xlsx', '鹹魚映射表'),
            ('replace_xlsx', '替換詞表'),
            ('keyword_xlsx', '關鍵詞表'),
        ]
        for key, label in important:
            p = paths.get(key, '')
            if not p:
                lines.append(f"  - {label}: (未設定)")
                continue
            ap = p if os.path.isabs(p) else os.path.join(os.path.dirname(__file__), p)
            lines.append(f"  {'✓' if os.path.exists(ap) else '✗'} {label}: {p}")

        # 顯示
        win = tk.Toplevel(self.root)
        win.title('環境診斷')
        win.geometry('600x500')
        txt = scrolledtext.ScrolledText(win, font=('Consolas', 10), wrap=tk.WORD)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        txt.insert('1.0', '\n'.join(lines))
        txt.configure(state='disabled')
        ttk.Button(win, text='關閉', command=win.destroy).pack(pady=(0, 8))

    def _update_path_status(self, key):
        """更新路徑狀態顯示 (綠色已設置/紅色未設置)"""
        var, lbl, is_dir = self._path_status_labels[key]
        path = var.get().strip()
        if not path:
            lbl.configure(text='  未設置', foreground='gray')
            return
        # 解析相對路徑
        import os
        abs_path = os.path.join(os.path.dirname(__file__), path) if not os.path.isabs(path) else path
        if is_dir:
            exists = os.path.isdir(abs_path)
        else:
            exists = os.path.isfile(abs_path)
        if exists:
            lbl.configure(text='  OK', foreground='green')
        else:
            lbl.configure(text='  找不到文件', foreground='red')

    def _browse_path(self, var, is_dir=False):
        if is_dir:
            path = filedialog.askdirectory()
        else:
            path = filedialog.askopenfilename(
                filetypes=[("Excel文件", "*.xlsx"), ("所有文件", "*.*")])
        if path:
            var.set(path)

    def _save_settings(self, win):
        """從設定視窗回寫到 config"""
        try:
            # 路徑
            for key, var in self._path_vars.items():
                self.config.setdefault('paths', {})[key] = var.get()

            # 價格
            pc = self.config.setdefault('price', {})
            try:
                pc['mercari_divisor'] = int(self._divisor_var.get())
            except (ValueError, AttributeError):
                pass
            try:
                pc['min_price'] = float(self._min_price_var.get())
            except (ValueError, AttributeError):
                pass
            try:
                pc['max_price'] = float(self._max_price_var.get())
            except (ValueError, AttributeError):
                pass
            try:
                pc['floor_price'] = int(self._floor_var.get())
            except (ValueError, AttributeError):
                pass

            new_tiers = []
            for v_min, v_max, v_mul in getattr(self, '_tier_vars', []):
                try:
                    new_tiers.append({
                        'min': float(v_min.get()),
                        'max': float(v_max.get()),
                        'multiplier': float(v_mul.get()),
                    })
                except ValueError:
                    continue
            if new_tiers:
                pc['tiers'] = new_tiers

            # 默認值
            for src_type in ('mercari', 'goofish'):
                defs = self.config.setdefault('defaults', {}).setdefault(src_type, {})
                for key, var in self._def_vars.get(src_type, {}).items():
                    val = var.get()
                    if key == 'quantity':
                        try:
                            val = int(val)
                        except ValueError:
                            val = 1
                    defs[key] = val
                if hasattr(self, '_payment_var'):
                    defs['payment'] = self._payment_var.get()

            # 說明模板 (先自動保存, 然後深拷貝到 config 防止 destroy 時被污染)
            if hasattr(self, '_tpl_auto_save'):
                self._tpl_auto_save()
            if hasattr(self, '_tpl_working_list'):
                self.config.setdefault('desc_templates', {})['templates'] = \
                    copy.deepcopy(self._tpl_working_list)

            # LaMa device + pool_size
            if hasattr(self, '_lama_device_var'):
                lama = self.config.setdefault('lama', {})
                lama['device'] = self._lama_device_var.get()
                pool_v = self._lama_pool_var.get() if hasattr(self, '_lama_pool_var') else 'auto'
                lama['pool_size'] = None if pool_v == 'auto' else int(pool_v)

            save_config(self.config)
            self._refresh_template_combo()

            # 設定 loading 旗標, 防止 win.destroy() 觸發 trace 回調污染數據
            self._tpl_loading = True
            messagebox.showinfo("提示", "設定已保存")
            win.destroy()
        except Exception as e:
            messagebox.showerror("保存失敗", f"保存設定時出錯:\n{e}")

    # ══════════════════ 說明模板管理 ══════════════════

    def _refresh_template_combo(self):
        """刷新主介面的模板下拉選單"""
        templates = self.config.get('desc_templates', {}).get('templates', [])
        names = [t['name'] for t in templates]
        self.template_combo['values'] = names
        # 如果當前選擇不在列表中, 清空
        if self.template_var.get() not in names:
            self.template_var.set(names[0] if names else '')

    def _build_template_tab(self, parent):
        """構建設定視窗中的說明模板管理Tab"""
        # 深拷貝模板列表作為工作副本
        self._tpl_working_list = copy.deepcopy(
            self.config.get('desc_templates', {}).get('templates', []))
        self._tpl_loading = False
        self._tpl_current_idx = None

        # ── 左側: 模板列表 ──
        left = ttk.Frame(parent)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))

        ttk.Label(left, text="模板列表:", font=(FONT, 10, 'bold')).pack(anchor=tk.W)

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self._tpl_listbox = tk.Listbox(list_frame, width=18, font=(FONT, 10),
                                        exportselection=False)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                   command=self._tpl_listbox.yview)
        self._tpl_listbox.configure(yscrollcommand=scrollbar.set)
        self._tpl_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 填充列表
        for tpl in self._tpl_working_list:
            self._tpl_listbox.insert(tk.END, tpl['name'])

        self._tpl_listbox.bind('<<ListboxSelect>>', self._tpl_on_select)

        # 列表操作按鈕
        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_row, text="新增", command=self._tpl_add, width=6).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="刪除", command=self._tpl_delete, width=6).pack(side=tk.LEFT)

        # ── 右側: 編輯區 ──
        right = ttk.Frame(parent)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 先建立所有變量 (避免 trace 回調時引用不存在的變量)
        self._tpl_name_var = tk.StringVar()
        self._tpl_font_var = tk.StringVar()
        self._tpl_size_var = tk.StringVar()
        self._tpl_color_var = tk.StringVar()
        self._tpl_pos_var = tk.StringVar(value='before')

        # 模板名稱
        ttk.Label(right, text="模板名稱:", font=(FONT, 10)).pack(anchor=tk.W)
        ttk.Entry(right, textvariable=self._tpl_name_var, width=30,
                  font=(FONT, 10)).pack(fill=tk.X, pady=(2, 8))

        # 字體與字號
        font_frame = ttk.Frame(right)
        font_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(font_frame, text="字體:", font=(FONT, 10)).pack(side=tk.LEFT)
        font_families = ['微軟正黑體', '新細明體', '標楷體', '細明體',
                         '黑體', '宋體', 'Arial', 'Verdana', 'Times New Roman']
        font_combo = ttk.Combobox(font_frame, textvariable=self._tpl_font_var,
                                   values=font_families, width=14, font=(FONT, 9))
        font_combo.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(font_frame, text="字號:", font=(FONT, 10)).pack(side=tk.LEFT)
        size_combo = ttk.Combobox(font_frame, textvariable=self._tpl_size_var,
                                   values=['10', '12', '14', '16', '18', '20', '24', '28', '32'],
                                   width=5, font=(FONT, 9))
        size_combo.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(font_frame, text="px", foreground='gray',
                  font=(FONT, 8)).pack(side=tk.LEFT, padx=(2, 0))

        # 顏色設定
        color_frame = ttk.Frame(right)
        color_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(color_frame, text="文字顏色:", font=(FONT, 10)).pack(side=tk.LEFT)
        self._tpl_color_entry = ttk.Entry(color_frame, textvariable=self._tpl_color_var,
                                           width=10, font=(FONT, 10))
        self._tpl_color_entry.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Button(color_frame, text="選擇顏色",
                   command=self._tpl_pick_color, width=8).pack(side=tk.LEFT, padx=(0, 4))
        self._tpl_color_preview = tk.Label(color_frame, text="  ■■  ", font=(FONT, 12),
                                            bg='#f0f0f0', relief='sunken')
        self._tpl_color_preview.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(color_frame, text="(空=預設顏色)", foreground='gray',
                  font=(FONT, 8)).pack(side=tk.LEFT, padx=(8, 0))

        # 插入位置
        pos_frame = ttk.Frame(right)
        pos_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(pos_frame, text="插入位置:", font=(FONT, 10)).pack(side=tk.LEFT)
        ttk.Radiobutton(pos_frame, text="說明前面 (模板在上, 商品描述在下)",
                        variable=self._tpl_pos_var, value='before').pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(pos_frame, text="說明後面",
                        variable=self._tpl_pos_var, value='after').pack(side=tk.LEFT, padx=(8, 0))

        # 模板內容
        ttk.Label(right, text="模板內容 (即時預覽字體與顏色):", font=(FONT, 10)).pack(anchor=tk.W)
        self._tpl_content_text = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, font=(FONT, 10), height=14)
        self._tpl_content_text.pack(fill=tk.BOTH, expand=True, pady=(2, 8))

        # 綁定 trace — 所有變量和控件都已建立, 自動保存
        self._tpl_font_var.trace_add('write', lambda *a: (self._tpl_update_content_style(), self._tpl_auto_save()))
        self._tpl_size_var.trace_add('write', lambda *a: (self._tpl_update_content_style(), self._tpl_auto_save()))
        self._tpl_color_var.trace_add('write', lambda *a: (self._tpl_update_preview(), self._tpl_auto_save()))
        self._tpl_name_var.trace_add('write', lambda *a: self._tpl_auto_save())
        self._tpl_pos_var.trace_add('write', lambda *a: self._tpl_auto_save())
        self._tpl_content_text.bind('<KeyRelease>', lambda e: self._tpl_auto_save())

        # 提示
        ttk.Label(right,
                  text="提示: 編輯自動保存, 名稱重複會自動替換原有模板。\n"
                       "支持多行文字、Emoji、HTML標籤。字體/字號/顏色會自動套用。\n"
                       "最終需點擊底部「保存設定」才會永久生效。",
                  foreground='gray', font=(FONT, 8), justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        # 自動選中第一個模板
        if self._tpl_working_list:
            self._tpl_listbox.selection_set(0)
            self._tpl_on_select()

    def _tpl_on_select(self, event=None):
        """列表選擇變更 → 載入模板到編輯區"""
        if getattr(self, '_tpl_loading', False):
            return
        sel = self._tpl_listbox.curselection()
        if sel:
            idx = sel[0]
        elif getattr(self, '_tpl_current_idx', None) is not None:
            idx = self._tpl_current_idx
        else:
            return
        if idx >= len(self._tpl_working_list):
            return
        self._tpl_loading = True
        try:
            self._tpl_current_idx = idx
            tpl = self._tpl_working_list[idx]
            self._tpl_name_var.set(tpl.get('name', ''))
            self._tpl_font_var.set(tpl.get('font_family', ''))
            self._tpl_size_var.set(tpl.get('font_size', ''))
            self._tpl_color_var.set(tpl.get('color', ''))
            self._tpl_pos_var.set(tpl.get('position', 'before'))
            self._tpl_content_text.delete('1.0', tk.END)
            self._tpl_content_text.insert('1.0', tpl.get('content', ''))
            self._tpl_update_preview()
            self._tpl_update_content_style()
        finally:
            self._tpl_loading = False

    def _tpl_add(self):
        """新增空白模板"""
        existing_names = [t['name'] for t in self._tpl_working_list]
        base_name = "新模板"
        name = base_name
        n = 1
        while name in existing_names:
            n += 1
            name = f"{base_name}{n}"

        new_tpl = {'name': name, 'content': '', 'color': '',
                   'font_family': '', 'font_size': '', 'position': 'before'}
        self._tpl_working_list.append(new_tpl)
        self._tpl_listbox.insert(tk.END, name)
        new_idx = self._tpl_listbox.size() - 1
        self._tpl_listbox.selection_clear(0, tk.END)
        self._tpl_listbox.selection_set(new_idx)
        self._tpl_listbox.see(new_idx)
        # 直接設定 current_idx 確保可靠
        self._tpl_current_idx = new_idx
        self._tpl_on_select()

    def _tpl_delete(self):
        """刪除選中的模板 (即時生效, 不需要保存設定)"""
        sel = self._tpl_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "請先選擇要刪除的模板")
            return
        idx = sel[0]
        name = self._tpl_working_list[idx]['name']
        if not messagebox.askyesno("確認刪除", f"確定要刪除模板「{name}」嗎？"):
            return
        self._tpl_loading = True
        try:
            self._tpl_working_list.pop(idx)
            self._tpl_listbox.delete(idx)
            self._tpl_current_idx = None
            self._tpl_name_var.set('')
            self._tpl_font_var.set('')
            self._tpl_size_var.set('')
            self._tpl_color_var.set('')
            self._tpl_pos_var.set('before')
            self._tpl_content_text.delete('1.0', tk.END)
        finally:
            self._tpl_loading = False
        # 即時保存到磁碟
        self.config.setdefault('desc_templates', {})['templates'] = \
            copy.deepcopy(self._tpl_working_list)
        save_config(self.config)
        self._refresh_template_combo()

    def _tpl_auto_save(self):
        """自動保存當前編輯區到工作列表 (每次欄位變更時觸發)
        - 如果沒有選中任何模板但輸入了名稱 → 自動新增
        - 如果名稱重複 → 自動替換已有的
        - 否則 → 正常更新當前選中的模板
        """
        if getattr(self, '_tpl_loading', False):
            return
        if not hasattr(self, '_tpl_content_text'):
            return

        name = self._tpl_name_var.get().strip()
        if not name:
            return

        idx = getattr(self, '_tpl_current_idx', None)

        # 收集當前欄位資料
        content = self._tpl_content_text.get('1.0', tk.END).rstrip('\n')
        tpl_data = {
            'name': name,
            'content': content,
            'color': self._tpl_color_var.get().strip(),
            'font_family': self._tpl_font_var.get().strip(),
            'font_size': self._tpl_size_var.get().strip(),
            'position': self._tpl_pos_var.get(),
        }

        # ── 情況1: 沒有選中模板 → 自動新增或更新同名模板 ──
        if idx is None or idx >= len(self._tpl_working_list):
            # 先找有沒有同名的
            for i, t in enumerate(self._tpl_working_list):
                if t['name'] == name:
                    self._tpl_working_list[i] = tpl_data
                    self._tpl_current_idx = i
                    self._tpl_loading = True
                    try:
                        self._tpl_listbox.selection_clear(0, tk.END)
                        self._tpl_listbox.selection_set(i)
                    finally:
                        self._tpl_loading = False
                    return
            # 全新模板 → 自動建立
            self._tpl_working_list.append(tpl_data)
            self._tpl_loading = True
            try:
                self._tpl_listbox.insert(tk.END, name)
                new_idx = self._tpl_listbox.size() - 1
                self._tpl_listbox.selection_clear(0, tk.END)
                self._tpl_listbox.selection_set(new_idx)
                self._tpl_current_idx = new_idx
            finally:
                self._tpl_loading = False
            return

        # ── 情況2: 名稱重複 → 自動替換已有的 ──
        for i, t in enumerate(self._tpl_working_list):
            if i != idx and t['name'] == name:
                self._tpl_loading = True
                try:
                    self._tpl_working_list[i] = tpl_data
                    self._tpl_working_list.pop(idx)
                    self._tpl_listbox.delete(0, tk.END)
                    for t in self._tpl_working_list:
                        self._tpl_listbox.insert(tk.END, t['name'])
                    new_idx = i if i < idx else i - 1
                    self._tpl_current_idx = new_idx
                    self._tpl_listbox.selection_set(new_idx)
                finally:
                    self._tpl_loading = False
                return

        # ── 情況3: 正常更新 ──
        self._tpl_working_list[idx] = tpl_data
        old_name = self._tpl_listbox.get(idx)
        if old_name != name:
            self._tpl_loading = True
            try:
                self._tpl_listbox.delete(idx)
                self._tpl_listbox.insert(idx, name)
                self._tpl_listbox.selection_set(idx)
            finally:
                self._tpl_loading = False

    def _tpl_pick_color(self):
        """打開顏色選擇器"""
        initial = self._tpl_color_var.get().strip() or '#000000'
        try:
            result = colorchooser.askcolor(color=initial, title="選擇模板文字顏色")
        except Exception:
            result = colorchooser.askcolor(title="選擇模板文字顏色")
        if result and result[1]:
            self._tpl_color_var.set(result[1])

    def _tpl_update_preview(self):
        """更新顏色預覽方塊 + 內容編輯區樣式"""
        color = self._tpl_color_var.get().strip()
        if color:
            try:
                self._tpl_color_preview.configure(fg=color, text="  ■■  ")
            except tk.TclError:
                self._tpl_color_preview.configure(fg='black', text="  ??  ")
        else:
            self._tpl_color_preview.configure(fg='gray', text="  --  ")
        self._tpl_update_content_style()

    def _tpl_update_content_style(self):
        """更新模板內容編輯區的字體和顏色, 實時預覽效果"""
        if not hasattr(self, '_tpl_content_text'):
            return
        # 字體
        font_family = self._tpl_font_var.get().strip() or FONT
        try:
            font_size = int(self._tpl_size_var.get())
            if font_size < 6:
                font_size = 10
        except (ValueError, TypeError):
            font_size = 10
        try:
            self._tpl_content_text.configure(font=(font_family, font_size))
        except tk.TclError:
            self._tpl_content_text.configure(font=(FONT, font_size))
        # 顏色
        color = self._tpl_color_var.get().strip()
        if color:
            try:
                self._tpl_content_text.configure(fg=color)
            except tk.TclError:
                self._tpl_content_text.configure(fg='#333')
        else:
            self._tpl_content_text.configure(fg='#333')

    # ══════════════════ 工具 ══════════════════

    def _sync_config_from_ui(self):
        """UI變量→config dict"""
        self.config['source_type'] = self.source_var.get()
        self.config['last_input_file'] = self.file_var.get()
        self.config['process_mode'] = self.mode_var.get()
        self.config['output_dir'] = self.out_dir_var.get()
        self.config['output_name'] = self.out_name_var.get().strip() or 'test'
        self.config['output_conflict'] = self.conflict_var.get()
        self.config['tg_id'] = self.tg_id_var.get().strip()
        for key, var in self.step_vars.items():
            self.config.setdefault('steps', {}).setdefault(key, {})['enabled'] = var.get()
        self.config.setdefault('desc_templates', {})['selected'] = self.template_var.get()

    def _on_tg_id_change(self):
        """tg_id 變更 → 同步 config + 重查配額 (debounce)"""
        # 立即鎖定貴功能 (空 ID = 鎖死), 不等 800ms 查回來
        if not self.tg_id_var.get().strip():
            self._update_expensive_lockout(None)
        if hasattr(self, '_tg_after_id'):
            try: self.root.after_cancel(self._tg_after_id)
            except Exception: pass
        self._tg_after_id = self.root.after(800, self._refresh_quota)

    def _update_expensive_lockout(self, quota_info):
        """根據配額狀態鎖/解鎖 3 個貴功能 checkbox.

        鎖死條件 (任一):
            tg_id 空著
            quota.limit == 0  (admin 還沒給額度)
            quota.remaining == 0  (今日已用滿)
        否則解鎖.

        ★ 4.0.16: 鎖死時除了 disable 還要取消勾選 (var.set(False)),
          否則用戶按「開始處理」, pipeline 仍以為要跑貴功能, 觸發 QuotaExceeded crash.
          (解鎖時不自動勾回, 用戶想勾自己勾)
        ★ 4.0.33 race condition 修: 跑批中 GUI 不該改 config — 配額預扣到 0 時
          GUI 看 remaining=0 觸發此函數, 改 self.config['steps'] 結果 pipeline thread
          後面 step (Step K0/K) 讀 config 看到 enabled=False → 跳過. 配額已扣但圖片
          沒處理 — 用戶吃虧. 跑批中此函數直接 early-return 不動 config / var.
        """
        # ★ 4.0.33: 跑批中 (btn_stop NORMAL = pipeline 在跑) → 不動 config / var
        try:
            if str(self.btn_stop['state']) == 'normal':
                return
        except Exception:
            pass

        tg_id = self.tg_id_var.get().strip()
        if not tg_id:
            new_state = tk.DISABLED
            reason = '需填 TG ID'
        elif quota_info and quota_info.get('limit', -1) == 0:
            new_state = tk.DISABLED
            reason = '額度為 0, 請聯繫管理員或 /apply 申請'
        elif quota_info and quota_info.get('remaining', -1) == 0:
            new_state = tk.DISABLED
            reason = f"今日已用滿 ({quota_info.get('used','?')}/{quota_info.get('limit','?')}), 等 10:00 重置"
        else:
            new_state = tk.NORMAL
            reason = ''

        # ★ 取消勾選 (only on lock — 解鎖時不動 var, 讓用戶自決)
        # ★ 4.0.48: 三合一後 GUI 只一個 cb (_expensive), 鎖死它即可; 內部 step_vars 三個一起降
        unchecked_any = []
        cb = self._step_checkbuttons.get('_expensive')
        if cb:
            try: cb.configure(state=new_state)
            except Exception: pass
        if new_state == tk.DISABLED:
            # 透過合成 var 一鍵 unset → trace 同步三個 internal var
            if hasattr(self, 'expensive_var') and self.expensive_var.get():
                self.expensive_var.set(False)
            # 兼容 + 收 unchecked_any 給下面 save_config / 提示用
            for key in EXPENSIVE_STEP_KEYS:
                var = self.step_vars.get(key)
                if var is not None and var.get():
                    var.set(False)
                    unchecked_any.append(key)
                    unchecked_any.append(key)

        # 取消的時候提示用戶, 並把改寫進 config (持久化, 不然下次啟動還勾著)
        if unchecked_any:
            try:
                # 同步到 config['steps']
                steps_cfg = self.config.setdefault('steps', {})
                for key in unchecked_any:
                    steps_cfg.setdefault(key, {})['enabled'] = False
                save_config(self.config)
            except Exception:
                pass
            try:
                # 在處理日誌印一行提示, 用戶看得到
                names = {
                    'v65_stage_e': 'Stage E',
                    'image_dewatermark': '去水印救援',
                    'image_opt': '首圖 AI 優化',
                }
                self._log_msg(
                    f"[配額] 自動取消勾選 ({reason}): "
                    + ', '.join(names.get(k, k) for k in unchecked_any)
                    + ' — 仍可跑基本流程 (分類/價格/關鍵詞/V65 Stage A-D/標題去重 等), 不需配額.'
                )
            except Exception:
                pass

        # 在配額狀態列旁顯示鎖死原因
        if hasattr(self, '_quota_label') and reason:
            current = self.quota_status_var.get()
            if '🔒' not in current:
                self.quota_status_var.set(f"{current}  🔒 {reason}")

    def _check_new_version_async(self):
        """★ 4.0.66: 背景 check 雲端新版, 有新版在標題列提醒 (因為 start.bat 只啟動時拉,
        啟動後雲端推新版 → 不重啟用不到. 此 thread 每 10 分鐘 check 一次).
        """
        import threading as _th

        def _qlog(msg):
            """寫到 GUI 日誌區 (用戶看得到)"""
            try:
                self.log_queue.put(msg)
            except Exception:
                pass

        def worker():
            try:
                import json as _json
                import urllib.request as _req
                import urllib.error as _uerr
                # 1. 讀本地 current_version.txt
                local_ver = ''
                here = os.path.dirname(os.path.abspath(__file__))
                vp = os.path.join(here, 'current_version.txt')
                try:
                    if os.path.isfile(vp):
                        with open(vp, 'r', encoding='utf-8') as f:
                            local_ver = f.read().strip()
                except Exception as _e:
                    _qlog(f'[版本檢查] 讀 current_version.txt 失敗: {_e}')
                if not local_ver:
                    _qlog(f'[版本檢查] 找不到 current_version.txt ({vp}), 跳過 check')
                    return
                # 2. 讀 update_config.json (跟 updater 用同一份)
                cfg_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    '_updater', 'update_config.json'
                )
                if not os.path.isfile(cfg_path):
                    _qlog(f'[版本檢查] update_config.json 不存在 ({cfg_path}), 跳過')
                    return
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = _json.load(f)
                worker_url = cfg.get('worker_url', '')
                read_key = cfg.get('read_key', '')
                if not worker_url or not read_key:
                    _qlog(f'[版本檢查] update_config 缺 worker_url/read_key, 跳過')
                    return
                # 3. GET /update/check?app=product_hub
                url = f'{worker_url}/update/check?app=product_hub'
                req = _req.Request(url, headers={
                    'Authorization': f'Bearer {read_key}',
                    'User-Agent': 'Mozilla/5.0 toolkit-inapp-check/4.0',
                })
                try:
                    with _req.urlopen(req, timeout=10) as r:
                        if r.status != 200:
                            _qlog(f'[版本檢查] HTTP {r.status}, 跳過')
                            return
                        body = r.read().decode('utf-8', errors='ignore')
                except (_uerr.URLError, Exception) as _e:
                    _qlog(f'[版本檢查] 網路錯: {type(_e).__name__}, 跳過')
                    return
                try:
                    manifest = _json.loads(body)
                except Exception:
                    _qlog(f'[版本檢查] 回非 JSON, 跳過')
                    return
                cloud_ver = str(manifest.get('version', '')).strip()
                if not cloud_ver:
                    _qlog(f'[版本檢查] manifest 缺 version, 跳過')
                    return
                # 4. 比版本 (semver-ish 比較: 4.0.66 vs 4.0.65)
                def _ver_tuple(v):
                    try:
                        return tuple(int(x) for x in v.split('.'))
                    except Exception:
                        return (0,)
                if _ver_tuple(cloud_ver) > _ver_tuple(local_ver):
                    _qlog(f'[版本檢查] ⚠ 有新版! 本地 {local_ver} → 雲端 {cloud_ver}, 請關閉軟件重開')
                    self.root.after(0, lambda: self._set_new_version_notice(cloud_ver, local_ver))
                else:
                    _qlog(f'[版本檢查] 已是最新版 {local_ver} (雲端 {cloud_ver})')
            except Exception as _e:
                _qlog(f'[版本檢查] 內部錯: {type(_e).__name__}: {str(_e)[:80]}')

        try:
            t = _th.Thread(target=worker, daemon=True, name='inapp-version-check')
            t.start()
        except Exception:
            pass
        # 排下次 check (10 分鐘)
        self.root.after(10 * 60 * 1000, self._check_new_version_async)

    def _set_new_version_notice(self, cloud_ver: str, local_ver: str):
        """在標題列右側顯示「⚠ 有新版 X.Y.Z (請關閉重開)」"""
        try:
            self.root.title(f'商品處理中樞 v{VERSION}    ⚠ 有新版 {cloud_ver} (目前 {local_ver}, 請關閉軟件重開以套用)')
        except Exception:
            pass

    def _refresh_quota(self):
        """從 Worker 查當前配額狀態, 顯示在 quota_status_var, 並依結果鎖/解鎖貴功能"""
        tg_id = self.tg_id_var.get().strip()
        if not tg_id:
            self.quota_status_var.set('配額: (未填 TG ID — 貴功能鎖死)')
            self._quota_label.configure(foreground='red')
            self._update_expensive_lockout(None)
            return
        # 寫入 config 並立刻持久化 (使用者填一次, 永遠記住)
        if self.config.get('tg_id') != tg_id:
            self.config['tg_id'] = tg_id
            try:
                save_config(self.config)
            except Exception:
                pass

        def worker():
            try:
                from quota_client import QuotaClient
                qc = QuotaClient(self.config)
                if not qc.is_active():
                    self.root.after(0, lambda: (
                        self.quota_status_var.set('配額: 已關閉 (mode=off)'),
                        self._update_expensive_lockout(None)))
                    return
                status = qc.check()
                if status is None:
                    self.root.after(0, lambda: (
                        self.quota_status_var.set('配額: 配置不完整'),
                        self._update_expensive_lockout(None)))
                    return
                used = status.get('used', '?')
                limit = status.get('limit', '?')
                rem = status.get('remaining', '?')
                msg = f'配額: 今日已用 {used}/{limit} (剩 {rem} 件)'
                color = '#16a34a' if isinstance(rem, int) and rem > 0 else '#be123c'
                self.root.after(0, lambda m=msg, c=color, s=status: (
                    self.quota_status_var.set(m),
                    self._quota_label.configure(foreground=c),
                    self._update_expensive_lockout(s)))
            except Exception as e:
                self.root.after(0, lambda er=e: (
                    self.quota_status_var.set(f'配額: 查詢失敗 ({er})'),
                    self._update_expensive_lockout(None)))
        threading.Thread(target=worker, daemon=True).start()

    def _open_output_dir(self):
        out_dir = self.config.get('output_dir', '')
        if not out_dir:
            fpath = self.file_var.get().strip()
            if fpath:
                out_dir = os.path.join(os.path.dirname(fpath), 'output')
            else:
                out_dir = str(BASE_DIR / 'output')
        if os.path.isdir(out_dir):
            os.startfile(out_dir)
        else:
            messagebox.showinfo("提示", f"輸出目錄不存在: {out_dir}")

    def _open_detail_log(self):
        """找 output_dir 下最新的詳細日誌_*.txt 用記事本開啟"""
        out_dir = self.config.get('output_dir', '')
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showinfo("提示", f"輸出目錄不存在: {out_dir}")
            return
        # 找最新的詳細日誌
        import glob
        logs = glob.glob(os.path.join(out_dir, '詳細日誌_*.txt'))
        if not logs:
            messagebox.showinfo("提示", f"找不到詳細日誌檔 (跑一次後才會生成)\n位置: {out_dir}")
            return
        latest = max(logs, key=os.path.getmtime)
        try:
            os.startfile(latest)
        except Exception as e:
            messagebox.showerror("錯誤", f"開啟失敗: {e}")

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == '__main__':
    main()
