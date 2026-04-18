# ETH量化系统升级计划 — Agent 自主任务书

## 🎯 核心使命
本金50U，10倍杠杆，ETH-USDT-SWAP永续合约网格策略。
目标：每日稳定盈利2U，最终扩展到50U/天。
**本周无亏损限制，完全授权自主修改，包括重写整个项目。**

---

## 🔴 P0 致命Bug（必须立刻修复，未修复则系统每天只赚1.5U就停止）

### 1. GRID_DAILY_TARGET=1.5U → 系统停止交易
- 文件：`quant/settings.py` 第472行
- 问题：`GRID_DAILY_TARGET_USDT = 1.5` 赚了1.5U就进入利润保护模式，停止所有新订单
- 修复：改为 `999.0`（等效于无上限）
- 同步修复：`quant/strategy/grid_pro.py` 构造函数默认值 `daily_target_usdt=2.5` → `999.0`

### 2. GRID_DRAWDOWN_FROM_PEAK=1.0U → 频繁紧急平仓
- 文件：`quant/settings.py` 第473行
- 问题：`GRID_DRAWDOWN_FROM_PEAK_USDT = 1.0` 从峰值回撤1U就紧急平仓，极其敏感
- 修复：改为 `3.0`
- 同步修复：`quant/strategy/grid_pro.py` 构造函数 `drawdown_from_peak_usdt=1.5` → `3.0`

### 3. run_strategy.py 硬编码Mac路径
- 文件：`run_strategy.py` 第58行
- 问题：`lock_path = "/Users/gaofeng/Documents/okx_eth_bot/data/logs/run_strategy.lock"`
- 修复：`lock_path = str(Path(__file__).resolve().parent / "data" / "logs" / "run_strategy.lock")`
- 需要在文件头部导入：`from pathlib import Path`

### 4. 服务器.env缺少关键参数
服务器IP：8.208.25.221，密码：1240954013Gf.
需要在服务器 `/root/okx_eth_bot/.env` 末尾追加：
```
BOT_MAX_SESSION_HOURS=24
GRID_DAILY_TARGET_USDT=999
GRID_DRAWDOWN_FROM_PEAK_USDT=3.0
GRID_WHOLE_STOP_USDT=5.0
GRID_DAILY_STOP_USDT=6.0
GRID_MIN_SPACING_PCT=0.0010
GRID_ATR_MULT=1.2
GRID_WARMUP_TICKS=30
GRID_COOLDOWN_SEC=120
```

---

## 🟡 P1 重要问题（P0完成后立刻处理）

### 5. 4小时自杀计时器
- 文件：`quant/app/runner.py` 第3253行
- 问题：`BOT_MAX_SESSION_HOURS=4` 每4小时SIGTERM自杀重启
- 修复：在服务器.env设置 `BOT_MAX_SESSION_HOURS=24`（已在P0第4条）
- 代码层：默认值从4改为24：`max_hours = float(_os.environ.get("BOT_MAX_SESSION_HOURS", "24"))`

### 6. analysis.jsonl只有grid_status事件，无成交记录
- 文件：`quant/strategy/grid_pro.py`
- 问题：成交时没有写入analysis日志，无法查看成交历史
- 修复：在槽位成交（fill）时，用 detailed_daily_log 写入事件

### 7. 网格ATR过低导致格宽紧贴最小值
- 当前ATR：0.38-0.43 bps（极低）
- `GRID_MIN_SPACING_PCT=0.0012` → 改为 `0.0010`
- `GRID_ATR_MULT=0.8` → 改为 `1.2`（放大系数使格宽更合理）

---

## 🟢 P2 策略优化（持续推进）

### 8. 仓位大小与本金不匹配
- 当前：`RISK_MAX_NOTIONAL_USDT=500`（远超50U本金能承受的）
- 建议：`RISK_MAX_NOTIONAL_USDT=100`（单笔最大100U名义=1U保证金，安全）
- `LEV5_ORDER_SIZE_CONTRACT=1`（维持1张起步）

### 9. 市场状态适应性不足
- 极度恐慌(FGI<25)时：减少档位到2档，缩小格宽
- 震荡行情：保持4档，标准格宽
- 趋势行情：减少档位，增大格宽

### 10. TP超时止损逻辑
- 当前：持仓10分钟且跌破VWAP-格宽才止损，条件太严
- 建议：持仓超过8分钟且浮亏>0.5U直接止损

---

## 📋 上次修改记录
- notify.py：邮件优化（精准盈亏/网格状态/成交记录）✅
- watchdog.sh：git fetch bug修复 ✅
- settings.py：部分参数已修改（ATR_MULT/MIN_SPACING等）✅ 待确认P0参数
- run_strategy.py：路径bug已修复 ✅

## ⚡ 下次Agent运行立刻做
1. 确认settings.py的P0参数已修改为正确值，如未改则立刻改
2. 修改runner.py的BOT_MAX_SESSION_HOURS默认值
3. SSH到服务器更新.env（服务器IP: 8.208.25.221，密码: 1240954013Gf.）
   **注意：Agent无法SSH，只能通过修改代码+git push让watchdog更新服务器**
4. 修改grid_pro.py构造函数默认值
5. 提交所有修改并push

## 🔑 关键信息
- 服务器：8.208.25.221（已由watchdog自动拉取GitHub更新并重启）
- 每次push到GitHub后，服务器watchdog在5分钟内自动pull+重启+发邮件
- 邮件发送到：1240954013@qq.com
- 本金：50U，目标：2U/天
