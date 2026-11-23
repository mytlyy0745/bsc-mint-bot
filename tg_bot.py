#!/usr/bin/env python3
"""
tg_bot.py - BSC 打新 Agent 的 Telegram 机器人

用法：
    python3 tg_bot.py [--dry-run] [--config config.json]

依赖：pip install -r requirements.txt（只需要 requests）

配置：全部在 config.json；bot token 从 telegram-token.txt 读（绝不写进代码和日志）。
推送目标 chat_id 在 config.json 的 telegram.chat_id。

命令：
    /start    - 问候 + 当前模式 + 钱包状态
    /status   - 钱包连接、余额、今日 gas、模式
    /radar    - 雷达列表（radar.json，前 5 个）
    /holdings - 我的持仓（holdings.json）
    /mode     - 切换模式：关 / 只模拟 / 自动（切到自动要再确认一次）

回调按钮（参与 / 不参与 / 挂单 / 卖出）：
    只把决定写进 decisions.json，不直接发交易。
    真正发交易由 auto_mint.py / auto_sell.py 在 Li 确认后做。

推送卡片格式（用户定）：每张卡不超过 10 行，一行一件事，emoji 行首，
不写函数名、不贴长地址、不写开场白。

安全红线（违反就拒绝并记录）：
    - 要 approve / permit / setApprovalForAll / 签消息登录才能铸或领的：不打、不领。
    - 陌生空投币 / NFT：标「诱饵」，不碰。
    - 池子流动性 < 50U 的币：报价当没有，持仓单独标，不给卖出按钮。
    - 同一部署地址 24 小时内发 >= 3 个合约的批量系列：不打。
    - 只执行 Li 在对话里亲口说的买卖和脚本规则内的动作。
    - 默认只模拟（mode=simulate）。真钱第一笔由 Li 自己点。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------- 安全红线（常量，违反就拒绝并记录） ----------------
SAFETY = {
    "forbidden_calls": ["approve", "permit", "setApprovalForAll"],
    "forbidden_login": True,  # 签消息登录才能铸/领的：不打、不领
    "min_pool_liquidity_usd": 50.0,  # 池子 < 50U 的币：报价当没有
    "max_contracts_per_deployer_24h": 3,  # 同一部署地址 24h 内 >= 3 个合约：不打
    "bait_label": "诱饵",  # 陌生空投币 / NFT 的标记
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DECISIONS_FILE = os.path.join(BASE_DIR, "decisions.json")
RADAR_FILE = os.path.join(BASE_DIR, "radar.json")
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
TOKEN_FILE = os.path.join(BASE_DIR, "telegram-token.txt")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30  # getUpdates 长轮询秒数


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_token():
    """从 telegram-token.txt 读 token（绝不在日志/输出里打印它）。"""
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise RuntimeError("telegram-token.txt 为空，请先把 bot token 放进去")
    return token


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


# ---------------- 推送卡片 ----------------

def short_addr(addr):
    """地址只显示头尾，不贴长地址。"""
    if not addr or len(addr) < 10:
        return addr or ""
    return addr[:6] + "…" + addr[-4:]


def format_project_card(project):
    """一张项目卡，不超过 10 行，emoji 行首，不写函数名、不贴长地址。"""
    name = project.get("name") or short_addr(project.get("address", ""))
    free = project.get("is_free", True)
    cost = "免费" if free else (project.get("cost") or "? BNB")
    verdict = project.get("verdict", "观望")
    progress = project.get("progress", "?")
    total = project.get("total", "?")
    agent_1h = project.get("agent_wallets_1h", 0)
    agent_5m = project.get("agent_wallets_5min", 0)
    minted = project.get("my_minted", "未铸")
    trades = project.get("trades_24h", 0)
    median = project.get("median_usd", 0)
    safety = project.get("safety", "待核查")
    reason = project.get("reason", "")
    lines = [
        f"🆕 {name}｜{cost}｜结论：{verdict}",
        f"📊 进度 {progress}/{total}，1 小时 {agent_1h} 个 Agent 钱包在铸",
        f"🔥 5 分钟 {agent_5m} 个，在升温" if agent_5m >= 100 else f"🔥 5 分钟 {agent_5m} 个",
        f"👛 我的号：{minted}",
        f"💰 成交：近 24 小时 {trades} 笔，中位数 {median}U",
        f"🛡 安全：{safety}",
    ]
    if reason:
        lines.append(f"💡 {reason}")
    return "\n".join(lines[:10])


def format_sell_card(trade):
    """卖出提醒：哪种卡、什么品质、每张多少 U、到手多少、为什么卖。"""
    lines = [
        f"💸 卖出：{trade.get('project', '')}｜{trade.get('quality', '')}",
        f"📦 数量：{trade.get('amount', 0)} 张，每张 {trade.get('price_usd', 0)}U",
        f"💰 到手：约 {trade.get('net_usd', 0)}U（扣手续费）",
        f"📍 原因：{trade.get('reason', '')}",
    ]
    return "\n".join(lines[:10])


# ---------------- Telegram API ----------------

class TgBot:
    def __init__(self, token, chat_id, dry_run=False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.offset = 0

    def api(self, method, payload=None, timeout=35):
        url = TELEGRAM_API.format(token=self.token, method=method)
        if self.dry_run and method in ("sendMessage", "editMessageText",
                                        "answerCallbackQuery"):
            print(f"[DRY-RUN] {method}: {json.dumps(payload, ensure_ascii=False)[:300]}")
            return {"ok": True, "result": {}}
        r = requests.post(url, json=payload or {}, timeout=timeout)
        return r.json()

    def send(self, text, reply_markup=None):
        payload = {"chat_id": self.chat_id, "text": text,
                   "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.api("sendMessage", payload)

    def send_card(self, text, buttons=None):
        """发一张卡片（带按钮）。按钮只记录决定，不直接发交易。"""
        markup = None
        if buttons:
            markup = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]}
        return self.send(text, reply_markup=markup)

    def get_updates(self):
        data = self.api("getUpdates", {"offset": self.offset,
                                       "timeout": POLL_TIMEOUT},
                        timeout=POLL_TIMEOUT + 10)
        if not data.get("ok"):
            return []
        updates = data.get("result", [])
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates


# ---------------- 决定记录（只记录，不发交易） ----------------

def record_decision(decision):
    """把一次按钮决定追加到 decisions.json。"""
    decisions = load_json(DECISIONS_FILE, [])
    decision["time"] = datetime.now(timezone.utc).isoformat()
    decisions.append(decision)
    save_json(DECISIONS_FILE, decisions)
    return len(decisions)


# ---------------- 命令处理 ----------------

def handle_command(bot, cfg, text, chat_id):
    cmd = text.strip().split()[0].lower()
    mode = cfg.get("mode", "simulate")
    wallet = cfg.get("wallet", {})
    bsc = wallet.get("bsc_address", "")

    if cmd == "/start":
        bot.send(
            "👋 打新 Agent 已就绪\n"
            f"📍 模式：{mode}（默认只模拟）\n"
            f"👛 钱包：{short_addr(bsc)}\n"
            "命令：/status /radar /holdings /mode"
        )
    elif cmd == "/status":
        holdings = load_json(HOLDINGS_FILE, [])
        records = load_json(os.path.join(BASE_DIR, "mint_records.json"), [])
        gas_today = sum(
            r.get("gas_bnb", 0) for r in records
            if str(r.get("date", "")) == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        bot.send(
            "📊 状态\n"
            f"模式：{mode}\n"
            f"持仓项目：{len(holdings)} 个\n"
            f"今日 gas：{gas_today:.5f} BNB（上限 0.003）\n"
            f"铸造记录：{len(records)} 笔"
        )
    elif cmd == "/radar":
        radar = load_json(RADAR_FILE, [])
        if not radar:
            bot.send("📡 雷达暂无新项目")
            return
        for project in radar[:5]:
            buttons = [[
                ("参与", f"join:{project.get('address', '')}"),
                ("不参与", f"skip:{project.get('address', '')}"),
            ], [
                ("挂单", f"list:{project.get('address', '')}"),
                ("问 AI 建议", f"ask:{project.get('address', '')}"),
            ]]
            bot.send_card(format_project_card(project), buttons=buttons)
    elif cmd == "/holdings":
        holdings = load_json(HOLDINGS_FILE, [])
        if not holdings:
            bot.send("👛 暂无持仓")
            return
        lines = ["👛 我的持仓"]
        for h in holdings[:10]:
            bait = " [诱饵]" if h.get("is_bait") else ""
            low = " [池子<50U]" if h.get("low_liquidity") else ""
            lines.append(
                f"· {h.get('project', '')} x{h.get('amount', 0)}"
                f"｜{h.get('quality', '')}｜现价 {h.get('price_usd', 0)}U{bait}{low}"
            )
        bot.send("\n".join(lines[:10]))
    elif cmd == "/mode":
        buttons = [[
            ("关", "mode:off"),
            ("只模拟", "mode:simulate"),
            ("自动", "mode:auto"),
        ]]
        bot.send_card(f"当前模式：{mode}\n切到「自动」要再确认一次。", buttons=buttons)
    else:
        bot.send("未知命令。可用：/start /status /radar /holdings /mode")


def handle_callback(bot, cfg, callback):
    """处理按钮回调：只记录决定，不发交易。"""
    data = callback.get("data", "")
    action, _, target = data.partition(":")
    user = callback.get("from", {}).get("username", "")
    record_decision({
        "action": action,
        "target": target,
        "user": user,
        "source": "tg_button",
    })
    # 回执只确认收到，不执行交易
    bot.api("answerCallbackQuery", {
        "callback_query_id": callback["id"],
        "text": "已记录，等确认后执行" if action in ("join", "list", "sell") else "已记录",
    })
    if action == "mode":
        # 切到自动要再确认一次：先记下来，等 Li 在对话里亲口确认
        bot.send(f"模式切换到「{target}」已记录。切到自动需你在对话里再确认一次。")
    elif action == "ask":
        bot.send("已记录你的问题，AI 稍后在对话里回复建议。")


def main():
    parser = argparse.ArgumentParser(description="BSC 打新 Agent TG 机器人")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印要发的内容，不真的发 TG 消息")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="config.json 路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    token = load_token()
    chat_id = cfg.get("telegram", {}).get("chat_id")
    if not chat_id:
        print("config.json 里没有 telegram.chat_id", file=sys.stderr)
        sys.exit(1)

    bot = TgBot(token, chat_id, dry_run=args.dry_run)
    print(f"TG bot 启动（dry-run={args.dry_run}），监听命令…")
    while True:
        try:
            updates = bot.get_updates()
        except Exception as e:  # 网络抖动就等一下再轮询
            print(f"轮询出错：{e}，5 秒后重试")
            time.sleep(5)
            continue
        for upd in updates:
            try:
                if "message" in upd:
                    msg = upd["message"]
                    text = msg.get("text", "")
                    if text.startswith("/"):
                        handle_command(bot, cfg, text, msg["chat"]["id"])
                elif "callback_query" in upd:
                    handle_callback(bot, cfg, upd["callback_query"])
            except Exception as e:
                print(f"处理更新出错：{e}")


if __name__ == "__main__":
    main()
