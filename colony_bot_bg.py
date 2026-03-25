"""
PlayColony 自动套利机器人（后台运行版）

通过 Playwright CDP 连接已有浏览器，完全后台运行，不占用鼠标和屏幕。

使用步骤：
1. 关闭所有 Chrome 窗口
2. 用以下命令启动 Chrome（开启调试端口）：
   chrome.exe --remote-debugging-port=9222 https://www.playcolony.xyz/play
3. 在游戏中连接钱包，进入 Swap Resources 界面
4. python colony_bot_bg.py calibrate   # 校准坐标（仅首次）
5. python colony_bot_bg.py test        # 测试 OCR
6. python colony_bot_bg.py run         # 监控（dry-run）
7. python colony_bot_bg.py run --live  # 实际交易
"""

import json
import time
import sys
import re
import logging
import asyncio
from datetime import datetime, timedelta
from itertools import permutations
from pathlib import Path
from typing import Optional, Dict, List
from io import BytesIO

from PIL import Image, ImageEnhance, ImageOps
import pytesseract
from playwright.async_api import async_playwright, Page

from config import (
    CALIBRATION_FILE, LOG_FILE, FEE, MIN_PROFIT,
    CHECK_INTERVAL, MAX_TRADES_PER_HOUR,
    RATE_PAIRS, RESOURCES, TESSERACT_CMD,
    TRADE_WAIT,
)

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("colony_bot_bg")

CDP_URL = "http://localhost:9222"


# ============================================================
# 浏览器连接
# ============================================================

async def connect_to_game(playwright):
    """连接到已有 Chrome 实例，返回游戏页面"""
    browser = await playwright.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = context.pages[0]
    for pg in context.pages:
        if "playcolony" in pg.url:
            page = pg
            break
    log.info(f"已连接: {page.url}")
    return browser, page


# ============================================================
# 校准
# ============================================================

async def calibrate():
    print("=" * 50)
    print("  PlayColony 坐标校准（后台版）")
    print("=" * 50)

    async with async_playwright() as p:
        browser, page = await connect_to_game(p)

        # 截图让用户在图片查看器中读取坐标
        await page.screenshot(path="calibration_screenshot.png")
        print(f"\n已保存截图: calibration_screenshot.png")
        print("请用画图工具打开截图，底部状态栏会显示鼠标坐标\n")

        steps = [
            ("rate_1_tl", "第 1 组汇率（Metal→Gas）数字区域 左上角"),
            ("rate_1_br", "第 1 组汇率（Metal→Gas）数字区域 右下角"),
            ("rate_2_tl", "第 2 组汇率（Gas→Crystal）数字区域 左上角"),
            ("rate_2_br", "第 2 组汇率（Gas→Crystal）数字区域 右下角"),
            ("rate_3_tl", "第 3 组汇率（Crystal→Metal）数字区域 左上角"),
            ("rate_3_br", "第 3 组汇率（Crystal→Metal）数字区域 右下角"),
            ("sell_dropdown", "SELL 资源下拉按钮"),
            ("sell_input", "SELL 输入框中心"),
            ("buy_dropdown", "BUY 资源下拉按钮"),
            ("trade_button", "TRADE 按钮中心"),
        ]

        coords = {}
        for key, desc in steps:
            raw = input(f"  [{desc}] 输入坐标 x,y: ").strip()
            x, y = [int(v.strip()) for v in raw.split(",")]
            coords[key] = [x, y]
            print(f"    ✓ ({x}, {y})")

        # 点开下拉菜单截图
        print("\n点击 SELL 下拉按钮，截图菜单选项位置...")
        sx, sy = coords["sell_dropdown"]
        await page.mouse.click(sx, sy)
        await asyncio.sleep(1)
        await page.screenshot(path="calibration_dropdown.png")
        print("已保存: calibration_dropdown.png")

        for res_name in ["Metal", "Gas", "Crystal"]:
            raw = input(f"  [下拉菜单 - {res_name}] 输入坐标 x,y: ").strip()
            x, y = [int(v.strip()) for v in raw.split(",")]
            coords[f"option_{res_name.lower()}"] = [x, y]
            print(f"    ✓ ({x}, {y})")

        await page.keyboard.press("Escape")
        CALIBRATION_FILE.write_text(json.dumps(coords, indent=2), encoding="utf-8")
        print(f"\n校准完成 → {CALIBRATION_FILE}")
        await browser.close()


def load_calibration() -> dict:
    if not CALIBRATION_FILE.exists():
        print("未找到校准文件，请先运行: python colony_bot_bg.py calibrate")
        sys.exit(1)
    return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))


# ============================================================
# 汇率 OCR
# ============================================================

class RateReader:
    def __init__(self, coords: dict):
        self.coords = coords

    async def read_all(self, page: Page) -> Dict[str, Optional[float]]:
        screenshot_bytes = await page.screenshot()
        full_img = Image.open(BytesIO(screenshot_bytes))
        results = {}
        for i, (sell, buy) in enumerate(RATE_PAIRS, 1):
            results[f"{sell}_{buy}"] = self._ocr_rate(full_img, i)
        return results

    def _ocr_rate(self, full_img: Image.Image, index: int) -> Optional[float]:
        tl = self.coords[f"rate_{index}_tl"]
        br = self.coords[f"rate_{index}_br"]
        crop = full_img.crop((tl[0], tl[1], br[0], br[1]))

        crop = crop.resize((crop.width * 5, crop.height * 5), Image.LANCZOS)
        crop = crop.convert("L")
        crop = ImageOps.invert(crop)
        crop = ImageEnhance.Contrast(crop).enhance(4)
        crop = crop.point(lambda p: 255 if p > 128 else 0)

        text = pytesseract.image_to_string(
            crop,
            config="--psm 7 -c tessedit_char_whitelist=0123456789.:"
        ).strip()

        m = re.search(r"(\d+\.\d+)", text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        log.warning(f"OCR 失败 (组{index}): '{text}'")
        return None

    async def debug_screenshot(self, page: Page):
        data = await page.screenshot()
        full = Image.open(BytesIO(data))
        full.save("debug_full.png")
        for i in range(1, 4):
            tl = self.coords[f"rate_{i}_tl"]
            br = self.coords[f"rate_{i}_br"]
            crop = full.crop((tl[0], tl[1], br[0], br[1]))
            crop.save(f"debug_rate_{i}_raw.png")
            proc = crop.resize((crop.width * 5, crop.height * 5), Image.LANCZOS)
            proc = proc.convert("L")
            proc = ImageOps.invert(proc)
            proc = ImageEnhance.Contrast(proc).enhance(4)
            proc = proc.point(lambda p: 255 if p > 128 else 0)
            proc.save(f"debug_rate_{i}_processed.png")


# ============================================================
# 套利引擎（复用 colony_bot 的逻辑）
# ============================================================

class ArbitrageEngine:
    def __init__(self, fee: float = FEE, min_profit: float = MIN_PROFIT):
        self.fee_mul = 1 - fee
        self.min_profit = min_profit

    def find_opportunities(self, rates: Dict[str, float]) -> List[dict]:
        full = {}
        for pair_key, rate in rates.items():
            if rate is None or rate <= 0:
                continue
            sell, buy = pair_key.split("_")
            full[(sell, buy)] = rate
            full[(buy, sell)] = 1.0 / rate

        opps = []
        for perm in permutations(RESOURCES):
            a, b, c = perm
            r_ab = full.get((a, b))
            r_bc = full.get((b, c))
            r_ca = full.get((c, a))
            if None in (r_ab, r_bc, r_ca):
                continue
            gross = r_ab * r_bc * r_ca
            net = gross * (self.fee_mul ** 3)
            opps.append({
                "path": [a, b, c, a],
                "rates": [r_ab, r_bc, r_ca],
                "gross": gross, "net": net,
                "profit": net - 1.0,
            })
        opps.sort(key=lambda x: x["profit"], reverse=True)
        return opps

    def best(self, rates: Dict[str, float]) -> Optional[dict]:
        opps = self.find_opportunities(rates)
        return opps[0] if opps and opps[0]["profit"] > self.min_profit else None


# ============================================================
# 交易执行
# ============================================================

class TradeExecutor:
    RES_KEYS = {
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
        now = datetime.now()
        if now - self.hour_start > timedelta(hours=1):
            self.trades_this_hour = 0
            self.hour_start = now
        return self.trades_this_hour < MAX_TRADES_PER_HOUR

    async def execute_path(self, page: Page, path: List[str], amount: int) -> bool:
        if not self.can_trade():
            log.warning("每小时交易上限")
            return False

        for i in range(len(path) - 1):
            sell, buy = path[i], path[i + 1]
            log.info(f"  [{i+1}/{len(path)-1}] {sell} → {buy}, 数量={amount}")
            if self.dry_run:
                log.info("  [DRY-RUN] 跳过")
                continue
            if not await self._trade(page, sell, buy, amount):
                log.error("  交易失败")
                return False
            await asyncio.sleep(TRADE_WAIT)
            self.trades_this_hour += 1
        return True

    async def _trade(self, page: Page, sell: str, buy: str, amount: int) -> bool:
        try:
            await self._click(page, "sell_dropdown")
            await asyncio.sleep(0.8)
            await self._click(page, self.RES_KEYS[sell])
            await asyncio.sleep(0.8)

            await self._click(page, "buy_dropdown")
            await asyncio.sleep(0.8)
            await self._click(page, self.RES_KEYS[buy])
            await asyncio.sleep(0.8)

            await self._click(page, "sell_input")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.type(str(amount), delay=50)
            await asyncio.sleep(1.5)

            await self._click(page, "trade_button")
            await asyncio.sleep(0.5)

            self.trade_log.append({
                "time": datetime.now().isoformat(),
                "sell": sell, "buy": buy, "amount": amount,
            })
            return True
        except Exception as e:
            log.error(f"异常: {e}")
            return False

    async def _click(self, page: Page, key: str):
        x, y = self.coords[key]
        await page.mouse.click(x, y)


# ============================================================
# 主循环
# ============================================================

class Bot:
    def __init__(self, dry_run: bool = True):
        self.coords = load_calibration()
        self.reader = RateReader(self.coords)
        self.engine = ArbitrageEngine()
        self.executor = TradeExecutor(self.coords, dry_run=dry_run)
        self.dry_run = dry_run
        self.cycle = 0
        self.total_profit = 0.0

    async def run(self):
        mode = "DRY-RUN" if self.dry_run else "⚡ LIVE"
        log.info(f"启动后台监控 [{mode}]")
        log.info(f"阈值: {MIN_PROFIT*100:.1f}% | 间隔: {CHECK_INTERVAL}s")
        log.info("Ctrl+C 停止\n")

        async with async_playwright() as p:
            browser, page = await connect_to_game(p)
            try:
                while True:
                    await self._tick(page)
                    await asyncio.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                log.info("\n停止")
                self._summary()
            finally:
                await browser.close()

    async def _tick(self, page: Page):
        self.cycle += 1
        rates = await self.reader.read_all(page)
        valid = {k: v for k, v in rates.items() if v is not None}

        if len(valid) < 3:
            log.warning(f"[#{self.cycle}] OCR 失败: {[k for k,v in rates.items() if v is None]}")
            return

        rate_str = " | ".join(f"{k}: {v:.4f}" for k, v in valid.items())
        log.info(f"[#{self.cycle}] {rate_str}")

        opps = self.engine.find_opportunities(valid)
        best = opps[0] if opps else None
        if not best:
            return

        path_str = " → ".join(best["path"])
        log.info(f"  最优: {path_str} | 净={best['net']:.6f} | 利润={best['profit']*100:+.3f}%")

        if best["profit"] > MIN_PROFIT:
            log.info(f"  ★ 套利！{best['profit']*100:.3f}%")
            amount = 5000
            ok = await self.executor.execute_path(page, best["path"], amount)
            if ok:
                profit = amount * best["profit"]
                self.total_profit += profit
                log.info(f"  完成，预计利润: {profit:.1f}")
        elif best["profit"] > 0:
            log.info(f"  ○ 微利，低于阈值")

    def _summary(self):
        log.info("=" * 40)
        log.info(f"检测: {self.cycle} | 交易: {len(self.executor.trade_log)} | 利润: {self.total_profit:.1f}")
        log.info("=" * 40)


# ============================================================
# 测试
# ============================================================

async def test_ocr():
    coords = load_calibration()
    reader = RateReader(coords)

    async with async_playwright() as p:
        browser, page = await connect_to_game(p)
        await reader.debug_screenshot(page)
        print("截图已保存: debug_full.png, debug_rate_*\n")

        rates = await reader.read_all(page)
        print("汇率:")
        for pair, rate in rates.items():
            print(f"  {pair}: {f'{rate:.4f}' if rate else '❌'}")

        valid = {k: v for k, v in rates.items() if v is not None}
        if len(valid) == 3:
            opps = ArbitrageEngine().find_opportunities(valid)
            print("\n套利分析:")
            for o in opps[:4]:
                print(f"  {' → '.join(o['path'])}: {o['profit']*100:+.3f}%")

        await browser.close()


# ============================================================
# 入口
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1].lower()
    if cmd == "calibrate":
        asyncio.run(calibrate())
    elif cmd == "test":
        asyncio.run(test_ocr())
    elif cmd == "run":
        asyncio.run(Bot(dry_run="--live" not in sys.argv).run())
    else:
        print(f"未知命令: {cmd}\n可用: calibrate | test | run [--live]")


if __name__ == "__main__":
    main()
