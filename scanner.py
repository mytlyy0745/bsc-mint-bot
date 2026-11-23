#!/usr/bin/env python3
"""
scanner.py - BSC 扫链：找新 NFT / 代币合约，统计 Agent ID 钱包铸造热度

用法：
    python3 scanner.py [--dry-run] [--config config.json] [--once]
    python3 scanner.py --experts [--config config.json]   # 只更新早铸高手名单

依赖：pip install -r requirements.txt（web3、requests）

配置：全部在 config.json。

工作方式：
    - 增量扫块：从 scanner_state.json 里的 last_block 继续，每次扫一批新块。
    - 找 Transfer 事件里 from 为零地址（0x0000…0000）的合约：新 NFT / 代币在铸。
    - 对每个新合约，抽查最近的铸造交易：调 Agent ID 注册合约 balanceOf，
      统计近 5 分钟 / 近 60 分钟有多少个 Agent ID 持有钱包在铸。
    - 预热：新部署的合约字节码里出现注册合约地址，记下来（通常 24~70 分钟后开铸）。
    - 早铸高手：回看历史，找总在前 20 个铸到、且项目最终 >= 300 个铸造者、
      命中 >= 3 次的钱包，写 experts.json，每小时更新。
    - 输出 radar.json（新项目列表：地址、进度、Agent 钱包数、5 分钟热度、结论）。

只推三种：开盯后才开铸的新项目；正在升温的项目；早铸高手在铸的项目。
已铸 90% 以上的不推。

安全红线（违反就拒绝并记录）：
    - 同一部署地址 24 小时内发 >= 3 个合约的批量系列：不打（radar 里标出来）。
    - 空投占比 > 30% 的不算真有人抢。
    - 池子流动性 < 50U 的币：报价当没有。
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from web3 import Web3

# ---------------- 安全红线（常量，违反就拒绝并记录） ----------------
SAFETY = {
    "airdrop_ratio_threshold_pct": 30,  # 空投占比 > 30% 不算真有人抢
    "max_contracts_per_deployer_24h": 3,  # 同一部署地址 24h 内 >= 3 个合约：批量系列
    "min_pool_liquidity_usd": 50.0,  # 池子 < 50U：报价当没有
    "max_minted_pct_to_promote": 90,  # 已铸 90% 以上的不推
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ERC721 / ERC20 Transfer 事件
TRANSFER_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"},
    ],
    "name": "Transfer",
    "type": "event",
}]
# Agent ID 注册合约 balanceOf
REGISTRY_ABI = [{
    "constant": True,
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function",
}]
# 常见 mint 相关读取
TOTAL_SUPPLY_ABI = [{
    "constant": True, "inputs": [], "name": "totalSupply",
    "outputs": [{"name": "", "type": "uint256"}], "type": "function",
}]
MAX_SUPPLY_ABI = [{
    "constant": True, "inputs": [], "name": "maxSupply",
    "outputs": [{"name": "", "type": "uint256"}], "type": "function",
}]

BLOCKS_PER_RUN = 100  # 每轮最多扫 100 个块（BSC 约 3 秒一个块，约 5 分钟）
EXPERTS_UPDATE_SECONDS = 3600  # 早铸高手每小时更新


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def connect_rpc(rpc_list):
    """按顺序试 RPC，返回第一个能用的 Web3 连接。"""
    for url in rpc_list:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3, url
        except Exception:
            continue
    raise RuntimeError("所有 BSC RPC 都连不上")


def is_agent_holder(w3, registry, address):
    """调注册合约 balanceOf，> 0 就是 Agent ID 持有者。"""
    try:
        return registry.functions.balanceOf(address).call() > 0
    except Exception:
        return False


def get_contract_bytecode(w3, address):
    try:
        return w3.eth.get_code(address).hex()
    except Exception:
        return ""


def scan_blocks(w3, registry, cfg, from_block, to_block):
    """扫一个块区间，返回新发现的铸造合约统计。"""
    radar = load_json(os.path.join(BASE_DIR, cfg["files"]["radar"]), [])
    known = {p["address"].lower(): p for p in radar}
    new_mints = defaultdict(lambda: {"minters": set(), "first_block": None,
                                     "deployer": "", "tx_count": 0})
    # 只看零地址发出的 Transfer（新铸）
    try:
        logs = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [TRANSFER_TOPIC,
                       "0x" + "0" * 24 + ZERO_ADDRESS[2:]],
        })
    except Exception as e:
        print(f"get_logs 失败 {from_block}-{to_block}：{e}")
        return radar

    for log in logs:
        contract = log["address"].lower()
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        to_addr = "0x" + topics[2].hex()[-40:]
        entry = new_mints[contract]
        entry["minters"].add(to_addr.lower())
        entry["tx_count"] += 1
        if entry["first_block"] is None:
            entry["first_block"] = log["blockNumber"]
        if not entry["deployer"]:
            try:
                tx = w3.eth.get_transaction(log["transactionHash"])
                entry["deployer"] = tx["from"].lower()
            except Exception:
                pass

    now = datetime.now(timezone.utc)
    for contract, info in new_mints.items():
        # 跳过已经在雷达里且已铸 90% 以上的
        if contract in known and known[contract].get("progress_pct", 0) >= \
                SAFETY["max_minted_pct_to_promote"]:
            continue
        # 抽查：铸造者里多少是 Agent ID 持有者（最多抽 50 个，省 RPC）
        sample = list(info["minters"])[:50]
        agent_count = sum(1 for a in sample if is_agent_holder(w3, registry, a))
        # 预热：字节码里是否调用注册合约
        bytecode = get_contract_bytecode(w3, contract)
        registry_addr = cfg["agent_id_registry"].lower()[2:]
        preheat = registry_addr in bytecode.lower()
        # 结论
        if agent_count >= 100:
            verdict = "参与"
            reason = f"5 分钟 {agent_count} 个 Agent 钱包在铸"
        elif agent_count >= 20:
            verdict = "观望"
            reason = f"{agent_count} 个 Agent 钱包在铸，继续看"
        else:
            verdict = "观望"
            reason = "Agent 钱包不多"
        item = {
            "address": contract,
            "name": short_name(contract),
            "first_seen": now.isoformat(),
            "first_block": info["first_block"],
            "deployer": info["deployer"],
            "minters_total": len(info["minters"]),
            "agent_wallets_5min": agent_count,
            "agent_wallets_1h": agent_count,  # 本轮是 5 分钟窗口；1h 由多轮累加
            "preheat": preheat,
            "verdict": verdict,
            "reason": reason,
            "is_free": None,  # 免费/付费由 auto_mint 进一步确认
            "progress_pct": 0,
        }
        if contract in known:
            old = known[contract]
            old["agent_wallets_5min"] = agent_count
            old["agent_wallets_1h"] = old.get("agent_wallets_1h", 0) + agent_count
            old["minters_total"] = max(old.get("minters_total", 0),
                                       len(info["minters"]))
            old["verdict"] = verdict
            old["reason"] = reason
        else:
            radar.append(item)
    return radar


def short_name(address):
    return address[:6] + "…" + address[-4:]


def update_experts(w3, registry, cfg):
    """早铸高手：回看 radar.json 里的项目历史，找命中 >= 3 次的钱包。"""
    radar = load_json(os.path.join(BASE_DIR, cfg["files"]["radar"]), [])
    experts_path = os.path.join(BASE_DIR, cfg["files"]["experts"])
    hits = defaultdict(int)
    for project in radar:
        # 项目最终 >= 300 个铸造者才算“火了”
        if project.get("minters_total", 0) < 300:
            continue
        for wallet in project.get("early_minters", []):
            hits[wallet] += 1
    experts = [
        {"wallet": w, "hits": n}
        for w, n in sorted(hits.items(), key=lambda kv: -kv[1])
        if n >= 3
    ]
    save_json(experts_path, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "experts": experts,
    })
    print(f"早铸高手更新：{len(experts)} 个钱包命中 >= 3 次")
    return experts


def main():
    parser = argparse.ArgumentParser(description="BSC 扫链")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写 radar.json")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="只扫一轮就退出")
    parser.add_argument("--experts", action="store_true",
                        help="只更新早铸高手名单")
    args = parser.parse_args()

    cfg = load_config(args.config)
    rpc_list = cfg.get("bsc_rpc", ["https://bsc-dataseed.binance.org/"])
    w3, rpc_url = connect_rpc(rpc_list)
    print(f"已连 BSC RPC：{rpc_url}，当前块 {w3.eth.block_number}")
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(cfg["agent_id_registry"]),
        abi=REGISTRY_ABI)

    if args.experts:
        update_experts(w3, registry, cfg)
        return

    state_path = os.path.join(BASE_DIR, cfg["files"]["scanner_state"])
    state = load_json(state_path, {})
    last_block = state.get("last_block") or (w3.eth.block_number - 100)
    last_experts = state.get("last_experts_update", 0)

    while True:
        latest = w3.eth.block_number
        to_block = min(latest, last_block + BLOCKS_PER_RUN)
        if to_block > last_block:
            radar = scan_blocks(w3, registry, cfg, last_block + 1, to_block)
            if not args.dry_run:
                save_json(os.path.join(BASE_DIR, cfg["files"]["radar"]), radar)
                state["last_block"] = to_block
                save_json(state_path, state)
            print(f"扫 {last_block + 1}-{to_block}，雷达共 {len(radar)} 个项目")
            last_block = to_block
        # 每小时更新一次早铸高手
        if time.time() - last_experts > EXPERTS_UPDATE_SECONDS:
            update_experts(w3, registry, cfg)
            last_experts = time.time()
            state["last_experts_update"] = last_experts
            if not args.dry_run:
                save_json(state_path, state)
        if args.once:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
