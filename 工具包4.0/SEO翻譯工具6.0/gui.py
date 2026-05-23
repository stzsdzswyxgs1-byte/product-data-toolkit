# -*- coding: utf-8 -*-
"""日轉繁 SEO 翻譯工具 — GUI（進度優化版）"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from translator import (
    CFG, KATAKANA_TO_LATIN,
    load_first_sheet, resolve_column_name, translate_dataframe,
    load_katakana_map, TITLE_SYNONYMS, DESC_SYNONYMS,
    quality_analyze, _ProgressSaved, load_progress, clear_progress,
)


# ══════════════════ 階段定義 ══════════════════
STAGES = [
    ("translate", "翻譯"),
    ("kana",      "假名清除"),
    ("keyword",   "關鍵詞提取"),
    ("seo",       "SEO優化"),
    ("quality",   "品質篩選"),
    ("save",      "保存檔案"),
]

# 各階段佔整體進度的權重（翻譯最重）
STAGE_WEIGHTS = {
    "translate": 55,
    "kana":      5,
    "keyword":   15,
    "seo":       15,
    "quality":   5,
    "save":      5,
}


class TranslatorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("日轉繁 SEO 翻譯工具 v3")
        self.root.geometry("960x780")
        self.root.minsize(860, 680)

        self.log_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None

        # --- Variables ---
        self.file_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.api_url_var = tk.StringVar(value="https://api.example.com/v1")
        self.api_key_var = tk.StringVar(value="<TEST_API_KEY>")
        self.model_var = tk.StringVar(value="gpt-5.4-mini")
        self.seo_model_var = tk.StringVar(value="gpt-5.5")
        self.workers_var = tk.StringVar(value="30")
        self.batch_var = tk.StringVar(value="15")
        self.timeout_var = tk.StringVar(value="180")
        self.kana_cleanup_var = tk.BooleanVar(value=True)
        self.seo_var = tk.BooleanVar(value=True)
        self.translate_var = tk.BooleanVar(value=True)
        self.quality_filter_var = tk.BooleanVar(value=True)
        self.filter_words_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="就緒")
        self.progress_var = tk.DoubleVar(value=0.0)

        # --- 進度追蹤 ---
        self._stage_labels = {}       # stage_key -> tk.Label
        self._stage_status = {}       # stage_key -> "pending" | "running" | "done" | "skipped"
        self._stage_status_lock = threading.Lock()
        self._overall_start = 0
        self._stage_start_time = 0
        self._current_stage = ""
        self._stats = {"total": 0, "cached": 0, "api": 0, "elapsed": 0}

        self._build_layout()
        self._load_saved_config()
        self._poll_log()

    # ══════════════════ 界面佈局 ══════════════════
    def _build_layout(self):
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)

        # ── 檔案選擇 ──
        file_frame = ttk.LabelFrame(container, text="檔案", padding=8)
        file_frame.pack(fill="x")

        ttk.Label(file_frame, text="輸入檔案").grid(row=0, column=0, sticky="w")
        ttk.Entry(file_frame, textvariable=self.file_var, width=60).grid(
            row=0, column=1, sticky="we", padx=6)
        ttk.Button(file_frame, text="瀏覽...", command=self._browse_input).grid(
            row=0, column=2, padx=4)

        ttk.Label(file_frame, text="輸出路徑").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(file_frame, textvariable=self.out_var, width=60).grid(
            row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(file_frame, text="瀏覽...", command=self._browse_output).grid(
            row=1, column=2, padx=4, pady=(6, 0))
        file_frame.columnconfigure(1, weight=1)

        # ── 模型設定 ──
        model_frame = ttk.LabelFrame(container, text="模型設定", padding=8)
        model_frame.pack(fill="x", pady=(8, 0))

        ttk.Label(model_frame, text="翻譯模型").grid(row=0, column=0, sticky="w")
        ttk.Entry(model_frame, textvariable=self.model_var, width=20).grid(
            row=0, column=1, sticky="w", padx=6)

        ttk.Label(model_frame, text="SEO模型").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Entry(model_frame, textvariable=self.seo_model_var, width=20).grid(
            row=0, column=3, sticky="w", padx=6)

        # ── 翻譯參數 ──
        param_frame = ttk.LabelFrame(container, text="翻譯參數", padding=8)
        param_frame.pack(fill="x", pady=(8, 0))

        ttk.Label(param_frame, text="線程數").grid(row=0, column=0, sticky="w")
        ttk.Entry(param_frame, textvariable=self.workers_var, width=6).grid(
            row=0, column=1, sticky="w", padx=6)

        ttk.Label(param_frame, text="批次大小").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Entry(param_frame, textvariable=self.batch_var, width=6).grid(
            row=0, column=3, sticky="w", padx=6)

        ttk.Label(param_frame, text="超時(秒)").grid(row=0, column=4, sticky="w", padx=(16, 0))
        ttk.Entry(param_frame, textvariable=self.timeout_var, width=6).grid(
            row=0, column=5, sticky="w", padx=6)

        ttk.Checkbutton(param_frame, text="啟用正文翻譯",
                        variable=self.translate_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(param_frame, text="將日文內容翻成繁體中文，並清除殘留日文",
                  foreground="gray").grid(
            row=1, column=2, columnspan=4, sticky="w", padx=(0, 12), pady=(8, 0))

        ttk.Checkbutton(param_frame, text="SEO 標題優化",
                        variable=self.seo_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(param_frame, text="優化標題，提升搜尋排名",
                  foreground="gray").grid(
            row=2, column=2, columnspan=4, sticky="w", pady=(4, 0))

        # ── 品質篩選 ──
        filter_frame = ttk.LabelFrame(container, text="品質篩選（自動檢查問題商品）", padding=6)
        filter_frame.pack(fill="x", pady=(6, 0))

        ttk.Checkbutton(filter_frame, text="啟用品質篩選",
                        variable=self.quality_filter_var).grid(
            row=0, column=0, sticky="w")
        ttk.Label(filter_frame, text="自動檢查重複、低分、瑕疵或命中過濾詞的商品，分到對應分頁",
                  foreground="gray").grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(filter_frame, text="自訂過濾詞").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self._filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_words_var, width=50)
        self._filter_entry.grid(row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        filter_frame.columnconfigure(1, weight=1)

        self._filter_placeholder = "例如：測試 勿拍,專拍，補差價"
        if not self.filter_words_var.get():
            self._filter_entry.insert(0, self._filter_placeholder)
            self._filter_entry.configure(foreground="gray")
        self._filter_entry.bind("<FocusIn>", self._filter_focus_in)
        self._filter_entry.bind("<FocusOut>", self._filter_focus_out)

        ttk.Label(filter_frame,
                  text="填入不希望出現在標題裡的詞，命中後標記為需注意。多個詞可用空格、, 或 ， 分隔",
                  foreground="gray").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # ── 按鈕列 ──
        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill="x", pady=(10, 0))

        self.run_btn = ttk.Button(btn_frame, text="開始翻譯", command=self._run)
        self.run_btn.pack(side="left")

        self.resume_btn = ttk.Button(btn_frame, text="繼續上次進度", command=self._resume)
        self.resume_btn.pack(side="left", padx=(10, 0))

        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 0))

        self.open_btn = ttk.Button(btn_frame, text="開啟輸出檔", command=self._open_output)
        self.open_btn.pack(side="left", padx=(10, 0))

        # 右側實時計時器
        self._timer_var = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self._timer_var,
                  font=("Consolas", 10, "bold")).pack(side="right")

        # ── 進度區域 ──
        progress_outer = ttk.LabelFrame(container, text="處理進度", padding=8)
        progress_outer.pack(fill="x", pady=(8, 0))

        # 整體進度條
        bar_frame = ttk.Frame(progress_outer)
        bar_frame.pack(fill="x")
        self._progress_label = ttk.Label(bar_frame, text="0%", width=5, anchor="e")
        self._progress_label.pack(side="right", padx=(6, 0))
        self.progress_bar = ttk.Progressbar(
            bar_frame, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x", expand=True)

        # 階段指示器（水平排列）
        stage_frame = ttk.Frame(progress_outer)
        stage_frame.pack(fill="x", pady=(6, 0))

        for i, (key, name) in enumerate(STAGES):
            lbl = ttk.Label(stage_frame, text=f"  {name}", foreground="gray",
                            font=("Microsoft JhengHei UI", 9))
            lbl.grid(row=0, column=i, padx=(0, 12), sticky="w")
            self._stage_labels[key] = lbl
            self._stage_status[key] = "pending"

        # 統計信息行
        self._stats_var = tk.StringVar(value="")
        ttk.Label(progress_outer, textvariable=self._stats_var,
                  foreground="#555", font=("Consolas", 9)).pack(fill="x", pady=(4, 0))

        # ── 日誌 ──
        log_frame = ttk.LabelFrame(container, text="執行日誌", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap="word",
                                                   font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        # ── 狀態列 ──
        status_bar = ttk.Label(container, textvariable=self.status_var, anchor="w",
                               relief="sunken", padding=(4, 2))
        status_bar.pack(fill="x", pady=(6, 0))

    # ══════════════════ 階段進度管理 ══════════════════
    def _reset_stages(self):
        """重置所有階段到待處理狀態"""
        for key in self._stage_status:
            self._stage_status[key] = "pending"
            self._stage_labels[key].configure(foreground="gray")
            self._stage_labels[key].configure(text=f"  {dict(STAGES)[key]}")
        self.progress_var.set(0)
        self._progress_label.configure(text="0%")
        self._stats_var.set("")
        self._timer_var.set("")
        self._stats = {"total": 0, "cached": 0, "api": 0, "elapsed": 0}

    def _set_stage(self, stage_key: str, status: str):
        """更新階段狀態: running / done / skipped（線程安全）"""
        with self._stage_status_lock:
            self._stage_status[stage_key] = status
        self.log_queue.put(("__stage__", (stage_key, status)))

    def _apply_stage(self, stage_key: str, status: str):
        """在主線程中更新階段 UI"""
        name = dict(STAGES).get(stage_key, stage_key)
        self._stage_status[stage_key] = status
        lbl = self._stage_labels.get(stage_key)
        if not lbl:
            return
        if status == "running":
            lbl.configure(text=f">> {name}...", foreground="#0066CC",
                          font=("Microsoft JhengHei UI", 9, "bold"))
            self._current_stage = stage_key
            self._stage_start_time = time.time()
        elif status == "done":
            elapsed = time.time() - self._stage_start_time if self._stage_start_time else 0
            if elapsed >= 1:
                lbl.configure(text=f"[OK] {name} ({elapsed:.0f}s)", foreground="#008800",
                              font=("Microsoft JhengHei UI", 9))
            else:
                lbl.configure(text=f"[OK] {name}", foreground="#008800",
                              font=("Microsoft JhengHei UI", 9))
            # 更新整體進度
            self._update_overall_progress()
        elif status == "skipped":
            lbl.configure(text=f"[--] {name}", foreground="#999",
                          font=("Microsoft JhengHei UI", 9))
            self._update_overall_progress()

    def _update_overall_progress(self):
        """根據已完成的階段計算整體進度"""
        done_weight = 0
        total_weight = 0
        for key, _ in STAGES:
            w = STAGE_WEIGHTS.get(key, 5)
            total_weight += w
            if self._stage_status[key] in ("done", "skipped"):
                done_weight += w
        pct = done_weight / total_weight * 100 if total_weight > 0 else 0
        self.progress_var.set(pct)
        self._progress_label.configure(text=f"{pct:.0f}%")

    def _update_stage_progress(self, current, total, stage_name):
        """更新階段內的細粒度進度"""
        # 找到對應的 stage key
        stage_key = self._current_stage
        stage_weight = STAGE_WEIGHTS.get(stage_key, 5)

        # 計算已完成階段的累積權重
        done_weight = 0
        total_weight = sum(STAGE_WEIGHTS.get(k, 5) for k, _ in STAGES)
        for key, _ in STAGES:
            if key == stage_key:
                break
            if self._stage_status[key] in ("done", "skipped"):
                done_weight += STAGE_WEIGHTS.get(key, 5)

        # 當前階段的部分進度
        stage_pct = current / total if total > 0 else 0
        overall_pct = (done_weight + stage_weight * stage_pct) / total_weight * 100

        self.progress_var.set(overall_pct)
        self._progress_label.configure(text=f"{overall_pct:.0f}%")

        # ETA
        if self._stage_start_time and current > 0:
            elapsed = time.time() - self._stage_start_time
            eta = elapsed / current * (total - current)
            if eta >= 60:
                eta_str = f"剩餘 {int(eta//60)}分{int(eta%60)}秒"
            else:
                eta_str = f"剩餘 {int(eta)}秒"
            self.status_var.set(f"{stage_name} {current}/{total} | {eta_str}")
        else:
            self.status_var.set(f"{stage_name} {current}/{total}")

    def _update_timer(self):
        """更新右上角計時器"""
        if self._overall_start > 0 and self.worker_thread and self.worker_thread.is_alive():
            elapsed = time.time() - self._overall_start
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            self._timer_var.set(f"已用時 {mins:02d}:{secs:02d}")
            self.root.after(1000, self._update_timer)
        elif self._overall_start > 0:
            elapsed = time.time() - self._overall_start
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            self._timer_var.set(f"總耗時 {mins:02d}:{secs:02d}")

    def _update_stats_display(self):
        """更新統計信息行"""
        s = self._stats
        parts = []
        if s["total"]:
            parts.append(f"共 {s['total']} 行")
        if s["cached"]:
            parts.append(f"快取 {s['cached']}")
        if s["api"]:
            parts.append(f"API {s['api']}")
        if parts:
            self._stats_var.set("  |  ".join(parts))

    # ══════════════════ Placeholder 輔助 ══════════════════
    def _filter_focus_in(self, event):
        if self._filter_entry.get() == self._filter_placeholder:
            self._filter_entry.delete(0, "end")
            self._filter_entry.configure(foreground="")

    def _filter_focus_out(self, event):
        if not self._filter_entry.get().strip():
            self._filter_entry.delete(0, "end")
            self._filter_entry.insert(0, self._filter_placeholder)
            self._filter_entry.configure(foreground="gray")

    # ══════════════════ 檔案瀏覽 ══════════════════
    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="選擇 Excel / CSV 檔案",
            filetypes=[("Excel & CSV", "*.xlsx *.xls *.csv"), ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"), ("All", "*.*")]
        )
        if path:
            self.file_var.set(path)
            if not self.out_var.get():
                base, _ = os.path.splitext(path)
                self.out_var.set(f"{base}_繁體.xlsx")

    def _browse_output(self):
        input_path = self.file_var.get().strip()
        input_ext = os.path.splitext(input_path)[1].lower() if input_path else ""
        if input_ext == ".csv":
            path = filedialog.asksaveasfilename(
                title="儲存翻譯結果",
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv"), ("All", "*.*")]
            )
        else:
            path = filedialog.asksaveasfilename(
                title="儲存翻譯結果",
                defaultextension=".xlsx",
                filetypes=[("Excel", "*.xlsx"), ("All", "*.*")]
            )
        if path:
            self.out_var.set(path)

    # ══════════════════ 設定持久化 ══════════════════
    def _config_path(self) -> str:
        import sys as _sys
        if getattr(_sys, 'frozen', False):
            return os.path.join(os.path.dirname(_sys.executable), "translator_config.json")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "translator_config.json")

    def _load_saved_config(self):
        import json
        path = self._config_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.api_url_var.set(cfg.get("api_url", self.api_url_var.get()))
            self.api_key_var.set(cfg.get("api_key", ""))
            self.model_var.set(cfg.get("model", self.model_var.get()))
            self.seo_model_var.set(cfg.get("seo_model", self.seo_model_var.get()))
            self.workers_var.set(str(cfg.get("workers", 15)))
            self.batch_var.set(str(cfg.get("batch_size", 30)))
            self.timeout_var.set(str(cfg.get("timeout", 180)))
            self.kana_cleanup_var.set(cfg.get("kana_cleanup", True))
            self.seo_var.set(cfg.get("seo", True))
            self.translate_var.set(cfg.get("enable_translate", True))
            self.quality_filter_var.set(cfg.get("quality_filter", True))
            self.filter_words_var.set(cfg.get("filter_words", ""))
        except Exception:
            pass

    def _save_config(self):
        import json
        existing = {}
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
        cfg = {
            "api_url": existing.get("api_url", self.api_url_var.get().strip()),
            "api_key": existing.get("api_key", self.api_key_var.get().strip()),
            "model": self.model_var.get().strip(),
            "seo_model": self.seo_model_var.get().strip(),
            "workers": int(self.workers_var.get() or 15),
            "batch_size": int(self.batch_var.get() or 30),
            "timeout": int(self.timeout_var.get() or 180),
            "kana_cleanup": self.kana_cleanup_var.get(),
            "seo": self.seo_var.get(),
            "enable_translate": self.translate_var.get(),
            "quality_filter": self.quality_filter_var.get(),
            "filter_words": "" if self.filter_words_var.get().strip() == self._filter_placeholder else self.filter_words_var.get().strip(),
            "last_input": self.file_var.get().strip(),
            "last_output": self.out_var.get().strip(),
        }
        try:
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ══════════════════ 日誌 ══════════════════
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(msg, tuple):
                event, payload = msg
                if event == "__done__":
                    self._set_running(False)
                elif event == "__error__":
                    messagebox.showerror("錯誤", str(payload))
                    self._set_running(False)
                elif event == "__stage__":
                    stage_key, status = payload
                    self._apply_stage(stage_key, status)
                elif event == "__progress__":
                    cur, total, stage_name = payload
                    self._update_stage_progress(cur, total, stage_name)
                elif event == "__stats__":
                    self._stats.update(payload)
                    self._update_stats_display()
                elif event == "__status__":
                    self.status_var.set(payload)
                elif event == "__ask_user__":
                    question, options, result_holder, answer_event = payload
                    answer = messagebox.askyesno("SEO 處理中斷", question)
                    result_holder["answer"] = answer
                    answer_event.set()
                continue
            self._append_log(msg)
        self.root.after(100, self._poll_log)

    # ══════════════════ 控制 ══════════════════
    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.run_btn.configure(state=state)
        self.resume_btn.configure(state=state)
        self.open_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _stop(self):
        self.stop_event.set()
        self.status_var.set("停止中...")
        self._log("使用者要求停止...")

    def _resume(self):
        """繼續上次保存的進度，自動從配置讀取上次的輸入/輸出路徑"""
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("執行中", "翻譯任務正在進行中")
            return

        # 嘗試從多個來源找到進度文件
        # 1. 當前已填的輸出路徑
        # 2. 配置文件裡的上次路徑
        out_path = self.out_var.get().strip()
        input_path = self.file_var.get().strip()
        prog = None

        if out_path:
            prog = load_progress(out_path)

        if not prog:
            # 從配置讀取上次的路徑
            import json
            try:
                with open(self._config_path(), "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                last_out = cfg.get("last_output", "")
                last_in = cfg.get("last_input", "")
                if last_out:
                    prog = load_progress(last_out)
                    if prog:
                        out_path = last_out
                        input_path = last_in
            except Exception:
                pass

        if not prog:
            messagebox.showinfo("無進度", "未找到保存的進度文件。\n請先使用「開始翻譯」執行一次。")
            return

        # 驗證輸入文件是否還存在
        if not input_path or not os.path.exists(input_path):
            messagebox.showerror("找不到原始檔案",
                f"進度文件需要原始輸入檔案，但找不到：\n{input_path}\n\n"
                f"請確認檔案未被移動或改名。")
            return

        completed = len(prog.get("responded_keys", set()))
        total = prog.get("row_count", 0)
        ans = messagebox.askyesno(
            "繼續上次進度",
            f"找到上次的進度：\n"
            f"檔案：{os.path.basename(input_path)}\n"
            f"SEO 已完成：{completed}/{total} 條\n\n"
            f"要繼續處理嗎？"
        )
        if not ans:
            return

        # 自動填入路徑
        self.file_var.set(input_path)
        self.out_var.set(out_path)
        # 啟動（_run 裡會偵測到進度文件，自動選擇繼續）
        self._run()

    def _open_output(self):
        path = self.out_var.get().strip()
        if path and os.path.exists(path):
            try:
                os.startfile(path)
            except Exception as e:
                messagebox.showerror("開啟失敗", str(e))
        else:
            messagebox.showwarning("檔案不存在", "輸出檔案尚未產生，請先執行翻譯。")

    # ══════════════════ 翻譯執行 ══════════════════
    def _run(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("執行中", "翻譯任務正在進行中")
            return

        input_path = self.file_var.get().strip()
        if not input_path or not os.path.exists(input_path):
            messagebox.showerror("錯誤", "請選擇有效的 Excel / CSV 檔案")
            return

        out_path = self.out_var.get().strip()
        if not out_path:
            base, _ = os.path.splitext(input_path)
            out_path = f"{base}_繁體.xlsx"
            self.out_var.set(out_path)

        # 檢查是否有上次保存的進度
        prog = load_progress(out_path)
        if prog:
            completed = len(prog.get("responded_keys", set()))
            total = prog.get("row_count", 0)
            ans = messagebox.askyesnocancel(
                "偵測到上次進度",
                f"上次 SEO 進度：{completed}/{total} 條已完成\n\n"
                f"【是】從上次進度繼續（推薦）\n"
                f"【否】清除進度，重新開始\n"
                f"【取消】不執行"
            )
            if ans is None:
                return  # 取消
            if ans is False:
                clear_progress(out_path)  # 重新開始

        # 同步設定
        CFG.api_base = self.api_url_var.get().strip().rstrip("/")
        CFG.api_key = self.api_key_var.get().strip()
        CFG.model = self.model_var.get().strip()
        CFG.seo_model = self.seo_model_var.get().strip()
        try:
            CFG.workers = int(self.workers_var.get())
        except ValueError:
            CFG.workers = 15
        try:
            CFG.batch_size = int(self.batch_var.get())
        except ValueError:
            CFG.batch_size = 30
        try:
            CFG.timeout = int(self.timeout_var.get())
        except ValueError:
            CFG.timeout = 180
        CFG.enable_kana_cleanup = self.kana_cleanup_var.get()
        CFG.enable_seo = self.seo_var.get()
        CFG.enable_translate = self.translate_var.get()

        self._save_config()
        self.stop_event.clear()
        self._set_running(True)
        self._reset_stages()
        self._overall_start = time.time()

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self._log(f"輸入：{input_path}")
        self._log(f"輸出：{out_path}")
        self._log(f"模型：{CFG.model} | SEO：{CFG.seo_model or CFG.model} | 線程：{CFG.workers} | 批次：{CFG.batch_size}")
        self._log(f"翻譯：{'ON' if CFG.enable_translate else 'OFF'} | SEO：{'ON' if CFG.enable_seo else 'OFF'} | 品質篩選：{'ON' if self.quality_filter_var.get() else 'OFF'}")

        self.status_var.set("啟動中...")

        self.worker_thread = threading.Thread(
            target=self._worker_main,
            args=(input_path, out_path),
            daemon=True,
        )
        self.worker_thread.start()
        self._update_timer()

    def _worker_main(self, input_path: str, out_path: str):
        try:
            t0 = time.time()

            # ── 讀取檔案 ──
            self._log("讀取檔案...")
            df = load_first_sheet(input_path)
            self._log(f"讀取完成：{len(df)} 行")
            self.log_queue.put(("__stats__", {"total": len(df)}))

            try:
                tcol = resolve_column_name(df, "標題", TITLE_SYNONYMS)
            except KeyError as e:
                self.log_queue.put(("__error__", f"找不到標題欄：{e}"))
                return
            try:
                dcol = resolve_column_name(df, "說明", DESC_SYNONYMS)
            except KeyError as e:
                self.log_queue.put(("__error__", f"找不到說明欄：{e}"))
                return

            self._log(f"標題欄：{tcol} | 說明欄：{dcol}")

            # ── 設定不啟用的階段為 skipped ──
            if not CFG.enable_translate:
                self._set_stage("translate", "skipped")
                self._set_stage("kana", "skipped")
            if not CFG.enable_seo:
                self._set_stage("keyword", "skipped")
                self._set_stage("seo", "skipped")
            if not self.quality_filter_var.get():
                self._set_stage("quality", "skipped")

            # ── progress callback（帶階段映射）──
            _stage_map = {
                "翻譯": "translate",
                "假名清除": "kana",
                "關鍵詞提取": "keyword",
                "SEO優化": "seo",
            }
            _active_stages = set()

            def progress_fn(current, total, stage_name):
                mapped = _stage_map.get(stage_name, "")
                # 自動偵測新階段開始
                if mapped and mapped not in _active_stages:
                    _active_stages.add(mapped)
                    # 把前一個 running 的標記為 done
                    with self._stage_status_lock:
                        running_keys = [k for k in _stage_map.values()
                                        if k != mapped and self._stage_status.get(k) == "running"]
                    for k in running_keys:
                        self._set_stage(k, "done")
                    self._set_stage(mapped, "running")
                self.log_queue.put(("__progress__", (current, total, stage_name)))

            def stop_check():
                return self.stop_event.is_set()

            def ask_user(question):
                """從 worker 線程向用戶提問，阻塞等待回答。返回 True=繼續等待重試, False=跳過此步驟"""
                result_holder = {"answer": False}
                answer_event = threading.Event()
                self.log_queue.put(("__ask_user__", (question, None, result_holder, answer_event)))
                answer_event.wait()  # 阻塞 worker 線程直到用戶回答
                return result_holder["answer"]

            # ── 開始翻譯 ──
            if CFG.enable_translate:
                self._set_stage("translate", "running")

            out = translate_dataframe(
                df, tcol, dcol,
                log_fn=self._log,
                progress_fn=progress_fn,
                stop_check=stop_check,
                ask_fn=ask_user,
                out_path=out_path,
            )

            # 收尾：把最後一個 running 階段標記為 done
            with self._stage_status_lock:
                running_keys = [k for k in _stage_map.values()
                                if self._stage_status.get(k) == "running"]
            for k in running_keys:
                self._set_stage(k, "done")

            # 假名清除在 translate_dataframe 內部執行，不會呼叫 progress_fn，
            # 需要手動更新狀態
            with self._stage_status_lock:
                kana_status = self._stage_status.get("kana", "pending")
            if kana_status == "pending":
                if CFG.enable_translate and self.kana_cleanup_var.get():
                    self._set_stage("kana", "done")
                else:
                    self._set_stage("kana", "skipped")

            if self.stop_event.is_set():
                self._log("已停止，保存已完成的部分...")
                try:
                    out_ext = os.path.splitext(out_path)[1].lower()
                    if out_ext == ".csv":
                        out.to_csv(out_path, index=False, encoding="utf-8-sig")
                    else:
                        out.to_excel(out_path, index=False)
                    self._log(f"部分結果已保存：{out_path}")
                except Exception as save_err:
                    self._log(f"保存失敗：{save_err}")
                self.status_var.set("已停止（部分結果已保存）")
                return

            # ── 品質篩選 ──
            self._set_stage("save", "running")
            out_ext = os.path.splitext(out_path)[1].lower()

            if self.quality_filter_var.get():
                self._set_stage("quality", "running")
                raw_filter = self.filter_words_var.get()
                if raw_filter.strip() == self._filter_placeholder:
                    raw_filter = ""
                custom_words = [w.strip() for w in re.split(r'[,\uFF0C\s]+', raw_filter) if w.strip()]
                good_df, warn_df, bad_df, dup_df = quality_analyze(
                    out, tcol, log_fn=self._log,
                    custom_filter_words=custom_words or None,
                )
                self._set_stage("quality", "done")

                internal_cols = ["_品質分數", "_品質標籤", "_問題說明"]
                good_clean = good_df.drop(columns=internal_cols, errors="ignore")
                warn_export = warn_df.copy()
                bad_export = bad_df.copy()
                dup_export = dup_df.drop(columns=["_品質分數", "_品質標籤"], errors="ignore") if len(dup_df) > 0 else dup_df

                if out_ext == ".csv":
                    good_clean.to_csv(out_path, index=False, encoding="utf-8-sig")
                    if len(bad_df) > 0 or len(warn_df) > 0:
                        problem_path = out_path.replace(".csv", "_問題商品.csv")
                        import pandas as _pd
                        _pd.concat([bad_export, warn_export]).to_csv(
                            problem_path, index=False, encoding="utf-8-sig")
                        self._log(f"問題商品：{problem_path}")
                else:
                    with __import__("pandas").ExcelWriter(out_path, engine="openpyxl") as writer:
                        good_clean.to_excel(writer, sheet_name="合格商品", index=False)
                        if len(warn_df) > 0:
                            warn_export.to_excel(writer, sheet_name="需注意", index=False)
                        if len(bad_df) > 0:
                            bad_export.to_excel(writer, sheet_name="問題商品", index=False)
                        if len(dup_df) > 0:
                            dup_export.to_excel(writer, sheet_name="重複商品", index=False)

                self._log(f"合格：{len(good_df)} | 注意：{len(warn_df)} | 問題：{len(bad_df)} | 重複：{len(dup_df)}")
            else:
                if out_ext == ".csv":
                    out.to_csv(out_path, index=False, encoding="utf-8-sig")
                else:
                    out.to_excel(out_path, index=False)

            self._set_stage("save", "done")

            elapsed = time.time() - t0
            self._log(f"全部完成！{len(df)} 行，耗時 {elapsed:.1f} 秒")
            self._log(f"已保存：{out_path}")
            self.log_queue.put(("__stats__", {"elapsed": elapsed}))
            self.log_queue.put(("__status__", f"完成 - {len(df)} 行, {elapsed:.1f}s"))

        except _ProgressSaved as ps:
            self._log(f"⏸ {ps}")
            self._log("進度已保存。下次開啟相同文件時可點擊【繼續上次進度】恢復。")
            self.log_queue.put(("__status__", "進度已保存（未輸出文件）"))
        except Exception as e:
            self._log(f"錯誤：{e}")
            self.log_queue.put(("__status__", "執行失敗"))
            import traceback
            self._log(traceback.format_exc())
        finally:
            self.log_queue.put(("__done__", None))


def main():
    root = tk.Tk()
    TranslatorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
