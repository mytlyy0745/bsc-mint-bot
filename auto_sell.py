#!/usr/bin/env python3
"""
auto_sell.py - 只管免费打到的持仓，自动卖

用法：
    python3 auto_sell.py [--dry-run] [--live] [--config config.json] [--once]
    默认只模拟（--dry-run）。真发挂单必须同时满足：
      1) 传 --live；
      2) config.json 的 mode 不是 "off"；
      3) Li 在对话里亲口确认过。

依赖：pip install -r requirements.txt（web3、requests）

配置：全部在 config.json。

规则（只管免费打到的；付费买的不自动卖）：
    - 盯每个持仓项目最近 5 笔成交的中位数。第一次到 0.5U 时：
        · 没有后续信号 → 全部按中位数 0.95 倍挂出，快卖。
        · 有后续信号（合约有分红/质押/领币函数、官推在发活动、有人在扫货）
          → 挂一半（中位数 1.1 倍），另一半留着，记进跟进台账。
    - 留着的：
        · 从最高点回落 40% 或成交明显变冷 → 剩下的按快卖价挂出；
        · 挂 6 小时没卖掉 → 降到中位数；
        · 中位数跌到挂单价一半以下 → 按 24 小时最低价清掉；
        · 留满 7 天没新活动且不到 0.3U → 也清掉。
    - 价格线：哪种卡最新一笔成交 >= 10U，把我这种卡还没挂的按这个价挂出，
      挂过的不重挂。
    - 同一项目按品质分组，各算各的价，普通卡不拿传说卡的成交价挂。
    - 项目自家的市场和 Element 都要看，按两边更高的价挂。
    - 每隔一段时间核对挂单，市场上没显示的按原价重挂，挂不上两小时后再试。
    - 挂单价偏离最近成交 30% 以上、或比原价降一半以上，先问 Li。
    - 1~2 个买家刷出 >= 5 笔成交的当刷量，不跟价。
    - 项目发币开盘：每号这个币的持仓值到 30U 就全卖（这个数可在 config 改）。
    - 诱饵币、池子 < 50U 的币不自动卖。

输出：listings.json（挂单）、trades.json（成交）。

安全红线（违反就拒绝并记录）：
    - 陌生空投币 / NFT：标「诱饵」，不碰、不卖。
    - 池子流动性 < 50U 的币：报价当没有，持仓单独标，不给卖出按钮。
    - 挂单价偏离最近成交 30% 以上、或比原价降一半以上，先问 Li。
    - 只执行 Li 在对话里亲口说的买卖和脚本规则内的动作。
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

# ---------------- 安全红线（常量，违反就拒绝并记录） ----------------
SAFETY = {
    "min_pool_liquidity_usd": 50.0,  # 池子 < 50U：报价当没有，不自动卖
    "bait_label": "诱饵",
    "list_price_deviation_pct": 30,  # 挂单价偏离最近成交 30% 以上：先问 Li
    "list_price_drop_pct": 50,  # 比原价降一半以上：先问 Li
    "wash_trade_min_buyers": 2,  # 1~2 个买家刷出 >= 5 笔成交：当刷量
    "wash_trade_min_trades": 5,
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")


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


def median_last_n(prices, n=5):
    """最近 n 笔成交的中位数。"""
    if not prices:
        return 0.0
    return statistics.median(prices[-n:])


def is_wash_trading(trades):
    """1~2 个买家刷出 >= 5 笔成交：当刷量，不跟价。"""
    if len(trades) < SAFETY["wash_trade_min_trades"]:
        return False
    buyers = {t.get("buyer", "") for t in trades[-SAFETY["wash_trade_min_trades"]:]}
    return len(buyers) <= SAFETY["wash_trade_min_buyers"]


def has_follow_up_signal(project, trades):
    """后续信号：分红/质押/领币函数、官推活动、扫货。

    本脚本只做链上可查的部分（合约有没有分红/质押/领币函数名、
    最近有没有大额扫货）；官推活动由 AI 人工判断后写进项目记录。
    """
    if project.get("official_activity"):
        return True
    funcs = project.get("contract_functions", [])
    for keyword in ("dividend", "stake", "claim", "reward", "分红", "质押", "领"):
        if any(keyword.lower() in f.lower() for f in funcs):
            return True
    # 最近 5 笔里有单笔 >= 10U 的扫货
    if any(t.get("price_usd", 0) >= 10 for t in trades[-5:]):
        return True
    return False


def decide_sell(cfg, holding, trades):
    """对一个持仓做卖出决策，返回 (action, detail)。

    action: hold / list_all / list_half / clear / ask_li
    """
    sell_cfg = cfg.get("sell", {})
    project = holding.get("project", "")
    quality = holding.get("quality", "普通")
    amount = holding.get("amount", 0)
    if amount <= 0:
        return "hold", "数量为 0"
    if holding.get("is_bait"):
        return "hold", "诱饵币，不自动卖"
    if holding.get("low_liquidity") or holding.get("pool_usd", 999) < \
            SAFETY["min_pool_liquidity_usd"]:
        return "hold", "池子 < 50U，不自动卖"
    # 项目发币开盘：持仓值到 30U 就全卖
    prices = [t.get("price_usd", 0) for t in trades]
    if not prices:
        return "hold", "暂无成交"
    if is_wash_trading(trades):
        return "hold", "疑似刷量，不跟价"
    median = median_last_n(prices, 5)
    peak = max(prices)
    holding_value = amount * median
    # 发币开盘清仓线
    if holding_value >= sell_cfg.get("token_launch_sell_at_usd", 30):
        return "clear", f"持仓值 {holding_value:.1f}U 到 30U，全卖"
    # 第一次到 0.5U
    first_target = sell_cfg.get("first_target_usd", 0.5)
    if median >= first_target and not holding.get("listed_once"):
        if has_follow_up_signal(holding, trades):
            half = amount // 2 or 1
            price = round(median * sell_cfg.get("half_sell_factor", 1.1), 4)
            return "list_half", {"price": price, "amount": half,
                                 "reason": "到 0.5U，有后续信号，挂一半 1.1 倍"}
        price = round(median * sell_cfg.get("quick_sell_factor", 0.95), 4)
        return "list_all", {"price": price, "amount": amount,
                            "reason": "到 0.5U，无后续信号，全部快卖 0.95 倍"}
    # 留着的：从最高点回落 40% 或成交变冷
    if holding.get("listed_once") and holding.get("remaining", 0) > 0:
        if peak > 0 and (peak - median) / peak >= \
                sell_cfg.get("peak_drawdown_pct", 40) / 100:
            price = round(median * sell_cfg.get("quick_sell_factor", 0.95), 4)
            return "list_all", {"price": price, "amount": holding["remaining"],
                                "reason": "从最高点回落 40%，剩下的快卖"}
        # 挂 6 小时没卖掉 → 降到中位数
        listed_at = holding.get("listed_at")
        if listed_at:
            try:
                listed_dt = datetime.fromisoformat(listed_at)
                if datetime.now(timezone.utc) - listed_dt > timedelta(hours=6):
                    return "list_all", {"price": round(median, 4),
                                        "amount": holding["remaining"],
                                        "reason": "挂 6 小时没卖掉，降到中位数"}
            except ValueError:
                pass
        # 中位数跌到挂单价一半以下 → 按 24 小时最低价清掉
        list_price = holding.get("list_price", 0)
        if list_price and median < list_price / 2:
            low_24h = min(prices[-20:]) if prices else median
            return "clear", {"price": round(low_24h, 4),
                             "amount": holding["remaining"],
                             "reason": "中位数跌到挂单价一半以下，按 24h 最低价清掉"}
        # 留满 7 天没新活动且不到 0.3U → 清掉
        held_since = holding.get("minted_at")
        if held_since:
            try:
                held_dt = datetime.fromisoformat(held_since)
                days = (datetime.now(timezone.utc) - held_dt).days
                if days >= sell_cfg.get("hold_days_no_activity", 7) \
                        and median < sell_cfg.get("hold_clear_below_usd", 0.3) \
                        and not holding.get("official_activity"):
                    return "clear", {"price": round(median, 4),
                                     "amount": holding["remaining"],
                                     "reason": "留满 7 天没新活动且 < 0.3U，清掉"}
            except ValueError:
                pass
    # 价格线：最新一笔 >= 10U，把还没挂的按这个价挂出
    latest = prices[-1] if prices else 0
    if latest >= 10 and not holding.get("listed_once"):
        return "list_all", {"price": round(latest, 4), "amount": amount,
                            "reason": "最新一笔 >= 10U，按这个价挂出"}
    return "hold", "未到卖出条件"


def main():
    parser = argparse.ArgumentParser(description="免费打到的持仓自动卖")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="只模拟，不真挂单（默认）")
    parser.add_argument("--live", action="store_true",
                        help="真挂单（还要 config mode != off 且 Li 确认过）")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args()
    live = args.live

    cfg = load_config(args.config)
    mode = cfg.get("mode", "simulate")
    if live and mode == "off":
        print("mode=off，拒绝真挂单，只模拟")
        live = False
    if live:
        print("⚠️ LIVE 模式：将真实挂单。请确认 Li 已亲口同意。")

    holdings_path = os.path.join(BASE_DIR, cfg["files"]["holdings"])
    listings_path = os.path.join(BASE_DIR, cfg["files"]["listings"])
    trades_path = os.path.join(BASE_DIR, cfg["files"]["trades"])

    while True:
        holdings = load_json(holdings_path, [])
        listings = load_json(listings_path, [])
        all_trades = load_json(trades_path, {})
        changed = False
        for holding in holdings:
            project = holding.get("project", "")
            quality = holding.get("quality", "普通")
            # 同一项目按品质分组：只看同品质的成交
            trades = [t for t in all_trades.get(project, [])
                      if t.get("quality", "普通") == quality]
            action, detail = decide_sell(cfg, holding, trades)
            if action == "hold":
                continue
            # 挂单价偏离最近成交 30% 以上、或比原价降一半以上：先问 Li
            if isinstance(detail, dict) and "price" in detail:
                prices = [t.get("price_usd", 0) for t in trades]
                last = prices[-1] if prices else 0
                if last and abs(detail["price"] - last) / last > \
                        SAFETY["list_price_deviation_pct"] / 100:
                    print(f"{project} 挂单价偏离最近成交 30% 以上，先问 Li，不挂")
                    continue
                orig = holding.get("list_price") or holding.get("mint_price_usd") or 0
                if orig and detail["price"] < orig * (1 - SAFETY["list_price_drop_pct"] / 100):
                    print(f"{project} 挂单价比原价降一半以上，先问 Li，不挂")
                    continue
            if action in ("list_all", "list_half", "clear"):
                listing = {
                    "project": project,
                    "quality": quality,
                    "address": holding.get("address", ""),
                    "amount": detail.get("amount", 0) if isinstance(detail, dict) else 0,
                    "price_usd": detail.get("price", 0) if isinstance(detail, dict) else 0,
                    "reason": detail.get("reason", "") if isinstance(detail, dict) else detail,
                    "action": action,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "live": live,
                }
                if not live:
                    listing["status"] = "simulated"
                    print(f"[模拟] {project} {quality}：{action} "
                          f"{listing['amount']} 张 @ {listing['price_usd']}U"
                          f"（{listing['reason']}）")
                else:
                    listing["status"] = "awaiting_baw"
                    print(f"✅ {project} {quality}：{action} 已记 awaiting_baw（等真挂确认）")
                listings.append(listing)
                holding["listed_once"] = True
                holding["list_price"] = listing["price_usd"]
                holding["listed_at"] = listing["time"]
                if action == "list_half":
                    holding["remaining"] = holding.get("amount", 0) - listing["amount"]
                elif action in ("list_all", "clear"):
                    holding["remaining"] = 0
                changed = True
        if changed:
            save_json(holdings_path, holdings)
            save_json(listings_path, listings)
        if args.once:
            break
        time.sleep(120)


if __name__ == "__main__":
    main()
