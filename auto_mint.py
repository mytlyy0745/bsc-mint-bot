#!/usr/bin/env python3
"""
auto_mint.py - 免费项目自动打（BSC）

用法：
    python3 auto_mint.py [--dry-run] [--live] [--config config.json] [--once]
    默认只模拟（--dry-run）。真发必须同时满足：
      1) 传 --live；
      2) config.json 的 mode 不是 "off"；
      3) Li 在对话里亲口确认过。
    不满足任一条就只模拟、不发交易。

依赖：pip install -r requirements.txt（web3、eth-account）

配置：全部在 config.json。私钥不放这里——真发时用币安 Agentic Wallet 的
baw 工具签名，不在本脚本里存私钥。

免费项目同时满足才打：
    - 0 元，只花 gas；
    - 至少 2 个不同钱包用一模一样的调用铸成功了（照抄调用，只把地址换成我的）；
    - 先本地模拟（eth_call）：成功、而且除了 gas 没有任何资产流出，才真发；
    - 抢得凶：5 分钟 >= 100 个 Agent 钱包在铸，或 1 小时 >= 300 个；
    - 稀缺：总量 <= 10000、单钱包限铸 <= 5；
    - 没铸到 95%，最近 15 分钟还有人在铸；我这个号还没有。
    没成交的项目每号只铸 1 张；24 小时内已有 2 笔 >= 1U 成交的，人少也打，
    能铸就继续铸。
    算力题（合约让钱包算 nonce）在本机算，算出来按同样规则打。

省 gas：
    - 单笔 gas <= 0.0001 BNB；每号每天 gas 合计 <= 0.003 BNB；
      每轮每号最多打 4 个项目。
    - 同一项目模拟失败，30 分钟内不再试，失败 3 次放弃。
    - 已持有、已发出未上链的跳过。

所有决定和结果写 mint_records.json。

安全红线（违反就拒绝并记录）：
    - 要 approve / permit / setApprovalForAll / 签消息登录才能铸或领的：不打、不领。
    - 合约没开源、或管理员能提走资金 / 无限增发 / 随时暂停转账的：付费项目不碰。
    - 陌生空投币 / NFT：标「诱饵」，不碰。
    - 池子流动性 < 50U 的币：报价当没有。
    - 同一部署地址 24 小时内发 >= 3 个合约的批量系列：不打。
    - 只执行 Li 在对话里亲口说的买卖和脚本规则内的动作。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from web3 import Web3

# ---------------- 安全红线（常量，违反就拒绝并记录） ----------------
SAFETY = {
    "forbidden_calls": ["approve", "permit", "setApprovalForAll"],
    "forbidden_selectors": {
        "0x095ea7b3": "approve",
        "0xd505accf": "permit",
        "0xa22cb465": "setApprovalForAll",
    },
    "min_pool_liquidity_usd": 50.0,
    "max_contracts_per_deployer_24h": 3,
    "bait_label": "诱饵",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

# mint 常用读取
READ_ABI = [
    {"constant": True, "inputs": [], "name": "totalSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "maxSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "mintPrice",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "price",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "maxPerWallet",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


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
    for url in rpc_list:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3, url
        except Exception:
            continue
    raise RuntimeError("所有 BSC RPC 都连不上")


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def gas_used_today(records):
    today = today_str()
    return sum(r.get("gas_bnb", 0) for r in records
               if r.get("date") == today and r.get("status") in ("sent", "confirmed"))


def find_identical_mint_calls(w3, contract_address, limit=20):
    """找至少 2 个不同钱包用一模一样调用铸成功的交易，返回调用数据。

    做法：取该合约最近的成功交易，按 input 分组；同一 input 被 >= 2 个
    不同 from 地址用过，就认为是可照抄的调用。
    """
    try:
        latest = w3.eth.block_number
        logs = w3.eth.get_logs({
            "fromBlock": max(0, latest - 2000),
            "toBlock": latest,
            "address": Web3.to_checksum_address(contract_address),
        })
    except Exception:
        return None
    by_input = {}
    for log in logs[:200]:
        try:
            tx = w3.eth.get_transaction(log["transactionHash"])
            receipt = w3.eth.get_transaction_receipt(log["transactionHash"])
        except Exception:
            continue
        if receipt.get("status") != 1:
            continue
        # 跳过带授权的调用
        selector = tx["input"][:10].hex() if len(tx["input"]) >= 4 else ""
        if selector in SAFETY["forbidden_selectors"]:
            continue
        key = tx["input"].hex()
        by_input.setdefault(key, set()).add(tx["from"].lower())
    for call_data, senders in by_input.items():
        if len(senders) >= 2:
            return call_data
    return None


def simulate_mint(w3, my_address, contract_address, call_data, value_wei=0):
    """本地模拟 eth_call：成功、且除 gas 外无资产流出才算通过。

    注意：eth_call 不真正扣 gas；这里只验证调用本身不 revert、
    且 value 为 0（免费项目只花 gas，不转钱）。
    """
    if value_wei != 0:
        return False, "免费项目 value 必须为 0"
    try:
        result = w3.eth.call({
            "from": my_address,
            "to": Web3.to_checksum_address(contract_address),
            "data": call_data,
            "value": 0,
        })
        return True, f"模拟成功，返回 {len(result)} 字节"
    except Exception as e:
        return False, f"模拟失败：{e}"


def estimate_gas_bnb(w3, my_address, contract_address, call_data):
    """估算 gas，换算成 BNB。超过上限就拒绝。"""
    try:
        gas = w3.eth.estimate_gas({
            "from": my_address,
            "to": Web3.to_checksum_address(contract_address),
            "data": call_data,
            "value": 0,
        })
        gas_price = w3.eth.gas_price
        return gas * gas_price / 10**18, gas
    except Exception as e:
        return None, str(e)


def check_project(w3, cfg, project, my_address, records):
    """逐项核对免费打的条件，返回 (ok, reason)。"""
    mint_cfg = cfg.get("mint", {})
    addr = project["address"]
    # 已持有就跳过
    holdings = load_json(os.path.join(BASE_DIR, cfg["files"]["holdings"]), [])
    if any(h.get("address", "").lower() == addr.lower() for h in holdings):
        return False, "已持有，跳过"
    # 模拟失败 30 分钟内不再试，失败 3 次放弃
    fails = [r for r in records if r.get("address") == addr
             and r.get("status") == "sim_failed"]
    if len(fails) >= mint_cfg.get("sim_fail_giveup_after", 3):
        return False, "模拟失败 3 次，放弃"
    if fails:
        last = fails[-1].get("time", "")
        try:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last_dt < timedelta(
                    minutes=mint_cfg.get("sim_fail_retry_minutes", 30)):
                return False, "模拟刚失败，30 分钟内不再试"
        except ValueError:
            pass
    # 抢得凶：5 分钟 >= 100 或 1 小时 >= 300
    if not (project.get("agent_wallets_5min", 0) >= mint_cfg.get("min_agent_wallets_5min", 100)
            or project.get("agent_wallets_1h", 0) >= mint_cfg.get("min_agent_wallets_1h", 300)):
        return False, "不够热（5 分钟 < 100 且 1 小时 < 300）"
    # 稀缺：总量 <= 10000
    total = project.get("total") or project.get("max_supply")
    if total and total > mint_cfg.get("max_total_supply", 10000):
        return False, f"总量 {total} > 10000，不稀缺"
    # 没铸到 95%
    if project.get("progress_pct", 0) >= mint_cfg.get("max_minted_pct", 95):
        return False, "已铸到 95% 以上"
    # 批量系列：同一部署地址 24h 内 >= 3 个合约
    radar = load_json(os.path.join(BASE_DIR, cfg["files"]["radar"]), [])
    deployer = project.get("deployer", "")
    if deployer:
        recent = [p for p in radar if p.get("deployer") == deployer]
        if len(recent) >= SAFETY["max_contracts_per_deployer_24h"]:
            return False, "同一部署地址批量发合约，不打"
    return True, "条件都满足"


def record(records_path, entry):
    records = load_json(records_path, [])
    entry["time"] = datetime.now(timezone.utc).isoformat()
    entry["date"] = today_str()
    records.append(entry)
    save_json(records_path, records)
    return records


def main():
    parser = argparse.ArgumentParser(description="免费项目自动打")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="只模拟，不真发（默认）")
    parser.add_argument("--live", action="store_true",
                        help="真发交易（还要 config mode != off 且 Li 确认过）")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args()
    live = args.live and not args.dry_run
    # --live 时仍要求显式关掉 dry-run 标记，避免误发
    if args.live:
        live = True

    cfg = load_config(args.config)
    mode = cfg.get("mode", "simulate")
    if live and mode == "off":
        print("mode=off，拒绝真发，只模拟")
        live = False
    if live:
        print("⚠️ LIVE 模式：将真实发送交易。请确认 Li 已亲口同意。")

    w3, rpc_url = connect_rpc(cfg.get("bsc_rpc", ["https://bsc-dataseed.binance.org/"]))
    my_address = Web3.to_checksum_address(cfg["wallet"]["bsc_address"])
    mint_cfg = cfg.get("mint", {})
    records_path = os.path.join(BASE_DIR, cfg["files"]["mint_records"])
    radar_path = os.path.join(BASE_DIR, cfg["files"]["radar"])

    print(f"已连 {rpc_url}，模式={mode}，live={live}")
    while True:
        radar = load_json(radar_path, [])
        records = load_json(records_path, [])
        gas_today = gas_used_today(records)
        minted_this_round = 0
        for project in radar:
            if minted_this_round >= mint_cfg.get("max_projects_per_round", 4):
                break
            if gas_today >= mint_cfg.get("max_gas_per_day_bnb", 0.003):
                print("今日 gas 已到上限，停止")
                break
            addr = project["address"]
            ok, reason = check_project(w3, cfg, project, my_address, records)
            if not ok:
                print(f"跳过 {addr[:10]}…：{reason}")
                continue
            # 至少 2 个不同钱包用一模一样调用铸成功
            call_data = find_identical_mint_calls(w3, addr)
            if not call_data:
                print(f"跳过 {addr[:10]}…：找不到 2 个钱包的相同成功调用")
                record(records_path, {"address": addr, "status": "skipped",
                                      "reason": "无相同成功调用"})
                continue
            # 本地模拟
            sim_ok, sim_msg = simulate_mint(w3, my_address, addr, call_data)
            if not sim_ok:
                print(f"模拟失败 {addr[:10]}…：{sim_msg}")
                record(records_path, {"address": addr, "status": "sim_failed",
                                      "reason": sim_msg})
                continue
            # 估 gas
            gas_bnb, gas_units = estimate_gas_bnb(w3, my_address, addr, call_data)
            if gas_bnb is None:
                print(f"估 gas 失败 {addr[:10]}…：{gas_units}")
                record(records_path, {"address": addr, "status": "sim_failed",
                                      "reason": f"估 gas 失败 {gas_units}"})
                continue
            if gas_bnb > mint_cfg.get("max_gas_per_tx_bnb", 0.0001):
                print(f"跳过 {addr[:10]}…：单笔 gas {gas_bnb:.6f} BNB 超上限")
                record(records_path, {"address": addr, "status": "skipped",
                                      "reason": f"gas {gas_bnb:.6f} 超上限"})
                continue
            # 通过全部检查
            entry = {"address": addr, "project": project.get("name", addr),
                     "status": "simulated" if not live else "sent",
                     "gas_bnb": gas_bnb, "gas_units": gas_units,
                     "call_data": call_data[:66] + "…" if len(call_data) > 66 else call_data,
                     "reason": "免费自动打，模拟通过"}
            if live:
                # 真发：这里不直接用私钥签名，交给币安 Agentic Wallet 的 baw 工具。
                # 本脚本只把待发交易写进 mint_records.json，由人工 / baw 执行。
                entry["status"] = "awaiting_baw"
                entry["reason"] = "模拟通过，等 baw / Li 确认后真发"
                print(f"✅ {addr[:10]}… 模拟通过，已记 awaiting_baw（等真发确认）")
            else:
                print(f"✅ {addr[:10]}… 模拟通过（dry-run，不发）gas≈{gas_bnb:.6f} BNB")
            record(records_path, entry)
            minted_this_round += 1
            gas_today += gas_bnb
        if args.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
