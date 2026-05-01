# ETH量化系统升级计划

## 本次（2026-05-01 第六十七轮）完成

### grid_pro.py：修复 _sync_tp 中 _recent_close_pnls 的 per-slot 粒度问题

**问题（bug 描述）**：
`_sync_tp`（TP 成交处理）在 `for s in self._slots:` 循环内，对每个 HOLDING slot 单独调用 `self._recent_close_pnls.append(net_after)`。

当网格有 3 个 HOLDING slot 同时 TP 成交时（全部为正收益）：
- 3 次 append 占满 deque(maxlen=3)，完全覆盖了之前的 2 次紧急亏损记录
- 从 loss_streak 角度看是"有利的"——但与 round66 修复的 _market_close_all 风格不一致
- 若未来出现极端场景（TP 后某些 slot 小亏），per-slot 多次 append 可能误触发 loss_streak

**修复**：
- 将 `self._recent_close_pnls.append(net_after)` 从 for 循环内移至循环后
- 改为 `self._recent_close_pnls.append(total_net)`，整个 TP 会话只记录 1 次
- 与 round66 的 _market_close_all 修复完全对称：两个平仓路径统一为 session 粒度

### grid_pro.py：loss_streak 冷静期 1800s → 900s

**理由**：
- 每次触发冻结 30 分钟，若每日触发 3 次 = 90 分钟无法开新仓（6.25% 日内时间浪费）
- 缩短至 900s（15 分钟）：每日 3 次触发 = 45 分钟损失，节省 45 分钟有效交易时间
- 15 分钟足够平息大多数短期波动并让市场重新稳定
- 配合 round66 的 session 粒度修复（现在需要 2 次独立会话才触发），实际触发频率已降低

**效果预期**：
- 极端行情下（频繁止损）每日多约 0.5~1.5 次网格机会
- 与 per_slot_stop=1.0U + loss_streak 仅按会话触发协同，整体减少不必要的停机时间

---

## 历史完成（节选）

### 第六十六轮（2026-04-30）
- [x] grid_pro.py: _market_close_all 中 loss_streak 改为会话粒度（多 slot 单次紧急平仓只算1次事件）

### 第六十五轮（2026-04-30）
- [x] settings.py: GRID_PER_SLOT_STOP_USDT 0.8 → 1.0

### 第六十四轮（2026-04-30）
- [x] grid_pro.py: _place_grid 新增 record_analysis('grid_opened') 事件

### 第一~六十三轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P2: round68：检查 _TP_AGING_SEC(1800s) 和 _SLOW_BLEED_AGING_SEC(1800s)
  - 这两个 1800s 是 TP 挂单存活时间（不是冷静期），不受 loss_streak 影响
  - 但应确认是否应随市场节奏调整：RANGING 时 900s 是否更合适
  - 条件：需先看实盘数据的 TP 成交时间分布

- [ ] P2: round68：_sz_scale 中间档 ATR 28-35bps → sz=0.85（待实盘验证后决定）

- [ ] P2: round69：分析 analysis.jsonl 的实际写入情况
  - 若 DATA_DIR 路径正确但文件仍为空，需检查 record_analysis 的 try/except 是否吞掉了异常

- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证（需实盘数据）

## 下次优先行动

**round68：**
1. 检查 _TP_AGING_SEC 的 RANGING 值是否可从 1800s 降至 900s（减少长时间挂单占用保证金）
2. 验证 record_analysis 在 try/except 内是否有未被捕获的路径问题
3. 若获得市场数据，基于 FGI 和资金费率做 P2 动态参数调整

## 系统评估
- **策略有效性**：9/10
  - 67轮迭代；全P0/P1已解决；两个平仓路径（TP+强平）均改为session粒度
  - loss_streak 触发机制更合理：需2次独立亏损会话 + 15min冷静期（原30min）
- **当前主要风险**：
  1. 实盘日志仍为空，所有优化依赖理论推导未经实盘数据验证
  2. 沙盒网络受限，无法实时获取市场数据
  3. 900s 冷静期若过短，高波动行情中可能过早恢复开仓导致连续亏损
- **累计运行轮次**：67
