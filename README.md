# BSC 打新 Agent — 执行层脚本

本机/服务器跑的程序：TG 机器人 + BSC 扫链 + 免费自动打 + 自动卖。
面板（artifact `bsc-da-xin-mian-ban`）只记决定、不发交易；这里是真正的执行层。

## 文件

| 文件 | 作用 |
| --- | --- |
| `config.json` | 共享配置：钱包地址、Agent ID 注册合约、RPC、gas 上限、卖出规则、模式 |
| `requirements.txt` | 依赖清单 |
| `tg_bot.py` | Telegram 机器人（纯 requests 轮询） |
| `scanner.py` | BSC 扫链，输出 `radar.json`、`experts.json` |
| `auto_mint.py` | 免费项目自动打，写 `mint_records.json` |
| `auto_sell.py` | 免费打到的持仓自动卖，写 `listings.json`、`trades.json` |
| `telegram-token.txt` | TG bot token（chmod 600，绝不进 git、不贴聊天） |
| `telegram-config.txt` | bot 用户名、Li 的 chat ID（token 不在里面） |

运行时生成的状态文件（都在本目录）：
`radar.json`、`experts.json`、`mint_records.json`、`holdings.json`、
`listings.json`、`trades.json`、`decisions.json`、`scanner_state.json`。

## 模式

`config.json` 里的 `mode` 有三种：`off`（关）、`simulate`（只模拟）、`auto`（自动）。
默认 `simulate`。真钱的第一笔由 Li 自己点；切到 `auto` 要 Li 在对话里亲口确认。

所有脚本默认只模拟（`--dry-run`）。真发必须显式传 `--live`，
且 `mode` 不是 `off`，且 Li 已确认。即使 `--live`，交易本身也不在本脚本里
用私钥签名——真发走币安 Agentic Wallet 的 `baw` 工具，由人工 / baw 执行，
脚本只把待发交易记成 `awaiting_baw`。

## 安全红线（写进每个脚本的常量）

- 要 approve / permit / setApprovalForAll / 签消息登录才能铸或领的：不打、不领。
- 合约没开源、或管理员能提走资金 / 无限增发 / 随时暂停转账的：付费项目不碰。
- 陌生空投币 / NFT：标「诱饵」，不碰、不卖。
- 池子流动性 < 50U 的币：报价当没有，持仓单独标，不给卖出按钮。
- 同一部署地址 24 小时内发 >= 3 个合约的批量系列：不打。
- 只执行 Li 在对话里亲口说的买卖和脚本规则内的动作。

## 在 Nodaroo Python 服务器上部署

Nodaroo 面板是 Pterodactyl 风格的游戏/应用托管，没有 SSH，
所有操作都在面板里做（Li 在手机上按步骤点）。

### 1. 确认服务器装好了

面板里打开 `Pyhon` 这个服务器（python generic，Sydney AU1），
状态要是 Running / 安装完成。如果还在 Installing，等它装完再继续。

### 2. 装依赖

在面板里打开服务器的 Console / 终端，依次执行：

```bash
pip install -r requirements.txt
```

如果 `pip` 不存在，先装：`sudo apt update && sudo apt install -y python3-pip`。

### 3. 放代码和 token

把本目录的全部文件上传到服务器的工作目录（面板的 File Manager 上传，
或用 `git clone` 后复制）。确认以下文件都在：

- `config.json`、`requirements.txt`
- `tg_bot.py`、`scanner.py`、`auto_mint.py`、`auto_sell.py`
- `telegram-token.txt`（chmod 600，token 已验证通过，不要再改）

注意：`telegram-token.txt` 不要提交到任何公开仓库。

### 4. 设启动命令

Nodaroo 的 python generic 服务器有一个 Startup Command（启动命令）。
一次只能跑一个主进程，建议这样分：

- 主进程跑 TG 机器人（常驻、收命令、发推送）：
  `python3 tg_bot.py`
- 扫链、自动打、自动卖用服务器的定时任务（或另开后台进程）跑：
  `python3 scanner.py`、`python3 auto_mint.py`、`python3 auto_sell.py`

如果面板只允许一个启动命令，就把启动命令设成 `python3 tg_bot.py`，
另外三个用 `nohup python3 scanner.py &` 这种方式在 Console 里手动启动，
或写一个 `start_all.sh` 一次拉起四个后台进程：

```bash
#!/bin/bash
cd "$(dirname "$0")"
nohup python3 tg_bot.py > tg_bot.log 2>&1 &
nohup python3 scanner.py > scanner.log 2>&1 &
nohup python3 auto_mint.py > auto_mint.log 2>&1 &
nohup python3 auto_sell.py > auto_sell.log 2>&1 &
wait
```

### 5. 怎么看日志

- 面板 Console 里直接看实时输出。
- 各脚本的日志文件：`tg_bot.log`、`scanner.log`、`auto_mint.log`、`auto_sell.log`。
- 决策和记录看 JSON 文件：`radar.json`（雷达）、`mint_records.json`（铸造记录）、
  `listings.json`（挂单）、`trades.json`（成交）、`decisions.json`（按钮决定）。

### 6. 先跑模拟，再开自动

1. 四个脚本先都用默认（只模拟）跑起来，观察一两天。
2. 确认 `radar.json` 有项目、`mint_records.json` 里的模拟都通过、没有报错。
3. 钱包充好 BNB（BSC 地址见 `config.json`）后再考虑开自动。
4. 切 `auto` 之前，Li 要在对话里亲口确认。

## 钱包

- BSC 地址：`0x0c2fC9a2aFf5e3B5470Fe8a913A77c84E890FEfD`
- Solana 地址：`HfGPViFxTEzuBdxLX8V4zadRQiGXPfaiMKZHrWwcNNsE`
- 现在是空的，没有 BNB。免费项目也要花 gas，必须先充 BNB 才能打。
- Agent ID 注册合约：`0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F`
  （`balanceOf > 0` 就是 Agent ID 持有者）
- Agentic Wallet 每日限额 $50,000，会话 48 小时过期（到 2026-11-25 23:42），
  过期后要重新扫码登录（`baw auth signin`）。

## TG 机器人

- Bot：@Renwuzhijia_bot
- 推送目标：Li 的 Telegram chat ID `9114908461`
- 命令：`/start` `/status` `/radar` `/holdings` `/mode`
- 按钮（参与 / 不参与 / 挂单 / 卖出）只把决定写进 `decisions.json`，
  不直接发交易。

## 常见问题

- **RPC 连不上**：`config.json` 的 `bsc_rpc` 里有多个备用地址，脚本会自动换下一个。
- **模拟失败**：同一项目 30 分钟内不再试，失败 3 次放弃，原因写进 `mint_records.json`。
- **今日 gas 到上限**：单笔 <= 0.0001 BNB，每天合计 <= 0.003 BNB，每轮每号最多 4 个项目。
- **钱包会话过期**：`baw wallet status` 显示 UNCONNECTED 时，重新跑 `baw auth signin` 扫码。
- **开发者模式**：`baw wallet settings` 里 `devMode.enabled` 目前显示 false，
  需要确认开启方式（见主对话）。
