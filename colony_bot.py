"""
PlayColony 自动套利机器人

使用前：
1. 安装 Tesseract OCR: choco install tesseract
   或下载: https://github.com/UB-Mannheim/tesseract/wiki
2. pip install -r requirements.txt
3. 浏览器打开游戏进入 Swap Resources 界面
4. python colony_bot.py calibrate   # 校准坐标
5. python colony_bot.py test        # 测试 OCR
6. python colony_bot.py run         # 监控（dry-run）
7. python colony_bot.py run --live  # 实际交易
"""

import json
import time
import sys
import re
import logging
from datetime import datetime, timedelta
from itertools import permutations
from typing import Optional, Dict, Tuple, List

import pyautogui
from PIL import Image, ImageEnhance, ImageOps
import mss
import pytesseract

from config import (
    CALIBRATION_FILE, LOG_FILE, FEE, MIN_PROFIT,
    CHECK_INTERVAL, TRADE_RATIO, MAX_TRADES_PER_HOUR,
    RATE_PAIRS, RESOURCES, TESSERACT_CMD,
    CLICK_PAUSE, TYPE_INTERVAL, TRADE_WAIT,
)

# 初始化
pyautogui.PAUSE = CLICK_PAUSE
pyautogui.FAILSAFE = True  # 鼠标移到左上角可紧急停止
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("colony_bot")


# ============================================================
# 校准
# ============================================================

def calibrate():
    """交互式校准，记录屏幕上关键 UI 元素的坐标"""
    print("=" * 50)
    print("  PlayColony 坐标校准")
    print("=" * 50)
    print("\n请保持游戏 Swap Resources 界面打开")
    print("按照提示将鼠标移到指定位置，按 Enter 记录\n")

    steps = [
        # 汇率区域 —— 每对需要左上角和右下角
        ("rate_1_tl", "第 1 组汇率（Metal→Gas）数字区域 左上角"),
        ("rate_1_br", "第 1 组汇率（Metal→Gas）数字区域 右下角"),
        ("rate_2_tl", "第 2 组汇率（Gas→Crystal）数字区域 左上角"),
        ("rate_2_br", "第 2 组汇率（Gas→Crystal）数字区域 右下角"),
        ("rate_3_tl", "第 3 组汇率（Crystal→Metal）数字区域 左上角"),
        ("rate_3_br", "第 3 组汇率（Crystal→Metal）数字区域 右下角"),
        # Swap 界面
        ("sell_dropdown", "SELL 资源下拉按钮（小箭头）"),
        ("sell_input", "SELL 输入框中心"),
        ("buy_dropdown", "BUY 资源下拉按钮（小箭头）"),
        ("trade_button", "TRADE 按钮中心"),
        # 下拉菜单选项 —— 先点击 SELL 的下拉按钮再标记
        ("option_metal", "下拉菜单中 Metal 选项"),
        ("option_gas", "下拉菜单中 Gas 选项"),
        ("option_crystal", "下拉菜单中 Crystal 选项"),
    ]

    coords = {}
    for key, desc in steps:
        input(f"  → 鼠标移到 [{desc}]，按 Enter...")
        pos = pyautogui.position()
        coords[key] = [pos.x, pos.y]
        print(f"    ✓ ({pos.x}, {pos.y})")

    CALIBRATION_FILE.write_text(json.dumps(coords, indent=2), encoding="utf-8")
    print(f"\n校准完成，已保存到 {CALIBRATION_FILE}")
    return coords


def load_calibration() -> dict:
    """加载校准数据"""
    if not CALIBRATION_FILE.exists():
        print("未找到校准文件，请先运行: python colony_bot.py calibrate")
        sys.exit(1)
    return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))


# ============================================================
# 汇率读取 (OCR)
# ============================================================

class RateReader:
    """从屏幕截图读取三组汇率"""

    def __init__(self, coords: dict):
        self.coords = coords
        self.sct = mss.mss()

    def read_all(self) -> Dict[str, Optional[float]]:
        """读取三组汇率，返回 {pair_key: rate}"""
        results = {}
        for i, (sell, buy) in enumerate(RATE_PAIRS, 1):
            pair_key = f"{sell}_{buy}"
            rate = self._read_one(i)
            results[pair_key] = rate
        return results

    def _read_one(self, index: int) -> Optional[float]:
        """读取第 index 组汇率"""
        tl = self.coords[f"rate_{index}_tl"]
        br = self.coords[f"rate_{index}_br"]
        region = {
            "left": tl[0],
            "top": tl[1],
            "width": br[0] - tl[0],
            "height": br[1] - tl[1],
        }

        screenshot = self.sct.grab(region)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        # 预处理：放大 → 灰度 → 反转 → 增强对比度 → 二值化
        img = img.resize((img.width * 5, img.height * 5), Image.LANCZOS)
        img = img.convert("L")
        img = ImageOps.invert(img)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(4)
        img = img.point(lambda p: 255 if p > 128 else 0)

        text = pytesseract.image_to_string(
            img,
            config="--psm 7 -c tessedit_char_whitelist=0123456789.:"
        ).strip()

        # 匹配 "1:X.XX" 或单独的 "X.XX"
        m = re.search(r"(\d+\.\d+)", text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

        log.warning(f"OCR 识别失败 (组{index}): '{text}'")
        return None

    def debug_screenshot(self, index: int):
        """保存预处理后的截图用于调试"""
        tl = self.coords[f"rate_{index}_tl"]
        br = self.coords[f"rate_{index}_br"]
        region = {
            "left": tl[0],
            "top": tl[1],
            "width": br[0] - tl[0],
            "height": br[1] - tl[1],
        }
        screenshot = self.sct.grab(region)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img.save(f"debug_rate_{index}_raw.png")

        img = img.resize((img.width * 5, img.height * 5), Image.LANCZOS)
        img = img.convert("L")
        img = ImageOps.invert(img)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(4)
        img = img.point(lambda p: 255 if p > 128 else 0)
        img.save(f"debug_rate_{index}_processed.png")


# ============================================================
# 套利计算
# ============================================================

class ArbitrageEngine:
    """计算三角套利机会"""

    def __init__(self, fee: float = FEE, min_profit: float = MIN_PROFIT):
        self.fee = fee
        self.min_profit = min_profit
        self.fee_multiplier = 1 - fee  # 0.997

    def find_opportunities(
        self, rates: Dict[str, float]
    ) -> List[dict]:
        """
        输入: {"Metal_Gas": 0.98, "Gas_Crystal": 1.02, "Crystal_Metal": 1.00}
        输出: 所有可行的套利路径列表
        """
        # 构建完整汇率表（正反向）
        full_rates = {}
        for pair_key, rate in rates.items():
            if rate is None or rate <= 0:
                continue
            sell, buy = pair_key.split("_")
            full_rates[(sell, buy)] = rate
            full_rates[(buy, sell)] = 1.0 / rate

        # 枚举所有三角路径
        opportunities = []
        for perm in permutations(RESOURCES):
            a, b, c = perm
            r_ab = full_rates.get((a, b))
            r_bc = full_rates.get((b, c))
            r_ca = full_rates.get((c, a))
            if r_ab is None or r_bc is None or r_ca is None:
                continue

            gross = r_ab * r_bc * r_ca
            net = gross * (self.fee_multiplier ** 3)
            profit = net - 1.0

            opportunities.append({
                "path": [a, b, c, a],
                "rates": [r_ab, r_bc, r_ca],
                "gross": gross,
                "net": net,
                "profit": profit,
            })

        # 按利润降序
        opportunities.sort(key=lambda x: x["profit"], reverse=True)
        return opportunities

    def best_opportunity(self, rates: Dict[str, float]) -> Optional[dict]:
        """返回最优的套利机会（利润 > 阈值），无则返回 None"""
        opps = self.find_opportunities(rates)
        if opps and opps[0]["profit"] > self.min_profit:
            return opps[0]
        return None


# ============================================================
# 交易执行
# ============================================================

class TradeExecutor:
    """通过模拟鼠标点击执行 Swap 交易"""

    RESOURCE_OPTIONS = {
        "Metal": "option_metal",
        "Gas": "option_gas",
        "Crystal": "option_crystal",
    }

    def __init__(self, coords: dict, dry_run: bool = True):
        self.coords = coords
        self.dry_run = dry_run
        self.trade_log: List[dict] = []
        self.trades_this_hour = 0
        self.hour_start = datetime.now()

    def can_trade(self) -> bool:
        """检查是否超过每小时交易次数限制"""
        now = datetime.now()
        if now - self.hour_start > timedelta(hours=1):
            self.trades_this_hour = 0
            self.hour_start = now
        return self.trades_this_hour < MAX_TRADES_PER_HOUR

    def execute_path(self, path: List[str], amount: int) -> bool:
        """
        执行三角套利路径
        path: ["Metal", "Gas", "Crystal", "Metal"]
        amount: 初始交易数量
        """
        if not self.can_trade():
            log.warning("已达到每小时交易上限")
            return False

        current_amount = amount
        for i in range(len(path) - 1):
            sell_res = path[i]
            buy_res = path[i + 1]
            log.info(f"  交易 {i+1}/3: {sell_res} → {buy_res}, 数量={current_amount}")

            if self.dry_run:
                log.info(f"  [DRY-RUN] 跳过实际交易")
                continue

            ok = self._do_single_trade(sell_res, buy_res, current_amount)
            if not ok:
                log.error(f"  交易失败！中断套利路径")
                return False

            # 等待交易确认
            time.sleep(TRADE_WAIT)
            self.trades_this_hour += 1

        return True

    def _do_single_trade(self, sell: str, buy: str, amount: int) -> bool:
        """执行单笔交易"""
        try:
            # 1. 选择 SELL 资源
            self._click("sell_dropdown")
            time.sleep(0.5)
            self._click(self.RESOURCE_OPTIONS[sell])
            time.sleep(0.5)

            # 2. 选择 BUY 资源
            self._click("buy_dropdown")
            time.sleep(0.5)
            self._click(self.RESOURCE_OPTIONS[buy])
            time.sleep(0.5)

            # 3. 输入数量
            self._click("sell_input")
            time.sleep(0.3)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.typewrite(str(amount), interval=TYPE_INTERVAL)
            time.sleep(1.0)  # 等待汇率计算

            # 4. 点击 TRADE
            self._click("trade_button")
            time.sleep(0.5)

            self.trade_log.append({
                "time": datetime.now().isoformat(),
                "sell": sell,
                "buy": buy,
                "amount": amount,
            })
            return True

        except Exception as e:
            log.error(f"交易执行异常: {e}")
            return False

    def _click(self, coord_key: str):
        """点击校准坐标"""
        x, y = self.coords[coord_key]
        pyautogui.click(x, y)


# ============================================================
# 主循环
# ============================================================

class Bot:
    """套利机器人主控"""

    def __init__(self, dry_run: bool = True):
        self.coords = load_calibration()
        self.reader = RateReader(self.coords)
        self.engine = ArbitrageEngine()
        self.executor = TradeExecutor(self.coords, dry_run=dry_run)
        self.dry_run = dry_run
        self.cycle_count = 0
        self.total_profit = 0.0

    def run(self):
        """持续监控汇率，发现套利机会立即执行"""
        mode_str = "DRY-RUN" if self.dry_run else "LIVE"
        log.info(f"启动套利监控 [{mode_str}]")
        log.info(f"最低利润阈值: {MIN_PROFIT*100:.1f}%")
        log.info(f"检测间隔: {CHECK_INTERVAL}s")
        log.info("按 Ctrl+C 停止\n")

        try:
            while True:
                self._tick()
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log.info("\n已停止监控")
            self._print_summary()

    def _tick(self):
        """单次检测周期"""
        self.cycle_count += 1

        # 读取汇率
        rates = self.reader.read_all()
        valid = {k: v for k, v in rates.items() if v is not None}

        if len(valid) < 3:
            failed = [k for k, v in rates.items() if v is None]
            log.warning(f"[#{self.cycle_count}] OCR 识别失败: {failed}")
            return

        # 显示汇率
        rate_str = " | ".join(
            f"{k}: {v:.4f}" for k, v in valid.items()
        )
        log.info(f"[#{self.cycle_count}] 汇率: {rate_str}")

        # 计算套利
        opps = self.engine.find_opportunities(valid)
        best = opps[0] if opps else None

        if best:
            path_str = " → ".join(best["path"])
            log.info(
                f"  最优路径: {path_str} | "
                f"毛收益: {best['gross']:.6f} | "
                f"净收益: {best['net']:.6f} | "
                f"利润率: {best['profit']*100:+.3f}%"
            )

            if best["profit"] > MIN_PROFIT:
                log.info(f"  ★ 发现套利机会！利润率 {best['profit']*100:.3f}%")
                self._execute_arbitrage(best)
            elif best["profit"] > 0:
                log.info(f"  ○ 有微利但低于阈值 {MIN_PROFIT*100:.1f}%")

    def _execute_arbitrage(self, opp: dict):
        """执行套利"""
        path = opp["path"]
        # 计算交易数量（使用固定比例）
        amount = 5000  # 默认交易量，可根据余额动态调整

        log.info(f"  执行交易: {' → '.join(path)}, 数量={amount}")
        ok = self.executor.execute_path(path, amount)
        if ok:
            estimated_profit = amount * opp["profit"]
            self.total_profit += estimated_profit
            log.info(f"  交易完成，预计利润: {estimated_profit:.1f}")

    def _print_summary(self):
        """打印运行总结"""
        log.info("=" * 50)
        log.info(f"总检测次数: {self.cycle_count}")
        log.info(f"总交易次数: {len(self.executor.trade_log)}")
        log.info(f"累计预计利润: {self.total_profit:.1f}")
        log.info("=" * 50)


# ============================================================
# 测试模式
# ============================================================

def test_ocr():
    """测试 OCR 识别准确性"""
    coords = load_calibration()
    reader = RateReader(coords)

    print("开始测试 OCR 识别...\n")
    for i in range(1, 4):
        reader.debug_screenshot(i)
        print(f"  截图已保存: debug_rate_{i}_raw.png / debug_rate_{i}_processed.png")

    print("\n读取汇率:")
    rates = reader.read_all()
    for pair, rate in rates.items():
        status = f"{rate:.4f}" if rate else "❌ 失败"
        print(f"  {pair}: {status}")

    # 计算套利
    valid = {k: v for k, v in rates.items() if v is not None}
    if len(valid) == 3:
        engine = ArbitrageEngine()
        opps = engine.find_opportunities(valid)
        print("\n套利分析:")
        for opp in opps[:4]:
            path_str = " → ".join(opp["path"])
            print(
                f"  {path_str}: "
                f"毛收益={opp['gross']:.6f} "
                f"净收益={opp['net']:.6f} "
                f"利润={opp['profit']*100:+.3f}%"
            )


# ============================================================
# 入口
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "calibrate":
        calibrate()
    elif cmd == "test":
        test_ocr()
    elif cmd == "run":
        live = "--live" in sys.argv
        bot = Bot(dry_run=not live)
        bot.run()
    else:
        print(f"未知命令: {cmd}")
        print("可用命令: calibrate | test | run [--live]")


if __name__ == "__main__":
    main()
