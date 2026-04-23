# ETH量化系统升级计划

## 本次（2026-04-23 第三十轮）完成

### grid_pro.py：两项 bug 修复

**1. Fix1：补仓节流 deque 清理窗口不一致（P1 正确性 bug）**

背景：
- `on_tick` 中有两处节流检查共用同一个 `_recent_entries_ts` deque
- 新格激活节流（step 10f）：`while > 120s` 清理 + `len >= 2` 检查
- 补仓节流（step 12）：`while > 60s` 清理 + `len >= 2` 检查
- 问题：step 12 的 60s 清理会删除 60-119s 内的历史条目，这些条目本应
  在下一次 step 10f 的 120s 窗口中被看到
- 复现场景：t=0 开格（添加条目A），t=70 step12运行（删除A，60s>60s），
  t=80 step10f运行（deque为空，len=0，允许第3次开格！）
- 实际应该：t=80 时条目A（80s<120s）仍在120s窗口内，应节流

修复：step 12 清理窗口从 60s 改为 120s（与新格节流一致）
效果：两个节流检查的清理窗口统一，防止过早删除历史条目导致激活节流失效

**2. Fix2：持仓硬超时兜底（P1 风险控制 bug）**

背景：
- 慢出血止损（7d）使用 `now - self._tp_placed_ts > 1800s`
- 但 `_tp_placed_ts` 在每次 TP 追踪（every 20-30s RANGING mode）时重置
- 结果：TP 在追踪，`_tp_placed_ts` 每 30s 重置，慢出血的 1800s 计时器永不到期
- 场景：价格小幅上行→TP上移追踪→价格下跌→浮亏-0.25U（<0.30阈值）→持仓无限延续
- 已有的 TP aging（7b）也使用 `_tp_placed_ts`，同样被追踪重置

修复：新增 7e 节基于 `slot.fill_ts`（首次成交时间戳，不随追踪重置）
的绝对持仓时长检查，60min 硬超时无条件触发紧急平仓
```python
_held_slots_for_timeout = [s for s in self._slots if s.state == _S.HOLDING and s.fill_ts > 0]
_oldest_fill_ts = min(s.fill_ts for s in _held_slots_for_timeout)
if now - _oldest_fill_ts > 3600.0:
    self._emergency_close("hard_hold_timeout", mid)
```
效果：任何持仓不超过 1 小时，防止资金长期锁定在僵局仓位

---

## 历史完成

### 第二十九轮（2026-04-23）
- [x] grid_pro.py: _price_1h_cache失败后每tick阻塞5s改为60s重试
- [x] grid_pro.py: status_summary增加fgi/atr_baseline_bps/eff_tp_mult监控字段

### 第二十八轮（2026-04-23）
- [x] grid_pro.py: _refresh_fgi失败后改为5分钟重试（原1小时）
- [x] grid_pro.py: fill_tp事件新增诊断字段

### 第二十七轮（2026-04-23）
- [x] grid_pro.py: _atr_baseline持久化（重启恢复）

### 第二十六轮（2026-04-23）
- [x] grid_pro.py: 修复_adaptive_trail_trigger/_adaptive_trail_offset边界裁剪bug

### 第二十五轮（2026-04-22）
- [x] grid_pro.py: ATR基线动态止盈（_atr_baseline慢速EMA + atr_ratio缩放eff_tp_mult）

### 第一~二十四轮（2026-04-18~22）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 网络恢复后验证实盘日志
  - 搜索 `hard_hold_timeout` —— 若出现说明历史有超1h持仓问题（修复验证）
  - 搜索 `_recent_entries_ts` 相关日志，确认节流更稳定
  - status_summary 确认 fgi/atr_baseline_bps/eff_tp_mult 字段输出

## 下次优先行动

1. **P3: 验证两项修复效果**
   - `grep -h "hard_hold_timeout" logs/*.log` 查看是否有超时触发
   - `grep -h "节流跳过" logs/*.log` 确认节流频率是否合理
   
2. **P3: 若无新bug，观察实盘PnL**
   - 目标：2U/天，本周调试期允许亏损
   - 关注：avg_win/avg_loss 比值改善情况

## 系统评估
- **策略有效性**：9/10
  - 30轮迭代；全P0/P1已解决；两项正确性bug修复
  - 本轮修复节流一致性（防过度交易）+硬超时兜底（防无限持仓）
  - 代码成熟度高，剩余改进空间主要在实盘数据反馈驱动的精细调参
- **当前主要风险**：
  1. 外部API网络受限（无法实时监控市场状态）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 硬超时60min可能在临界盈利时触发市价平（taker成本）
- **累计运行轮次**：30
