"""
測試 4 種翻譯配置對速度的影響
用真實日文段,跳過 cache,純測 API throughput
"""
import sys, os, time, re
sys.path.insert(0, r"C:/Users/USERNAME\Desktop\工具包4.0\SEO翻譯工具6.0")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from translator import CFG, translate_segments_api

INPUT_XLSX = r"C:/Users/USERNAME\Downloads\export_20260521133559.xlsx"
SAMPLE_SIZE = 200

print("=" * 60)
print("翻譯加速 benchmark")
print("=" * 60)

# 1. 從 xlsx 抽真實日文 segments
print(f"\n載入: {INPUT_XLSX}")
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

# 去重 + 截 SAMPLE_SIZE
segments = list(dict.fromkeys(segments))[:SAMPLE_SIZE]
print(f"  抽出 {len(segments)} 段日文(去重後)")

if len(segments) < 100:
    print(f"[WARN] segments 太少 ({len(segments)}), 結果可能不準")

# 2. 配置 API
CFG.api_base = "https://api.example.com/v1"
CFG.api_key = "<TEST_API_KEY>"
CFG.model = "gpt-5.5"
CFG.timeout = 90

# 3. 4 個配置對比
configs = [
    {"workers": 64, "batch_size": 25, "name": "當前(64w/25b)"},
    {"workers": 96, "batch_size": 30, "name": "代碼默認(96w/30b)"},
    {"workers": 64, "batch_size": 40, "name": "中等(64w/40b)"},
    {"workers": 96, "batch_size": 50, "name": "激進(96w/50b)"},
]

results = []

for cfg in configs:
    CFG.workers = cfg["workers"]
    CFG.batch_size = cfg["batch_size"]

    # 拆 batches (帶 index)
    items = list(enumerate(segments))
    batches = [items[i:i + cfg["batch_size"]] for i in range(0, len(items), cfg["batch_size"])]

    print(f"\n{'─' * 50}")
    print(f"測試: {cfg['name']}")
    print(f"  workers={cfg['workers']}, batch_size={cfg['batch_size']}, {len(batches)} batches")
    print(f"  開始...")

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
                print(f"  [batch 失敗] {str(e)[:80]}")

    elapsed = time.time() - t0
    avg_batch = sum(batch_times) / len(batch_times) if batch_times else 0
    throughput = len(segments) / elapsed if elapsed > 0 else 0

    print(f"  完成: {elapsed:.1f}s")
    print(f"  成功 {success} / 失敗 {failed} (共 {len(segments)})")
    print(f"  avg batch 時間: {avg_batch:.1f}s")
    print(f"  throughput: {throughput:.1f} 段/秒")

    results.append({
        **cfg,
        "elapsed": elapsed,
        "throughput": throughput,
        "success": success,
        "failed": failed,
        "avg_batch": avg_batch,
    })

    # 配置之間 sleep 一下,讓中介 cap 復原
    print(f"  (休息 5s 讓中介 RPM 視窗復原)")
    time.sleep(5)

# 4. 彙總
print(f"\n{'=' * 60}")
print("結論")
print(f"{'=' * 60}")
print(f"{'配置':<25} {'時間':<10} {'throughput':<15} {'avg batch':<12} {'成功率'}")
print("-" * 75)
baseline = results[0]["elapsed"]
for r in results:
    speedup = (baseline / r["elapsed"] - 1) * 100 if r["elapsed"] > 0 else 0
    success_rate = r["success"] / (r["success"] + r["failed"]) * 100 if (r["success"] + r["failed"]) > 0 else 0
    speedup_str = f"({speedup:+.0f}%)" if r != results[0] else "(baseline)"
    print(f"{r['name']:<25} {r['elapsed']:>6.1f}s {speedup_str:>8} {r['throughput']:>5.1f}/秒    {r['avg_batch']:>6.1f}s     {success_rate:>5.1f}%")

print(f"\n建議:")
best = max(results, key=lambda r: r["throughput"] if r["failed"] == 0 else 0)
print(f"  最快且穩定: {best['name']} ({best['throughput']:.1f} 段/秒)")
print(f"  改 translator_config.json: \"workers\": {best['workers']}, \"batch_size\": {best['batch_size']}")
