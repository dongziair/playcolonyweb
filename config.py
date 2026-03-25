"""配置文件"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CALIBRATION_FILE = BASE_DIR / "calibration.json"
LOG_FILE = BASE_DIR / "trade_log.txt"

# 交易参数
FEE = 0.003            # 0.3% 手续费
MIN_PROFIT = 0.003     # 最小利润率阈值（扣费后净利润 > 此值才交易）
CHECK_INTERVAL = 5     # 汇率检测间隔（秒）
TRADE_RATIO = 0.15     # 每次交易投入的资源占比
MAX_TRADES_PER_HOUR = 30

# 资源名称
RESOURCES = ["Metal", "Gas", "Crystal"]

# 汇率对方向（顶部栏从左到右显示的顺序）
# 每对: (卖出资源, 买入资源)
RATE_PAIRS = [
    ("Metal", "Gas"),
    ("Gas", "Crystal"),
    ("Crystal", "Metal"),
]

# Tesseract OCR 路径
if os.name == "nt":
    TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
else:
    TESSERACT_CMD = "tesseract"

# pyautogui 安全设置
CLICK_PAUSE = 0.3      # 每次点击后暂停（秒）
TYPE_INTERVAL = 0.05   # 打字间隔
TRADE_WAIT = 3.0       # 交易确认等待时间（秒）
