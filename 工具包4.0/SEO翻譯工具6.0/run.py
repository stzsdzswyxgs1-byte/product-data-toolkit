import subprocess
import sys

# 需要的依赖
required_packages = {
    "pandas": "pandas",
    "requests": "requests",
    "openpyxl": "openpyxl",
    "opencc": "opencc-python-reimplemented"
}

# 检查并安装
for module, package in required_packages.items():
    try:
        __import__(module)
    except ImportError:
        print(f"正在安装依赖: {package}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

# 启动GUI
from gui import main

if __name__ == "__main__":
    main()