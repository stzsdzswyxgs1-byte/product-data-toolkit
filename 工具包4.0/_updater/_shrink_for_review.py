# -*- coding: utf-8 -*-
"""shrink_for_review.py — 把 D:/toolkit-feedback/images 的 JPG 壓縮到 512x512 q70,
供 admin Claude session audit 用,避免累積 200+ 張 read 撞 Anthropic 32MB request limit.

用法:
    python _shrink_for_review.py <hash1> [hash2 ...]
    python _shrink_for_review.py --batch <file.txt>    # 一行一個 hash
    python _shrink_for_review.py --all-from-lama <date>  # 抽當天 lama-logs 所有 unique hash
    python _shrink_for_review.py --clean              # 清 review_shrink 資料夾

輸出: D:/toolkit-feedback/review_shrink/<hash[:16]>.jpg (~15-30KB each)
"""
import os
import sys
import argparse
import json
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

IMG_ROOT = Path(r'D:\toolkit-feedback\images')
SHRINK_ROOT = Path(r'D:\toolkit-feedback\review_shrink')
LAMA_ROOT = Path(r'D:\toolkit-feedback\lama-logs')

MAX_SIZE = 512
QUALITY = 70


def find_full_path(hash_: str) -> Path | None:
    """從 16 hex hash 找實際檔. D:/images/<前2>/<hash[:16]>.jpg"""
    h16 = hash_[:16] if len(hash_) > 16 else hash_
    p = IMG_ROOT / h16[:2] / f'{h16}.jpg'
    return p if p.exists() else None


def shrink_one(hash_: str) -> tuple[bool, str]:
    """壓縮一張. 回 (ok, msg)."""
    from PIL import Image
    src = find_full_path(hash_)
    if not src:
        return False, f'NOT_FOUND'
    SHRINK_ROOT.mkdir(parents=True, exist_ok=True)
    h16 = hash_[:16] if len(hash_) > 16 else hash_
    dst = SHRINK_ROOT / f'{h16}.jpg'
    if dst.exists():
        sz_orig = src.stat().st_size
        sz_new = dst.stat().st_size
        return True, f'CACHED  {sz_orig//1024}KB → {sz_new//1024}KB'
    try:
        img = Image.open(src).convert('RGB')
        if max(img.size) > MAX_SIZE:
            ratio = MAX_SIZE / max(img.size)
            img = img.resize(
                (int(img.size[0] * ratio), int(img.size[1] * ratio)),
                Image.LANCZOS
            )
        img.save(dst, 'JPEG', quality=QUALITY, optimize=True)
        sz_orig = src.stat().st_size
        sz_new = dst.stat().st_size
        ratio_pct = sz_new / sz_orig * 100
        return True, f'OK      {sz_orig//1024:>4}KB → {sz_new//1024:>3}KB ({ratio_pct:.0f}%)'
    except Exception as e:
        return False, f'ERROR   {type(e).__name__}: {str(e)[:50]}'


def cmd_shrink(hashes: list[str]):
    """批次壓縮多個 hash."""
    ok = 0
    fail = 0
    cached = 0
    total_orig = 0
    total_new = 0
    for h in hashes:
        success, msg = shrink_one(h)
        h16 = h[:16] if len(h) > 16 else h
        print(f'  {h16}  {msg}')
        if success:
            ok += 1
            if 'CACHED' in msg:
                cached += 1
        else:
            fail += 1
    # 算 total 大小
    for h in hashes:
        h16 = h[:16] if len(h) > 16 else h
        dst = SHRINK_ROOT / f'{h16}.jpg'
        src = find_full_path(h)
        if dst.exists():
            total_new += dst.stat().st_size
        if src:
            total_orig += src.stat().st_size
    print()
    print(f'總計: {len(hashes)} 張 | OK {ok} (cached {cached}) | FAIL {fail}')
    if ok > 0:
        print(f'原大小總和: {total_orig/1024/1024:.1f} MB')
        print(f'壓後總和:   {total_new/1024/1024:.1f} MB  ({total_new/total_orig*100:.0f}%)')
        print(f'平均單張:   {total_new//ok//1024} KB')
        print(f'輸出位置:   {SHRINK_ROOT}')


def cmd_all_from_lama(date: str):
    """從 lama-logs/<date>.jsonl 抽所有 unique stage_e_hash + output_hash"""
    fp = LAMA_ROOT / f'{date}.jsonl'
    if not fp.exists():
        print(f'找不到: {fp}')
        return
    hashes = set()
    with open(fp, 'r', encoding='utf-8') as f:
        for ln in f:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            for k in ('stage_e_hash', 'output_hash', 'input_hash'):
                h = e.get(k, '')
                if h and len(h) >= 16:
                    hashes.add(h[:16])
    print(f'從 {fp} 抽到 unique hash: {len(hashes)}')
    print(f'開始壓縮...')
    cmd_shrink(sorted(hashes))


def cmd_clean():
    """清空 review_shrink 資料夾"""
    if not SHRINK_ROOT.exists():
        print('資料夾不存在')
        return
    files = list(SHRINK_ROOT.glob('*.jpg'))
    total = sum(f.stat().st_size for f in files)
    for f in files:
        f.unlink()
    print(f'刪 {len(files)} 張, 釋放 {total/1024/1024:.1f} MB')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hashes', nargs='*', help='hash list')
    ap.add_argument('--batch', help='從檔讀 hash list (一行一個)')
    ap.add_argument('--all-from-lama', metavar='YYYY-MM-DD', help='抽某天 lama-logs 所有 hash')
    ap.add_argument('--clean', action='store_true', help='清空 review_shrink')
    args = ap.parse_args()

    if args.clean:
        cmd_clean()
        return
    if args.all_from_lama:
        cmd_all_from_lama(args.all_from_lama)
        return
    if args.batch:
        with open(args.batch, 'r', encoding='utf-8') as f:
            hashes = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
        cmd_shrink(hashes)
        return
    if args.hashes:
        cmd_shrink(args.hashes)
        return
    ap.print_help()


if __name__ == '__main__':
    main()
