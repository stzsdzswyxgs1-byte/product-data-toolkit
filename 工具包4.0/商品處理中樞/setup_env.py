# -*- coding: utf-8 -*-
"""
商品處理中樞 — 環境配置腳本 (由 setup.bat 呼叫)

責任分工:
  setup.bat (ASCII-only):  確保 Python 存在 (沒裝就自動下載安裝)
  setup_env.py (本檔):     建 venv + 偵測 GPU + 裝套件 + 預下載 LaMa 模型

★ 此檔處理所有「中文輸出」, 避免 Windows BAT 的 GBK/UTF-8 亂碼問題
★ Python 3.10+ 原生支援 UTF-8, 中文絕對不會亂碼
"""
import os
import sys
import subprocess
import shutil
import time

# ── 強制 stdout 用 UTF-8 (避免 Windows console 預設 GBK 印出亂碼) ──
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass  # Python 3.6 沒這方法 (但我們要求 3.10+ 不會踩到)


HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, '.venv')


def banner(s, char='='):
    line = char * 50
    print(f'\n{line}\n  {s}\n{line}')


def run(cmd, **kw):
    """跑指令, 失敗 raise. 預設不顯示子程序 stdout (太雜)."""
    return subprocess.run(cmd, check=True, **kw)


def get_pip():
    return os.path.join(VENV, 'Scripts', 'pip.exe' if os.name == 'nt' else 'pip')


def get_python():
    return os.path.join(VENV, 'Scripts', 'python.exe' if os.name == 'nt' else 'python')


# ────────────────────────────────────────────────────────────────
# Step 1: 建 venv
# ────────────────────────────────────────────────────────────────
def step_venv():
    print('\n[1/5] 建立虛擬環境 .venv ...')
    if os.path.exists(VENV):
        # 驗證可用
        if os.path.exists(get_python()):
            print('      ✓ 已存在, 跳過')
            return
        print('      ⚠ .venv 存在但不完整, 重建')
        shutil.rmtree(VENV, ignore_errors=True)
    run([sys.executable, '-m', 'venv', VENV])
    print('      ✓ 完成')


# ────────────────────────────────────────────────────────────────
# Step 2: 升級 pip
# ────────────────────────────────────────────────────────────────
def step_pip():
    print('\n[2/5] 升級 pip ...')
    run([get_python(), '-m', 'pip', 'install', '--upgrade', 'pip',
         '--quiet', '--disable-pip-version-check'])
    print('      ✓ 完成')


# ────────────────────────────────────────────────────────────────
# Step 3: 偵測 GPU
# ────────────────────────────────────────────────────────────────
def step_detect_gpu():
    """偵測 GPU 並挑對應 CUDA build.

    回傳: (mode, cuda_sub, name, vram_mb)
        mode='gpu'/'cpu', cuda_sub='cu121'/'cu124'/'cpu'

    決策表 (compute capability sm_X.Y):
        sm < 6.0          → cpu (Pascal 前, 跑現代 PyTorch 不穩)
        sm 6.0 - 9.x      → cu121 (Pascal/Volta/Turing/Ampere/Ada/Hopper, torch 2.5.x/2.6.x 都支援)
        sm 10.0+          → cu124 (Blackwell B100/B200/RTX 50xx, cu121 沒這代 SASS)
                            ★ 注意: RTX 50xx 是 sm_12.0, cu124 帶 sm_90 PTX, JIT 慢但能跑
                            ★ 真要原生 sm_120 SASS 需要 cu126 + torch 2.7+, 但會破壞 numpy 1.x ABI
    """
    print('\n[3/5] 偵測顯卡 ...')
    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,compute_cap', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace')
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().split('\n')[0]
            parts = [x.strip() for x in line.split(',')]
            name = parts[0]
            vram_mb = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            try:
                compute_cap = float(parts[2]) if len(parts) > 2 else 0.0
            except ValueError:
                compute_cap = 0.0

            print(f'      ✓ 偵測到 NVIDIA: {name} ({vram_mb / 1024:.1f}GB, sm_{compute_cap})')

            # ★ 老卡保護: compute capability < 6.0 (Pascal 之前) 跑現代 PyTorch 不穩
            #   sm_50 (Maxwell, 750/750Ti/970/980/Titan X Maxwell) 雖然能裝, 但 ops 多 fallback 或崩
            if 0 < compute_cap < 6.0:
                print(f'      ⚠ 此卡 compute capability {compute_cap} < 6.0 (Pascal 前)')
                print(f'        現代 PyTorch CUDA build 對它支援不穩, 強制改用 CPU torch (~200MB)')
                print(f'        慢但穩. 想強用 GPU 請手動改 lama config device=cuda.')
                return 'cpu', 'cpu', name, vram_mb

            # ★ 顯存太小: < 2GB 也走 CPU (LaMa 至少要 1-2GB free VRAM)
            if 0 < vram_mb < 1500:
                print(f'      ⚠ VRAM {vram_mb}MB 太小 (< 1.5GB), 強制改用 CPU torch')
                return 'cpu', 'cpu', name, vram_mb

            # ★ Blackwell (sm 10.0+): cu124 不夠用 — RTX 50xx 跑 LaMa 會 'no kernel image'
            #   sm_120 (RTX 50xx consumer) → cu128 build (torch 2.9.1+) 原生 SASS
            #   sm_100 (B100/B200 datacenter) → cu128 也可以, 統一走 cu128 簡化
            #   注意: cu128 路徑放寬 NUMPY1_TORCH_CAP, 可能 numpy ABI 細節有風險, 但已驗
            #         Aliyun cu128 最舊 2.9.1, runtime 跟 numpy 1.x forward-compat
            if compute_cap >= 10.0:
                print(f'      ✓ Blackwell 級顯卡 (sm_{compute_cap}) → 用 cu128 build (torch 2.9.1+)')
                print(f'        ★ cu128 對 sm_120 有原生 SASS, LaMa GPU 加速可用')
                return 'gpu', 'cu128', name, vram_mb

            # ★ Pascal/Volta/Turing/Ampere/Ada/Hopper (sm 6.0 - 9.x): cu121 都能跑
            print(f'      ✓ 用 cu121 build (torch 2.5.x/2.6.x 對 sm_{compute_cap} 原生支援)')
            return 'gpu', 'cu121', name, vram_mb
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
        pass
    print('      ⚠ 沒偵測到 NVIDIA 顯卡 → 走 CPU 模式 (慢但能跑)')
    return 'cpu', 'cpu', None, 0


# ────────────────────────────────────────────────────────────────
# Step 4: 裝套件
# ────────────────────────────────────────────────────────────────
BASE_PKGS = [
    'pandas>=2.0',
    'openpyxl>=3.1',
    'Pillow>=10.0',
    'requests>=2.31',
    'opencv-python>=4.8',
    'opencc-python-reimplemented',
    'lunardate',
]


# ────────────────────────────────────────────────────────────────
# pip 鏡像源自動偵測 (中國用戶卡頓 PyPI 必修)
# ────────────────────────────────────────────────────────────────
PIP_MIRRORS = [
    ('https://pypi.tuna.tsinghua.edu.cn/simple',  '清華大學 (中國)'),
    ('https://mirrors.aliyun.com/pypi/simple/',   '阿里雲 (中國)'),
    ('https://mirrors.cloud.tencent.com/pypi/simple/', '騰訊雲 (中國)'),
    ('https://pypi.org/simple',                   'PyPI 官方 (海外)'),
]


def select_fastest_mirror():
    """測試所有鏡像, 選最快的. 返回 (url, is_china).

    is_china=True 表示走中國鏡像 → 後續 PyTorch 也要強制走中國源.
    """
    import urllib.request
    print('  偵測 pip 鏡像源 (取最快)...')
    fastest = None
    fastest_t = float('inf')
    for url, name in PIP_MIRRORS:
        try:
            t0 = time.time()
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as r:
                r.read(200)
            dt = time.time() - t0
            print(f'    ✓ {name:24s}  {dt * 1000:.0f}ms')
            if dt < fastest_t:
                fastest_t = dt
                fastest = url
        except Exception as e:
            print(f'    ✗ {name:24s}  不通 ({type(e).__name__})')
    if fastest:
        name = next((n for u, n in PIP_MIRRORS if u == fastest), '?')
        print(f'  → 使用: {name}')
    fastest = fastest or 'https://pypi.org/simple'
    is_china = 'pypi.org' not in fastest  # 非 PyPI 官方 = 中國鏡像
    return fastest, is_china


def select_pytorch_index(sub, is_china):
    """選 PyTorch 鏡像. 中國用戶強制 Aliyun (不 probe — probe 量延遲不量帶寬).

    參數:
        sub: 'cu121' / 'cu124' / 'cpu' — 由 step_detect_gpu 決定

    回傳: (url, type, name)
        type='index'      → PEP 503 (用 --index-url)
        type='find-links' → 平面目錄 (用 --find-links + 強 pin 版本)
    """
    if is_china:
        # 中國同事直接 Aliyun, 不 probe — Official 在中國連線快但下載慢, probe 會誤判
        url = f'https://mirrors.aliyun.com/pytorch-wheels/{sub}/'
        print(f'  PyTorch 鏡像: 阿里雲 (中國) — find-links 模式 ({sub})')
        return (url, 'find-links', '阿里雲 (中國)')
    else:
        # 海外直接 PyTorch 官方
        url = f'https://download.pytorch.org/whl/{sub}'
        print(f'  PyTorch 鏡像: PyTorch 官方 (海外) — index 模式 ({sub})')
        return (url, 'index', 'PyTorch 官方 (海外)')


# numpy ABI 安全上限 (cu121/cu124 等舊路徑): torch 2.7+ 強制 numpy 2.x ABI 編譯
# simple-lama-inpainting 要求 numpy<2.0 → 安裝後 numpy 被砍回 1.x → 風險:torch c10.dll 找不到 numpy 2.x 符號
# 對舊 GPU 走保守路 (≤2.6.x). Blackwell (cu126/cu128) 沒選擇必須突破 — Aliyun cu128 最舊 2.9.1.
NUMPY1_TORCH_CAP = (2, 6)  # cu121/cu124 路徑用
BLACKWELL_TORCH_CAP = (2, 9)   # cu126/cu128 路徑 — 鎖 2.9.x (Aliyun 最舊也是 2.9.1, 較穩 vs 2.10/2.11)
TORCH_TV_PAIRS = {
    # major.minor → 對應 torchvision (Aliyun 上找得到的最新 patch 由掃描決定)
    (2, 11): '0.26',
    (2, 10): '0.25',
    (2, 9):  '0.24',
    (2, 8):  '0.23',
    (2, 7):  '0.22',
    (2, 6):  '0.21',
    (2, 5):  '0.20',
    (2, 4):  '0.19',
    (2, 3):  '0.18',
    (2, 2):  '0.17',
    (2, 1):  '0.16',
    (2, 0):  '0.15',
}


def find_aliyun_torch_versions(sub='cu121'):
    """從 Aliyun pytorch-wheels 抓 numpy 1.x 兼容的最新 torch/torchvision wheel.

    必須 pin 版本, 否則 pip 會抓 Aliyun 上最新的 (例如 2.11.0+cpu) — 這個是 numpy 2.x ABI 編譯,
    跟 simple-lama-inpainting 衝突 → c10.dll 載入失敗 (WinError 1114).
    """
    import urllib.request, re
    url = f'https://mirrors.aliyun.com/pytorch-wheels/{sub}/'
    py_ver = f'cp{sys.version_info.major}{sys.version_info.minor}'
    if sys.platform == 'win32':
        plat = 'win_amd64'
    elif sys.platform == 'darwin':
        plat = 'macosx'
    else:
        plat = 'linux_x86_64'

    plain_suffix = f'+{sub}' if sub.startswith('cu') else ''

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'  ⚠ 偵測 Aliyun 版本失敗: {e}, 用 fallback 版本')
        return None, None

    def list_versions(pkg_name):
        patterns = [
            rf'{pkg_name}-(\d+\.\d+\.\d+)(?:&#43;|\+|%2B){sub}-{py_ver}-{py_ver}-{plat}\.whl',
            rf'{pkg_name}-(\d+\.\d+\.\d+){re.escape(plain_suffix)}-{py_ver}-{py_ver}-{plat}\.whl',
        ]
        all_vers = set()
        for pat in patterns:
            all_vers.update(re.findall(pat, html))
        return all_vers

    # torch: 過濾版本上限 — Blackwell (cu126/cu128) 走寬鬆 cap, 其他走保守 cap
    torch_all = list_versions('torch')
    cap = BLACKWELL_TORCH_CAP if sub in ('cu126', 'cu128') else NUMPY1_TORCH_CAP
    torch_compat = [v for v in torch_all
                    if tuple(int(x) for x in v.split('.'))[:2] <= cap]
    if not torch_compat:
        print(f'  ⚠ Aliyun {sub} 沒有合適 torch wheel (cap=≤{cap[0]}.{cap[1]}.x)')
        return None, None
    torch_ver = sorted(torch_compat, key=lambda v: tuple(int(x) for x in v.split('.')))[-1]

    # torchvision: 對應 torch major.minor 找最新 patch
    torch_mm = tuple(int(x) for x in torch_ver.split('.'))[:2]
    tv_prefix = TORCH_TV_PAIRS.get(torch_mm)
    tv_ver = None
    if tv_prefix:
        tv_all = list_versions('torchvision')
        tv_match = [v for v in tv_all if v.startswith(tv_prefix + '.')]
        if tv_match:
            tv_ver = sorted(tv_match, key=lambda v: tuple(int(x) for x in v.split('.')))[-1]

    return torch_ver, tv_ver


def step_install_pkgs(mode, sub):
    """裝套件. 返回 is_china 給後續 step_warmup_lama 用.

    參數:
        mode: 'gpu' / 'cpu'  — 是否要 CUDA build
        sub:  'cu121' / 'cu124' / 'cpu'  — 哪個 CUDA build (由 step_detect_gpu 決定)
    """
    pip = get_pip()

    print('\n[4/5] 裝相依套件 (5-10 分鐘, 看網速) ...')

    # 偵測並選鏡像 (中國用戶必須走鏡像, 否則 PyPI 卡死)
    mirror, is_china = select_fastest_mirror()

    # pip 通用 args (不再 quiet, 讓使用者看到下載進度;
    #              加 timeout 避免單個套件卡死整個 install;
    #              --no-cache-dir 省 pip cache → 解壓速度 +30%)
    pip_args = [
        pip, 'install',
        '--disable-pip-version-check',
        '--default-timeout=60',
        '--index-url', mirror,
        '--retries', '3',
        '--no-cache-dir',
    ]

    # 4a. 基本套件
    print('\n      → 基本套件 (pandas / openpyxl / Pillow / opencv ...)')
    run(pip_args + BASE_PKGS)
    print('         ✓')

    # 4b. PyTorch — 中國用戶 Aliyun, 海外 PyTorch 官方
    print()
    torch_url, torch_type, torch_name = select_pytorch_index(sub, is_china)
    if mode == 'gpu':
        cuda_label = {
            'cu121': 'CUDA 12.1', 'cu124': 'CUDA 12.4',
            'cu126': 'CUDA 12.6', 'cu128': 'CUDA 12.8 — Blackwell 原生',
        }.get(sub, sub.upper())
        print(f'\n      → PyTorch GPU 版 (含 {cuda_label}, ~2.5GB)')
    else:
        print('\n      → PyTorch CPU 版 (~200MB)')

    torch_args = [pip, 'install',
                  '--disable-pip-version-check',
                  '--default-timeout=120',
                  '--retries', '3',
                  '--no-cache-dir']

    if torch_type == 'index':
        # PEP 503 (PyTorch 官方) — 直接抓
        torch_args += ['--index-url', torch_url, 'torch', 'torchvision']
    else:
        # 平面目錄 (Aliyun) — 必須強 pin 版本, 否則 pip 抓 PyPI 上的 plain torch (沒 cu121)
        # 結果 import 時 c10.dll 載入失敗
        print(f'      偵測 Aliyun {sub} 上對應您 Python 版本的最新 wheel...')
        torch_ver, tv_ver = find_aliyun_torch_versions(sub)

        # ★ Blackwell fallback 鏈: cu128 → cu126 → cu124 (越退越舊但仍能裝)
        #   舊 GPU 的 cu124 → cu121 fallback 也保留
        used_sub = sub
        if not (torch_ver and tv_ver):
            fallback_chain = {
                'cu128': ['cu126', 'cu124'],  # Blackwell consumer 退路
                'cu126': ['cu124'],            # Blackwell datacenter 退路
                'cu124': ['cu121'],            # Ada/Hopper 退路 (本來就有)
            }.get(sub, [])
            for fb in fallback_chain:
                print(f'      ⚠ Aliyun {used_sub} 沒對應 wheel, 退到 {fb}')
                torch_ver, tv_ver = find_aliyun_torch_versions(fb)
                if torch_ver and tv_ver:
                    used_sub = fb
                    torch_url = f'https://mirrors.aliyun.com/pytorch-wheels/{fb}/'
                    break

        if torch_ver and tv_ver:
            print(f'      ✓ 找到: torch=={torch_ver}+{used_sub}, torchvision=={tv_ver}+{used_sub}')
            suffix = f'+{used_sub}' if used_sub.startswith('cu') else ''
            torch_args += [
                '--index-url', mirror,        # 給 torch 的相依 (sympy/networkx/...)
                '--find-links', torch_url,    # PyTorch wheel 在這
                f'torch=={torch_ver}{suffix}',
                f'torchvision=={tv_ver}{suffix}',
            ]
        else:
            # Aliyun listing 抓不到 → 退回官方 (慢但能裝)
            print(f'      ⚠ Aliyun 找不到對應 wheel, 改用 PyTorch 官方 (會慢)')
            torch_args += ['--index-url', f'https://download.pytorch.org/whl/{sub}',
                           'torch', 'torchvision']
    run(torch_args)
    print('         ✓')

    # 4c. LaMa (走鏡像)
    print('\n      → LaMa 去水印模型 (simple-lama-inpainting)')
    run(pip_args + ['simple-lama-inpainting'])
    print('         ✓')

    return is_china


# ────────────────────────────────────────────────────────────────
# Step 5: 預下載 LaMa 模型權重
# ────────────────────────────────────────────────────────────────
LAMA_GITHUB_URL = 'https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt'
LAMA_KKGITHUB_URL = 'https://kkgithub.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt'


def step_warmup_lama(is_china=False):
    # 打包檔在: 直接跳過下載, 第一次用 image_dewatermark 自動讀
    bundled = os.path.join(HERE, 'big-lama.pt')
    if os.path.isfile(bundled):
        size_mb = os.path.getsize(bundled) / 1024 / 1024
        print(f'\n[5/5] 偵測到打包 LaMa 權重 ({size_mb:.0f} MB), 跳過 GitHub 下載')
        print(f'      ✓ 軟件啟動時會自動使用打包檔 (中國用戶福音)')
        return

    # 沒打包檔: 中國用戶走 kkgithub 鏡像 (3-10 MB/s), 海外走 GitHub 原 URL
    lama_url = LAMA_KKGITHUB_URL if is_china else LAMA_GITHUB_URL
    src = 'kkgithub 鏡像 (中國)' if is_china else 'GitHub'
    print(f'\n[5/5] 預下載 LaMa 模型權重 (~200MB, 從 {src} 下) ...')
    print('      ★ 這步「可選」, 跳過不影響. 下載慢時可 Ctrl+C 跳過')
    print('         → 跳過後第一次點「去水印救援」會自動下, 之後不再下')
    print()
    py = get_python()
    # 子程序也需要這個 URL — 透過 env 傳, simple_lama_inpainting 認 LAMA_MODEL_URL
    env = os.environ.copy()
    env['LAMA_MODEL_URL'] = lama_url
    try:
        # 不 capture, 讓用戶看到 torch.hub 下載進度 (kB/s + bar)
        # timeout 中國 8 分鐘 / 海外 5 分鐘
        timeout = 480 if is_china else 300
        r = subprocess.run(
            [py, '-c', 'from simple_lama_inpainting import SimpleLama; SimpleLama(); print("ready")'],
            timeout=timeout, env=env,
            encoding='utf-8', errors='replace')
        if r.returncode == 0:
            print('      ✓ LaMa 模型已備好')
        else:
            print('      ⚠ 預下載失敗 (不影響, 第一次跑軟件會自動再下)')
    except subprocess.TimeoutExpired:
        m = timeout // 60
        print(f'      ⚠ {m} 分鐘還沒下完, 跳過 (第一次跑軟件再下)')
    except KeyboardInterrupt:
        print('      ⊘ 用戶跳過 (第一次跑軟件再下)')
    except Exception as e:
        print(f'      ⚠ {e}')


# ────────────────────────────────────────────────────────────────
# 收尾: 環境檢查報告
# ────────────────────────────────────────────────────────────────
def final_report():
    banner('環境檢查')
    py = get_python()
    # 深度測試: import + matmul + arch_list — 對 Blackwell 用戶確認 sm_120 真能跑 kernel
    code = '''
import sys
print(f"Python  {sys.version.split()[0]}")
try:
    import torch
    print(f"torch   {torch.__version__}")
    cuda = torch.cuda.is_available()
    print(f"CUDA    {cuda}")
    if cuda:
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        archs = torch.cuda.get_arch_list()
        print(f"GPU     {name} (sm_{cap[0]}.{cap[1]})")
        print(f"archs   {archs}")
        # 深度測試: 真試 kernel — matmul 走 cuBLAS, conv 走 cuDNN
        try:
            x = torch.randn(64, 64, device="cuda")
            y_sum = float((x @ x).sum())
            print(f"matmul  OK (sum={y_sum:.1f})")
        except Exception as e:
            print(f"matmul  FAIL: {type(e).__name__}: {str(e)[:100]}")
            print(f"        ★ kernel launch 失敗 — 跑時 LaMa 會自動 fallback CPU (4.0.15 保險)")
        try:
            import torch.nn.functional as F
            x = torch.randn(1, 3, 32, 32, device="cuda")
            w = torch.randn(8, 3, 3, 3, device="cuda")
            y = F.conv2d(x, w)
            print(f"conv2d  OK (shape={list(y.shape)})")
        except Exception as e:
            print(f"conv2d  FAIL: {type(e).__name__}: {str(e)[:100]}")
            print(f"        ★ LaMa 用 conv 跑不了 — 會走 CPU LaMa")
    else:
        print(f"GPU     (無, 走 CPU)")
    # numpy ABI 檢查
    try:
        import numpy
        print(f"numpy   {numpy.__version__}")
    except Exception as e:
        print(f"numpy   IMPORT FAIL: {e}")
except Exception as e:
    print(f"⚠ torch import 炸了: {type(e).__name__}: {str(e)[:200]}")
    print(f"  常見原因: numpy ABI 不兼容 (WinError 1114)")
    print(f"  解法: pip install --force-reinstall numpy==1.26.4")
'''
    r = subprocess.run([py, '-c', code], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    print(r.stdout)
    if r.returncode != 0:
        print('⚠', r.stderr)

    banner('安裝完成 ✅', '=')
    print()
    print('  下一步:')
    print('    1. 雙擊 [start.bat] 啟動軟件')
    print('    2. 主介面填您的 TG ID')
    print('    3. 沒有 TG ID? 去 Telegram 找 @example_admin_bot 發 /start 拿到')
    print('    4. 把 TG ID 給管理員 /add 開通配額')
    print()


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────
def main():
    banner('商品處理中樞 — 自動環境配置')
    print(f'  Python   {sys.version.split()[0]}')
    print(f'  路徑     {HERE}')

    t0 = time.time()
    try:
        step_venv()
        step_pip()
        mode, sub, gpu_name, vram_mb = step_detect_gpu()
        is_china = step_install_pkgs(mode, sub)
        step_warmup_lama(is_china=bool(is_china))
        final_report()
        print(f'  總耗時: {time.time() - t0:.0f}s')
        return 0
    except subprocess.CalledProcessError as e:
        print(f'\n❌ 安裝指令失敗 (exit code {e.returncode}):')
        print(f'   {" ".join(e.cmd) if hasattr(e, "cmd") else "?"}')
        print('\n建議:')
        print('  1. 檢查網路連線')
        print('  2. 重跑 setup.bat (會跳過已裝部分)')
        print('  3. 若仍失敗, 把這整段訊息截圖給管理員')
        return 1
    except KeyboardInterrupt:
        print('\n\n用戶中止安裝')
        return 130
    except Exception as e:
        print(f'\n❌ 未預期錯誤: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
