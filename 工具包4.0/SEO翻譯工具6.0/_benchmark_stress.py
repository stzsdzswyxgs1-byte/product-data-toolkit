"""
壓力測試: 精細掃 workers x batch_size 找絕對最快
8 個配置, 200 段 x 1 run each, ~8 分鐘
"""
import sys, os, time, re
sys.path.insert(0, r"C:/Users/USERNAME\Desktop\工具包4.0\SEO翻譯工具6.0")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from translator import CFG, translate_segments_api

INPUT_XLSX = r"C:/Users/USERNAME\Downloads\export_20260521133559.xlsx"
SAMPLE_SIZE = 200

print("=" * 70)
print("壓力測試 — 找絕對最快配置")
print("=" * 70)

# 1. 抽 200 段日文
df = pd.read_excel(INPUT_XLSX)
segments = []
KANA = re.compile(r'[぀-ヿ]')
for col in df.columns:
    for v in df[col].dropna().astype(str):
        for line in v.split('\n'):
            line = line.strip()
            if 5 < len(line) < 150 and KANA.search(line):
                segments.append(line)
segments = list(dict.fromkeys(segments))[:SAMPLE_SIZE]
print(f"\nSample: {len(segments)} 段日文")

# 2. 配置
CFG.api_base = "https://api.example.com/v1"
CFG.api_key = "<TEST_API_KEY>"
CFG.model = "gpt-5.5"
CFG.timeout = 90

# 3. 測試配置 — 智能掃法
configs = [
    # 探索 batch size (固定 workers=96)
    {"workers": 96,  "batch_size": 15, "label": "小 batch (96/15)"},
    {"workers": 96,  "batch_size": 20, "label": "小 batch (96/20)"},
    {"workers": 96,  "batch_size": 25, "label": "微小 (96/25)"},
    {"workers": 96,  "batch_size": 30, "label": "★ 當前新 (96/30)"},
    {"workers": 96,  "batch_size": 35, "label": "中等 (96/35)"},
    # 探索 workers (固定 batch=30)
    {"workers": 64,  "batch_size": 30, "label": "少 worker (64/30)"},
    {"workers": 128, "batch_size": 30, "label": "多 worker (128/30)"},
    {"workers": 192, "batch_size": 30, "label": "極多 worker (192/30)"},
]

results = []
for i, cfg in enumerate(configs):
    CFG.workers = cfg["workers"]
    CFG.batch_size = cfg["batch_size"]

    items = list(enumerate(segments))
    batches = [items[j:j + cfg["batch_size"]] for j in range(0, len(items), cfg["batch_size"])]

    print(f"\n[{i+1}/{len(configs)}] {cfg['label']}")
    print(f"  workers={cfg['workers']}, batch={cfg['batch_size']}, {len(batches)} batches")

    t0 = time.time()
    success = 0
    failed = 0
    batch_times = []

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        fut_to_b = {}
        for b in batches:
            tb0 = time.time()
            fut = ex.submit(translate_segments_api, b, CFG.timeout, 2)
            fut_to_b[fut] = (b, tb0)

        for fut in as_completed(fut_to_b):
            b, tb0 = fut_to_b[fut]
            try:
                res = fut.result()
                success += len(res)
                batch_times.append(time.time() - tb0)
            except Exception as e:
                failed += len(b)
                print(f"    [batch err] {str(e)[:60]}")

    elapsed = time.time() - t0
    avg_batch = sum(batch_times) / len(batch_times) if batch_times else 0
    throughput = len(segments) / elapsed if elapsed > 0 else 0

    print(f"  → {elapsed:.1f}s | avg batch {avg_batch:.1f}s | {throughput:.1f} 段/秒 | 成功 {success}/{len(segments)}")

    results.append({
        **cfg,
        "elapsed": elapsed,
        "throughput": throughput,
        "success": success,
        "failed": failed,
        "avg_batch": avg_batch,
        "batches": len(batches),
    })

    # 配置之間 sleep, 讓中介 RPM 窗口復原
    if i < len(configs) - 1:
        time.sleep(8)

# 4. 彙總
print(f"\n{'=' * 70}")
print("壓力測試結果")
print(f"{'=' * 70}")
print(f"{'配置':<22} {'時間':>8} {'avg batch':>10} {'段/秒':>8} {'成功率':>8}")
print("-" * 70)
for r in sorted(results, key=lambda x: x["elapsed"]):
    success_rate = r["success"] / (r["success"] + r["failed"]) * 100 if (r["success"] + r["failed"]) > 0 else 0
    is_best = "🏆" if r == min(results, key=lambda x: x["elapsed"] if x["failed"] == 0 else float('inf')) else "  "
    print(f"{is_best} {r['label']:<20} {r['elapsed']:>6.1f}s {r['avg_batch']:>8.1f}s {r['throughput']:>6.1f} {success_rate:>7.1f}%")

# 5. 推薦
best = min([r for r in results if r["failed"] == 0], key=lambda x: x["elapsed"])
print(f"\n{'=' * 70}")
print(f"絕對最快 (100% 成功): {best['label']}")
print(f"  workers: {best['workers']}, batch_size: {best['batch_size']}")
print(f"  {best['elapsed']:.1f}s | {best['throughput']:.1f} 段/秒")

cur_96_30 = next((r for r in results if r["workers"] == 96 and r["batch_size"] == 30), None)
if cur_96_30 and best != cur_96_30:
    delta = (cur_96_30["elapsed"] - best["elapsed"]) / cur_96_30["elapsed"] * 100
    print(f"  比當前 96/30 快 {delta:.1f}%")
elif cur_96_30 == best:
    print(f"  ✓ 跟當前 96/30 配置一致, 不用改")
print(f"{'=' * 70}")
