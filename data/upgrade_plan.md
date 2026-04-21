# ETH量化系统升级计划

## 本次（2026-04-21 第十六轮）完成

### grid_pro.py：补齐动态整体止损 + 新增动态峰值回撤上限

**问题1（第15轮漏实现）：** upgrade_plan.md 记载"整体止损动态化已完成"，但代码审计发现
on_tick Step 6 仍为 `if unrealized <= -self._whole_stop`，使用的是初始化时固定的5.0U。

**修复1：** 在 Step 6 引入局部变量：
```
_eff_whole_stop = max(4.0, equity * 0.10) if equity else self._whole_stop
```
- 账户 50U → 止损 5.0U（与原来相同）
- 账户 80U → 止损 8.0U（账户增长时放宽）
- 账户 35U → 止损 4.0U（账户缩水时收紧，4U为底）
- equity 无效 → fallback 固定 self._whole_stop = 5.0U

**问题2（P3计划项）：** `_DailyPnL._drawdown_limit = 2.0U` 固定值，账户成长后偏紧。

**修复2：** 新增 `set_dynamic_drawdown_limit(equity)` 方法：
```
self._drawdown_limit = max(1.5, equity * 0.04)
```
在 Step 4 equity 计算完成后立即调用（1-tick 滞后，WS频率下<1秒可接受）。
- 账户 50U → 回撤上限 2.0U（约为原固定值）
- 账户 80U → 回撤上限 3.2U（随账户成长放宽）
- 账户 35U → 回撤上限 1.5U（下限保护）

**效果预期：** 止损不再因账户余额变化而不合比例地宽/紧，
两个阈值均锚定在账户余额的固定百分比（10% 整体止损 / 4% 回撤上限）。

---

## 历史完成

### 第十五轮（2026-04-20）
- [x] grid_pro.py: 整体止损计划动态化（upgrade_plan记录已完成，但代码实际未落地 → 本轮补齐）

### 第十四轮（2026-04-20）
- [x] runner.py: WS主循环 dispatch_tick 异常防护
- [x] grid_pro.py: _maybe_trail_tp 增加 RANGING 模式感知

### 第十三轮（2026-04-20）
- [x] settings.py: GRID_CONTRACTS_PER_SLOT 0.2→1.0
- [x] grid_pro.py: _update_tp增加RANGING模式TP系数0.8×spacing

### 第十二轮（2026-04-20）
- [x] grid_pro.py：修复 `_sz()` int() 截断 Bug
- [x] grid_pro.py：放宽 SHORT_VELOCITY_ALARM_PCT（-0.0025→-0.003）

### 第一~十一轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步, 资金费率, 趋势过滤等

---

## 待解决问题（按优先级）

- [ ] P1: 检查 runner.py 中 `equity_usdt` 是否写入 runtime 字典
      （动态止损核心依赖：若缺失，两处dynamic计算均fallback固定值，形同虚设）
- [ ] P1: 验证生产日志——确认日志出现 `有效阈值=X.XXU 余额=XX.XXU` 字段
- [ ] P1: 确认 analysis.jsonl 中 RANGING 模式 TP trail 参数是否按预期触发
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P3: RANGING TP trail步长0.15格是否最优（可依成交速度数据动态调整至0.10~0.20）

---

## 下次优先行动

1. **P1: 审计 runner.py → equity_usdt 数据流**
   - 搜索 `equity_usdt` 在 runner.py 和 exchange.py 中的写入位置
   - 确认 OKX REST /account 接口返回的 `totalEq` 字段被正确映射到 runtime["equity_usdt"]
   - 若缺失：在 runner.py 的 _fetch_account_info 中补充写入

2. **P2: 验证动态止损实际效果**
   - 日志关键词：`有效阈值=` `余额=`
   - 检查 drawdown_limit 是否在 tick 中随 equity 变化

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 16轮迭代；P0/P1/P2全面修复；P3精化完成2项
  - 最新：双动态阈值（整体止损 + 峰值回撤）随账户余额自适应
  - 主要缺口：equity_usdt 数据流待验证，动态功能依赖此字段
- **主要风险点**：
  1. equity_usdt 若未正确写入 runtime，动态计算fallback固定值（5U止损 / 2U回撤）
  2. 无法访问生产日志，改进效果依赖代码分析而非实盘验证
  3. 市场数据不可用，无法做FGI/资金费率自适应调整
- **累计运行轮次**：16
