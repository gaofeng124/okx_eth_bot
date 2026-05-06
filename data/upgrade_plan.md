# ETH量化系统升级计划

## 本次（2026-05-06 第九十轮）完成

### P1 验证：EWMA halflife 配置审查（结论：正确，无需修改）

**审查内容：**
- `_ewma_profit_avg`（line 1660）：RANGING=900s，TRENDING=2700s，正确
- `_tp_profits_ranging` / `_tp_profits_trending`：各自 deque(maxlen=20)，在 `__init__` 初始化，互不干扰
- `_adaptive_trail_trigger` / `_adaptive_trail_offset`：均调用 `self._ewma_profit_avg()`，该函数内部自行按当前 regime 选择 halflife，无需外部传参
- `is_ranging` 参数：调用处（line 1572/1576）正确传入 `_is_ranging = self._current_regime == Regime.RANGING`
- `_tp_current_bucket` 属性：按 `_current_regime` 路由到正确 bucket（line 1676-1678）
- `_replay_tp_history`：从 analysis.jsonl 重播，按 TRENDING_UP/TRENDING_DOWN 路由到 trending bucket，其余归 ranging，逻辑正确

**结论：** round89 的担忧已消除，实现无误。

### P3 实施：_position_sync_check 正常路径定期监控记录

**问题：**
`_position_sync_check` 每10s运行一次，但只在持仓不一致（`untracked_position_sync`/`ghost_position_sync`）时才写 analysis.jsonl 记录。长期运行时无法判断同步是否正常工作、差值是否有趋势性漂移。

**修复：**
1. `__init__` 新增 `self._pos_sync_count: int = 0`
2. `_position_sync_check` else 路径（diff 在阈值内）：`_pos_sync_count += 1`，每30次写一条 `position_sync_ok` 事件（exchange_sz, internal_held, diff, count），约每5分钟一次

**效果预期：**
- 每小时约12条 position_sync_ok 记录，每天约288条
- 可回溯检查 API 持仓是否有规律性漂移，无需依赖异常事件
- try/except 兜底，record_analysis 异常不影响主策略

---

## 历史完成（节选）

### 第八十九轮（2026-05-05）
- [x] grid_pro.py: _maybe_trail_tp 节流在 spacing 恢复后的短暂盲区修复（_reset_grid_state 和 _position_sync_check 两处 spacing 恢复后重置 _last_tp_trail_ts = 0.0）

### 第八十八轮（2026-05-05）
- [x] grid_pro.py: _position_sync_check untracked 补录路径：_cancel_order 返回值被忽略改为撤单失败时 continue

### 第八十七轮（2026-05-05）
- [x] grid_pro.py: _position_sync_check positive-diff 路径补充 record_analysis('untracked_position_sync')

---

## 待解决问题（按优先级）

- [ ] P1: round91: 实盘验证 position_sync_ok 事件是否正常写入 analysis.jsonl
- [ ] P2: 评估 _replay_tp_history 中 records[-40:] 按 regime 分别限制上限的必要性
  - 场景：若最近40条全是 TRENDING 记录，RANGING bucket 为空，restart 后 RANGING 自适应失效
  - 建议改法：分别取 ranging_records[-20:] + trending_records[-20:] 再合并，各 bucket 独立保留
- [ ] P2: 评估 _refresh_fgi/_refresh_funding REST fallback 在沙盒 timeout 代价
- [ ] P3: 持续监控 position_sync_ok diff 是否存在趋势性漂移（连续多次 diff>0 可能预示慢漏）

## 下次优先行动

**round91：**
1. 实施 _replay_tp_history regime 分别取样改进：
   - `ranging_recs = [r for r in records if r[2] not in ("TRENDING_UP","TRENDING_DOWN")]`
   - `trending_recs = [r for r in records if r[2] in ("TRENDING_UP","TRENDING_DOWN")]`
   - 分别 `-20:` 截取后合并重播，消除一个 regime 占满配额的问题
2. 检查 analysis.jsonl 写入路径是否在每日目录下正确轮转（换天后路径是否自动切换）

## 系统评估
- **策略有效性**：9/10
  - 90 轮迭代，核心链条已完整覆盖
  - P1 EWMA halflife 验证完毕，实现无误
  - position_sync 现有正常/异常双路径记录
- **当前主要风险**：
  1. 沙盒环境无法验证实盘 API 调用
  2. _replay_tp_history 存在 regime 采样偏斜风险（待 round91 修复）
- **累计运行轮次**：90
