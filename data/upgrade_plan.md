# ETH量化系统升级计划

## 本次（2026-05-06 第九十一轮）完成

### P2 修复：`_replay_tp_history` regime 分别采样（grid_pro.py line 1729-1747）

**问题：**
原实现从已排序的全量 `records` 中取最近 40 条统一处理，按 regime 分发到两个 bucket。若近期交易集中在 TRENDING 行情（如单边上涨/下跌），最近 40 条可能全是 TRENDING 记录，导致重启后 `_tp_profits_ranging` 为空，RANGING EWMA 无法即时生效，需等新成交才能恢复自适应能力。

**修复：**
```python
# 修改前：records[-40:] 混合分发
for ts, ps, regime_str in records[-40:]:
    ...

# 修改后：按 regime 分别截取最近20条
ranging_recs  = [(ts, ps) for ts, ps, r in records if r not in ("TRENDING_UP","TRENDING_DOWN")]
trending_recs = [(ts, ps) for ts, ps, r in records if r in ("TRENDING_UP","TRENDING_DOWN")]
for ts, ps in ranging_recs[-20:]:
    self._tp_profits_ranging.append((ts, ps))
for ts, ps in trending_recs[-20:]:
    self._tp_profits_trending.append((ts, ps))
```

**效果预期：**
- 每个 bucket 独立保留最近 20 条历史，不受另一 regime 占用影响
- 行情切换时两个 bucket 均有历史基础，EWMA 自适应即时可用
- 日志新增 `ranging原始N/trending原始N` 字段，方便追踪重播覆盖率

---

## 历史完成（节选）

### 第九十轮（2026-05-06）
- [x] grid_pro.py: `_position_sync_check` 正常路径每30次记录 `position_sync_ok` 事件，长期监控 API 一致性

### 第八十九轮（2026-05-05）
- [x] grid_pro.py: `_maybe_trail_tp` 节流盲区修复（`_reset_grid_state` 和 `_position_sync_check` 恢复 spacing 后重置 `_last_tp_trail_ts = 0.0`）

### 第八十八轮（2026-05-05）
- [x] grid_pro.py: `_position_sync_check` untracked 补录路径：`_cancel_order` 返回值被忽略改为撤单失败时 continue

### 第八十七轮（2026-05-05）
- [x] grid_pro.py: `_position_sync_check` positive-diff 路径补充 `record_analysis('untracked_position_sync')`

---

## 待解决问题（按优先级）

- [ ] P1: round92 验证：analysis.jsonl 每日目录轮转路径是否在换天后自动切换
  - 检查 `data/logs/daily/<date>/analysis.jsonl` 的 `<date>` 组件如何生成
  - 若使用 `_date.today()` 在初始化时固定，换天后路径不会更新，需改为运行时动态求值
- [ ] P1: 实盘验证 `position_sync_ok` 事件是否正常写入 analysis.jsonl（round90 新增）
- [ ] P2: 评估 `_refresh_fgi`/`_refresh_funding` REST fallback 在沙盒 timeout 代价
- [ ] P3: 持续监控 position_sync_ok diff 是否存在趋势性漂移

## 下次优先行动

**round92：**
1. 查找 analysis.jsonl 文件路径的生成逻辑（`_data_dir`、`record_analysis`、log 路由相关代码）
2. 确认换天时 `daily/<date>/` 目录是否动态更新，若不是则修复为 `date.today().isoformat()` 运行时求值
3. 检查是否有日志轮转 or 跨天路径切换的现有机制

## 系统评估
- **策略有效性**：9/10
  - 91 轮迭代，核心链条覆盖完整
  - EWMA 双 bucket 采样偏斜已修复
  - position_sync 正常/异常双路径记录就绪
- **当前主要风险**：
  1. 沙盒环境无法验证实盘 API 调用
  2. analysis.jsonl 跨天路径切换未验证（待 round92）
- **累计运行轮次**：91
