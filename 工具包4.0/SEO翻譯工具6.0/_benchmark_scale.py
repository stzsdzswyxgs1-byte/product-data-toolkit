"""
規模測試: 用 2000 段日文(接近真實 20k 量), 測 scaling 行為
4 個 top 配置 1 run each, 預計 4-6 分鐘
"""
import sys, os, time, re
sys.path.insert(0, r"C:/Users/USERNAME\Desktop\工具包4.0\SEO翻譯工具6.0")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from translator import CFG, translate_segments_api

INPUT_XLSX = r"C:/Users/USERNAME\Downloads\export_20260521135902.xlsx"
SAMPLE_SIZE = 2000

print("=" * 70)
print("規模測試 — 模擬真實 production scale")
print("=" * 70)

# 1. 抽 2000 段日文
print(f"\n載入大檔: {INPUT_XLSX}")
df = pd.read_excel(INPUT_XLSX)
print(f"  rows: {len(df)}, cols: {list(df.columns)[:5]}")

segments = []
KANA = re.compile(r'[぀-ヿ]')
for col in df.columns:
    for v in df[col].dropna().astype(str):
        for line in v.split('\n'):
            line = line.strip()
            if 5 < len(line) < 150 and KANA.search(line):
                segments.append(line)
segments = list(dict.fromkeys(segments))[:SAMPLE_SIZE]
print(f"  抽出 {len(segments)} 段日文(去重)")

# 2. 配置
CFG.api_base = "https://api.example.com/v1"
CFG.api_key = "<TEST_API_KEY>"
CFG.model = "gpt-5.5"
CFG.timeout = 90

# 3. 測 top 候選 + scaling 對照
configs = [
    {"workers": 96, "batch_size": 15, "label": "96/15 (小 batch 王)"},
    {"workers": 96, "batch_size": 20, "label": "96/20"},
    {"workers": 96, "batch_size": 30, "label": "96/30 (當前)"},
    {"workers": 96, "batch_size": 40, "label": "96/40"},
]

results = []
for i, cfg in enumerate(configs):
    CFG.workers = cfg["workers"]
    CFG.batch_size = cfg["batch_size"]

    items = list(enumerate(segments))
    batches = [items[j:j + cfg["batch_size"]] for j in range(0, len(items), cfg["batch_size"])]
    rounds = (len(batches) + cfg["workers"] - 1) // cfg["workers"]

    print(f"\n[{i+1}/{len(configs)}] {cfg['label']}")
    print(f"  workers={cfg['workers']}, batch={cfg['batch_size']}, {len(batches)} batches, ~{rounds} 輪")

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

    elapsed = time.time() - t0
    avg_batch = sum(batch_times) / len(batch_times) if batch_times else 0
    throughput = len(segments) / elapsed if elapsed > 0 else 0

    print(f"  → {elapsed:.1f}s | avg batch {avg_batch:.1f}s | {throughput:.1f} 段/秒 | 成功 {success}/{len(segments)} | rounds={rounds}")

    results.append({
        **cfg,
        "elapsed": elapsed,
        "throughput": throughput,
        "success": success,
        "failed": failed,
        "avg_batch": avg_batch,
        "batches": len(batches),
        "rounds": rounds,
    })

    # 配置之間 sleep
    if i < len(configs) - 1:
        print(f"  (休息 10s)")
        time.sleep(10)

# 4. 彙總
print(f"\n{'=' * 70}")
print(f"規模測試結果 ({SAMPLE_SIZE} 段)")
print(f"{'=' * 70}")
print(f"{'配置':<22} {'時間':>8} {'batches':>9} {'輪':>5} {'avg':>7} {'段/秒':>8} {'成功率':>8}")
print("-" * 75)
for r in sorted(results, key=lambda x: x["elapsed"]):
    rate = r["success"] / (r["success"] + r["failed"]) * 100 if (r["success"] + r["failed"]) > 0 else 0
    is_best = "🏆" if r == min(results, key=lambda x: x["elapsed"] if x["failed"] == 0 else float('inf')) else "  "
    print(f"{is_best} {r['label']:<20} {r['elapsed']:>6.1f}s {r['batches']:>8} {r['rounds']:>5} {r['avg_batch']:>5.1f}s {r['throughput']:>6.1f} {rate:>7.1f}%")

# 5. 推算真實 20k 段時間
print(f"\n{'=' * 70}")
print(f"推算: 你下次跑 20,544 段 (真實 production scale)")
print(f"{'=' * 70}")
PRODUCTION_SIZE = 20544
for r in sorted(results, key=lambda x: PRODUCTION_SIZE / x["throughput"]):
    est = PRODUCTION_SIZE / r["throughput"]
    print(f"  {r['label']:<20} 預估 {est:>5.0f}s ({est/60:.1f} 分)")

best = min([r for r in results if r["failed"] == 0], key=lambda x: x["elapsed"])
print(f"\n🏆 規模最快 (100% 成功): {best['label']}")
print(f"   workers={best['workers']}, batch_size={best['batch_size']}")
