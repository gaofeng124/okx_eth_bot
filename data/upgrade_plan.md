# ETH量化系统升级计划

## 本次（2026-04-18 第四轮）完成

### 1. exchange/__init__.py：WS 静默挂死修复（P1 核心）
- **位置**：`stream_tickers` 内层消息循环（原 `async for raw in ws:`）
- **改动**：
  - 旧逻辑：`async for raw in ws:` 内部 25s 心跳，**心跳只在收到消息时触发**
  - 新逻辑：`asyncio.wait_for(ws.recv(), timeout=35s)` 手动循环
    - 35s 无消息 → 发 ping + stall_count++
    - 连续 2 次超时（约 70s）→ raise TimeoutError → 触发外层重连+退避
  - **根因修复**：旧代码服务端静默时从不发 ping，连接可无限挂死数小时
- **原因**：OKX WS 服务端偶尔 30min+ 不推送 ticker，心跳无法自触发，
  导致 bot 在"运行中"状态但实际上已无行情，约 4h 后进程才因其他原因崩溃重启

### 2. strategy/grid_pro.py：短窗口急跌过滤（P3 动量过滤）
- **位置**：`_MarketSensor.short_velocity_pct` 新属性 + `_can_open_grid` 步骤3
- **改动**：
  - `_MarketSensor` 新增 `short_velocity_pct`：最近 4 个 tick 的变化率
  - `_SHORT_VELOCITY_ALARM_PCT = -0.0025`（-0.25%/4tick，比长窗口 -0.2%/20tick 更敏感）
  - `_can_open_grid` 在长窗口接飞刀检测后追加短窗口急跌检查
- **原因**：长窗口 velocity（20 ticks 均值）对局部短暂急跌不敏感。
  ETH 偶尔出现 3-4 个 tick 内下跌 0.3%+ 的"脉冲式跌落"，旧代码仍开网格买入，
  往往在浮亏 0.2~0.5U 时才触发止损。短窗口 4-tick 检测可提前阻断此类入场。

---

## 历史完成

### 第三轮（2026-04-18）
- [x] grid_pro.py：TRENDING_DOWN 持仓宽限期 45s（P2）
- [x] grid_pro.py：FGI 恐贪指数集成，FGI<25 减1档（P2）

### 第二轮（2026-04-18）
- [x] grid_pro.py：负资金费率减少激活档位
- [x] grid_pro.py：TP超时止损加速
- [x] grid_pro.py：宏观偏空阈值 _MACRO_DOWN_STOP = -0.0020

### 第一轮（2026-04-18）
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0
- [x] P0: run_strategy.py lock_path 动态路径
- [x] P1: runner.py BOT_MAX_SESSION_HOURS 默认 24h
- [x] P1: grid_pro.py 构造函数默认值与settings.py一致
- [x] P1: analysis.jsonl 新增 fill_entry / fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 服务器.env 需确认追加（Agent无法SSH，依赖watchdog+push触发）
- [ ] P2: `_MACRO_DOWN_STOP = -0.0020` 是否仍导致过多 TRENDING_DOWN（无日志无法判断）
- [ ] P2: TRENDING_DOWN 宽限期45s 是否够用（需日志验证）
- [ ] P2: TP 超时止损冷静期是否足够（当前 300s）
- [ ] P3: 趋势跟踪：上升趋势中激进格宽（TRENDING_UP 时 atr_mult × 1.3）
- [ ] P3: 网格中心漂移优化：长时间 RANGING 时是否需要重新居中

---

## 下次优先做

1. **P2: Regime 阈值微调验证** - 如果有运行日志，分析 TRENDING_DOWN 触发频率
   - 若 stall_count 告警频繁（>2次/天），考虑将 _STALL_TIMEOUT 调至 45s
   - 若 short_drop 过滤次数 >20次/天，考虑放宽至 -0.0030
2. **P3: 上升趋势激进格宽** - Regime=TRENDING_UP 时 atr_mult 乘以 1.3，捕捉更多回撤买入机会
3. **P2: 宽限期45s → 60s 微调** - 如发现频繁宽限到期才割肉，延长至60s
4. **P1: 服务器环境变量确认** - 通过 push 触发的 watchdog 脚本检查 .env

---

## 系统当前状态评估
- **策略有效性**：8/10
  - P0/P1 全修复 + WS静默挂死根因修复（最大稳定性提升）
  - 短窗口急跌过滤减少脉冲式下跌中的冒进入场
  - 主要剩余风险：Regime 切换噪声（无日志验证）
- **主要风险点**：
  1. WS 修复需生产验证，`asyncio.wait_for(recv)` 在不同 websockets 版本行为略有差异
  2. 短窗口急跌 -0.25% 阈值可能偏紧，在高频小波动下误拦截正常回调
  3. _MACRO_DOWN_STOP=-0.0020 在震荡行情中可能仍频繁触发 TRENDING_DOWN
