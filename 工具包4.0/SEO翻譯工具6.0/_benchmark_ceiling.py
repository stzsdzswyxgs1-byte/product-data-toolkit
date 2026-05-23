"""
天花板測試: 找 batch_size 真實上限
從 40 開始往上推, 看哪裡開始退步或失敗
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
print("天花板測試 — 推 batch_size 到極限")
print("=" * 70)

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
print(f"\nSample: {len(segments)} 段")

CFG.api_base = "https://api.example.com/v1"
CFG.api_key = "<TEST_API_KEY>"
CFG.model = "gpt-5.5"
CFG.timeout = 120  # 大 batch 給更長 timeout

# 從 40 開始往上, 看哪裡破功
configs = [
    {"workers": 96, "batch_size": 40, "label": "40 (control)"},
    {"workers": 96, "batch_size": 50, "label": "50"},
    {"workers": 96, "batch_size": 60, "label": "60"},
    {"workers": 96, "batch_size": 70, "label": "70"},
    {"workers": 96, "batch_size": 80, "label": "80"},
]

results = []
for i, cfg in enumerate(configs):
    CFG.workers = cfg["workers"]
    CFG.batch_size = cfg["batch_size"]

    items = list(enumerate(segments))
    batches = [items[j:j + cfg["batch_size"]] for j in range(0, len(items), cfg["batch_size"])]
    rounds = (len(batches) + cfg["workers"] - 1) // cfg["workers"]

    print(f"\n[{i+1}/{len(configs)}] batch_size={cfg['batch_size']} ({len(batches)} batches)")

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
                print(f"    [err] {str(e)[:60]}")

    elapsed = time.time() - t0
    avg_batch = sum(batch_times) / len(batch_times) if batch_times else 0
    throughput = len(segments) / elapsed if elapsed > 0 else 0
    fail_rate = failed / len(segments) * 100

    flag = ""
    if fail_rate > 0:
        flag = f"⚠️ 失敗 {fail_rate:.1f}%"
    elif avg_batch > 60:
        flag = "⚠️ batch 太慢"

    print(f"  → {elapsed:.1f}s | avg batch {avg_batch:.1f}s | {throughput:.1f} 段/秒 | 成功 {success}/{len(segments)} {flag}")

    results.append({
        **cfg,
        "elapsed": elapsed,
        "throughput": throughput,
        "success": success,
        "failed": failed,
        "fail_rate": fail_rate,
        "avg_batch": avg_batch,
        "batches": len(batches),
    })

    if i < len(configs) - 1:
        time.sleep(10)

# 彙總
print(f"\n{'=' * 70}")
print(f"天花板測試結果 ({SAMPLE_SIZE} 段)")
print(f"{'=' * 70}")
print(f"{'batch':<8} {'時間':>8} {'batches':>9} {'avg':>8} {'段/秒':>8} {'失敗率':>8}")
print("-" * 60)
for r in sorted(results, key=lambda x: (x["fail_rate"], x["elapsed"])):
    flag = " 🏆" if r == min([x for x in results if x["fail_rate"] == 0], key=lambda x: x["elapsed"]) else ""
    print(f"  {r['batch_size']:<4}  {r['elapsed']:>6.1f}s {r['batches']:>8} {r['avg_batch']:>6.1f}s {r['throughput']:>6.1f} {r['fail_rate']:>7.1f}%{flag}")

# 推算
print(f"\n{'=' * 70}")
print(f"推算 20,544 段時間:")
for r in sorted(results, key=lambda x: x["elapsed"] if x["fail_rate"] == 0 else float('inf')):
    est = 20544 / r["throughput"] if r["throughput"] > 0 else 0
    print(f"  batch={r['batch_size']:<3}  預估 {est:>5.0f}s ({est/60:.1f} 分) {' ⚠️' if r['fail_rate'] > 0 else ''}")
