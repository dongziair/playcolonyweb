"""
PlayColony 链上自动套利脚本

完全脱离浏览器，通过私钥直接与 Solana 链上合约交互。

使用步骤：
1. pip install solana solders
2. 编辑 .env 文件，填入你的 base58 私钥
3. python colony_onchain.py discover   # 自动发现用户 PDA 地址
4. python colony_onchain.py rates      # 查看当前汇率
5. python colony_onchain.py monitor    # 持续监控（dry-run）
6. python colony_onchain.py monitor --live  # 实际交易
"""

import json
import struct
import time
import sys
import os
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message

# ============================================================
# 常量
# ============================================================

RPC_URL = "https://as.magicblock.app/"
RESOURCE_PROGRAM = Pubkey.from_string("2K2374VEqxbFJWycxoj8ub2wBk7KwwnNn7M5V7QsL9r2")
POOL_STATE = Pubkey.from_string("AdQJrDXwWAeBPc254qnLBCWfyTqJqoAahRgZ4kok3PZD")

SWAP_DISCRIMINATOR = bytes.fromhex("8dac0ad04509389a")
COLLECT_DISCRIMINATOR = bytes.fromhex("49047707f2ff1de2")

METAL = 0
GAS = 1
CRYSTAL = 2
RESOURCE_NAMES = {0: "Metal", 1: "Gas", 2: "Crystal"}

# 交易参数
FEE = 0.003
MIN_PROFIT = 0.0
CHECK_INTERVAL_MIN = 5
CHECK_INTERVAL_MAX = 10
SWAP_AMOUNT = 10000

# 文件路径
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
PDA_FILE = BASE_DIR / "user_pdas.json"
LOG_FILE = BASE_DIR / "onchain_log.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("colony_onchain")

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58_ALPHABET.index(c)
    if n == 0:
        return b"\x00"
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


# ============================================================
# .env 加载 + 密钥加载
# ============================================================

def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if val and val != "在这里填入你的base58私钥":
                    os.environ.setdefault(key, val)


def load_keypair() -> Keypair:
    load_env()
    pk = os.environ.get("COLONY_PRIVATE_KEY")
    if pk:
        try:
            return Keypair.from_base58_string(pk)
        except ValueError:
            raw = b58decode(pk)
            log.info(f"key 字符数: {len(pk)}, 解码字节数: {len(raw)}")
            if len(raw) >= 64:
                return Keypair.from_bytes(raw[:64])
            return Keypair.from_bytes(raw)

    path = os.environ.get("COLONY_KEYPAIR_PATH")
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text())
        return Keypair.from_bytes(bytes(data))

    print("请在 .env 文件中填入私钥:")
    print(f"  文件路径: {ENV_FILE}")
    print("  格式: COLONY_PRIVATE_KEY=你的base58私钥")
    sys.exit(1)


# ============================================================
# 链上数据读取
# ============================================================

class PoolReader:

    def __init__(self, rpc: Client):
        self.rpc = rpc

    def read_pool_data(self) -> bytes:
        acct = self.rpc.get_account_info(POOL_STATE, commitment=Confirmed)
        if not acct.value:
            raise RuntimeError("无法读取资源池账户")
        return bytes(acct.value.data)

    def get_rates(self) -> Dict[str, float]:
        """读取并解析三组汇率

        数据结构 (120 bytes):
        [0:8]   Borsh discriminator
        [8:40]  Entity Pubkey (32 bytes)
        [40:42] Header (u16)
        [42:120] 3 组 TradingPool, 每组 26 bytes:
            u8  resource_a
            u8  resource_b
            u64 reserve_a
            u64 reserve_b
            u64 k
        """
        data = self.read_pool_data()
        rates = {}
        for pool_idx in range(3):
            base = 42 + pool_idx * 26
            res_a = data[base]
            res_b = data[base + 1]
            reserve_a = struct.unpack("<Q", data[base + 2 : base + 10])[0]
            reserve_b = struct.unpack("<Q", data[base + 10 : base + 18])[0]
            if reserve_a == 0 or reserve_b == 0:
                continue
            rate = reserve_b / reserve_a
            name_a = RESOURCE_NAMES.get(res_a, str(res_a))
            name_b = RESOURCE_NAMES.get(res_b, str(res_b))
            rates[f"{name_a}_{name_b}"] = rate
        return rates


# ============================================================
# PDA 发现
# ============================================================

class PDADiscovery:

    def __init__(self, rpc: Client, user: Pubkey):
        self.rpc = rpc
        self.user = user

    def discover(self) -> dict:
        log.info(f"钱包地址: {self.user}")
        pdas = self._search_pool_txs()
        if pdas:
            return pdas
        pdas = self._search_user_txs()
        if pdas:
            return pdas
        log.warning("未找到 swap 交易记录")
        log.warning("请先在游戏中手动执行一次 swap，然后重新运行 discover")
        return {}

    def _search_pool_txs(self) -> Optional[dict]:
        log.info("搜索 pool 账户交易记录...")
        sigs = self.rpc.get_signatures_for_address(POOL_STATE, limit=200)
        user_str = str(self.user)
        for sig_info in sigs.value:
            pdas = self._check_tx(sig_info.signature, user_str)
            if pdas:
                return pdas
        return None

    def _search_user_txs(self) -> Optional[dict]:
        log.info("搜索用户钱包交易记录...")
        sigs = self.rpc.get_signatures_for_address(self.user, limit=200)
        user_str = str(self.user)
        for sig_info in sigs.value:
            pdas = self._check_tx(sig_info.signature, user_str)
            if pdas:
                return pdas
        return None

    def _check_tx(self, signature, user_str: str) -> Optional[dict]:
        """从真实交易中提取 PDA 地址和 writable 标记"""
        tx = self.rpc.get_transaction(
            signature, max_supported_transaction_version=0
        )
        if not tx.value:
            return None

        msg = tx.value.transaction.transaction.message
        acct_keys = [str(k) for k in msg.account_keys]

        if user_str not in acct_keys:
            return None

        # 解析 message header 确定每个 account 的 writable 属性
        n_sigs = msg.header.num_required_signatures
        n_ro_signed = msg.header.num_readonly_signed_accounts
        n_ro_unsigned = msg.header.num_readonly_unsigned_accounts
        n_total = len(acct_keys)

        def is_writable(idx):
            is_signer = idx < n_sigs
            if is_signer:
                return idx < n_sigs - n_ro_signed
            return idx < n_total - n_ro_unsigned

        for ix in msg.instructions:
            raw = b58decode(ix.data)
            if raw[:8] != SWAP_DISCRIMINATOR:
                continue

            names = ["signer", "player_entity", "player_component",
                     "pool_state", "player_data", "user_state"]
            pdas = {}
            writable_flags = {}

            for j, idx in enumerate(ix.accounts):
                if j >= len(names):
                    break
                name = names[j]
                pdas[name] = acct_keys[idx]
                writable_flags[name] = is_writable(idx)

            pdas["_writable"] = writable_flags

            log.info("找到 PDA（含 writable 标记）:")
            for name in names:
                w = "W" if writable_flags.get(name) else "R"
                log.info(f"  {name}: {pdas[name]}  [{w}]")
            return pdas

        return None


# ============================================================
# 交易构建 & 发送
# ============================================================

class SwapExecutor:
    RES_MAP = {"Metal": 0, "Gas": 1, "Crystal": 2}

    def __init__(self, rpc: Client, keypair: Keypair, pdas: dict, dry_run=True):
        self.rpc = rpc
        self.keypair = keypair
        self.pdas = pdas
        self.dry_run = dry_run
        # 从 pdas 中加载 writable 标记（来自真实交易）
        self.writable = pdas.get("_writable", {})

    def _build_swap_ix(self, sell_type: int, buy_type: int, amount: int) -> Instruction:
        swap_data = (SWAP_DISCRIMINATOR
                     + struct.pack('<BB', sell_type, buy_type)
                     + struct.pack('<Q', amount)
                     + struct.pack('<Q', 0))

        w = self.writable

        swap_accounts = [
            AccountMeta(self.keypair.pubkey(),
                        is_signer=True,
                        is_writable=w.get("signer", True)),
            AccountMeta(Pubkey.from_string(self.pdas["player_entity"]),
                        is_signer=False,
                        is_writable=w.get("player_entity", False)),
            AccountMeta(Pubkey.from_string(self.pdas["player_component"]),
                        is_signer=False,
                        is_writable=w.get("player_component", False)),
            AccountMeta(POOL_STATE,
                        is_signer=False,
                        is_writable=w.get("pool_state", True)),
            AccountMeta(Pubkey.from_string(self.pdas["player_data"]),
                        is_signer=False,
                        is_writable=w.get("player_data", True)),
            AccountMeta(Pubkey.from_string(self.pdas["user_state"]),
                        is_signer=False,
                        is_writable=w.get("user_state", True)),
        ]
        return Instruction(RESOURCE_PROGRAM, swap_data, swap_accounts)

    def execute_swap(self, sell: str, buy: str, amount: int = SWAP_AMOUNT) -> Optional[str]:
        sell_type = self.RES_MAP[sell]
        buy_type = self.RES_MAP[buy]
        ix = self._build_swap_ix(sell_type, buy_type, amount)

        if self.dry_run:
            log.info(f"  [DRY-RUN] {sell} → {buy}, amount={amount}")
            try:
                blockhash = self.rpc.get_latest_blockhash(Confirmed).value.blockhash
                msg = Message.new_with_blockhash([ix], self.keypair.pubkey(), blockhash)
                tx = Transaction.new_unsigned(msg)
                tx.sign([self.keypair], blockhash)
                sim = self.rpc.simulate_transaction(tx)
                if sim.value.err:
                    log.warning(f"  模拟失败: {sim.value.err}")
                    for line in (sim.value.logs or []):
                        log.warning(f"    {line}")
                    return None
                else:
                    log.info(f"  模拟成功 ✓")
                    return "simulated"
            except Exception as e:
                log.warning(f"  模拟异常: {e}")
                return None

        try:
            blockhash = self.rpc.get_latest_blockhash(Confirmed).value.blockhash
            msg = Message.new_with_blockhash([ix], self.keypair.pubkey(), blockhash)
            tx = Transaction.new_unsigned(msg)
            tx.sign([self.keypair], blockhash)
            result = self.rpc.send_transaction(tx)
            sig = str(result.value)
            log.info(f"  交易已发送: {sig}")
            return sig
        except Exception as e:
            log.error(f"  发送失败: {e}")
            return None


# ============================================================
# Bot 主循环
# ============================================================

class Bot:

    def __init__(self, dry_run=True):
        self.keypair = load_keypair()
        self.rpc = Client(RPC_URL)

        if not PDA_FILE.exists():
            log.error("请先运行: python colony_onchain.py discover")
            sys.exit(1)
        self.pdas = json.loads(PDA_FILE.read_text())

        self.pool_reader = PoolReader(self.rpc)
        self.executor = SwapExecutor(self.rpc, self.keypair, self.pdas, dry_run)
        self.dry_run = dry_run
        self.cycle = 0

    def run(self):
        mode = "DRY-RUN" if self.dry_run else "⚡ LIVE"
        log.info(f"启动链上套利监控 [{mode}]")
        log.info(f"钱包: {self.keypair.pubkey()}")
        log.info(f"阈值: >{FEE*100:.1f}%手续费 | 间隔: {CHECK_INTERVAL_MIN}-{CHECK_INTERVAL_MAX}s | 每笔: {SWAP_AMOUNT}")
        log.info("Ctrl+C 停止\n")

        try:
            while True:
                self._tick()
                delay = random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX)
                time.sleep(delay)
        except KeyboardInterrupt:
            log.info("\n已停止")

    def _tick(self):
        self.cycle += 1
        try:
            rates = self.pool_reader.get_rates()
        except Exception as e:
            log.warning(f"[#{self.cycle}] 读取失败: {e}")
            return

        if len(rates) < 3:
            log.warning(f"[#{self.cycle}] 汇率不完整: {rates}")
            return

        rate_str = " | ".join(f"{k}: {v:.4f}" for k, v in rates.items())
        log.info(f"[#{self.cycle}] {rate_str}")

        # 收集所有有利可图的交易机会
        fee_mul = 1 - FEE
        opportunities = []
        for pair, rate in rates.items():
            res_a, res_b = pair.split("_")
            net_ab = rate * fee_mul
            net_ba = (1.0 / rate) * fee_mul

            if net_ab > 1 + MIN_PROFIT:
                profit = (net_ab - 1) * 100
                opportunities.append((profit, res_a, res_b))
            if net_ba > 1 + MIN_PROFIT:
                profit = (net_ba - 1) * 100
                opportunities.append((profit, res_b, res_a))

        if not opportunities:
            return

        # 按利润从高到低排序
        opportunities.sort(key=lambda x: x[0], reverse=True)

        # 依次尝试，失败就换下一个
        for profit, sell, buy in opportunities:
            log.info(f"  ★ {sell}→{buy} 净利润 {profit:.2f}%")
            result = self.executor.execute_swap(sell, buy)
            if result:
                return
            log.info(f"  {sell}→{buy} 失败，尝试下一对...")


# ============================================================
# 命令行入口
# ============================================================

def cmd_discover():
    kp = load_keypair()
    rpc = Client(RPC_URL)
    log.info(f"钱包地址: {kp.pubkey()}")
    disc = PDADiscovery(rpc, kp.pubkey())
    pdas = disc.discover()
    if pdas:
        PDA_FILE.write_text(json.dumps(pdas, indent=2))
        log.info(f"PDA 已保存到 {PDA_FILE}")


def cmd_rates():
    rpc = Client(RPC_URL)
    reader = PoolReader(rpc)
    rates = reader.get_rates()
    fee_mul = 1 - FEE
    print("\n当前汇率:")
    for pair, rate in rates.items():
        res_a, res_b = pair.split("_")
        net = rate * fee_mul
        inv_net = (1.0 / rate) * fee_mul
        status_ab = f"+{(net-1)*100:.2f}%" if net > 1 else f"{(net-1)*100:.2f}%"
        status_ba = f"+{(inv_net-1)*100:.2f}%" if inv_net > 1 else f"{(inv_net-1)*100:.2f}%"
        print(f"  {res_a}/{res_b}: {rate:.6f}  卖{res_a}买{res_b}={status_ab}  卖{res_b}买{res_a}={status_ba}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    if cmd == "discover":
        cmd_discover()
    elif cmd == "rates":
        cmd_rates()
    elif cmd == "monitor":
        live = "--live" in sys.argv
        Bot(dry_run=not live).run()
    else:
        print(f"未知命令: {cmd}\n可用: discover | rates | monitor [--live]")


if __name__ == "__main__":
    main()
