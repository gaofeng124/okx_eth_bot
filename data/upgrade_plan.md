# ETH量化系统升级计划

## 本次（2026-05-05 第八十六轮）完成

### 修复：grid_pro.py `_position_sync_check` 部分幽灵仓等比缩减 bug

**问题：**
`_position_sync_check` 中，当 `diff < -_sync_threshold` 且 `exchange_sz >= _sync_threshold`（部分幽灵仓）时：
- 旧代码在 line 2415 计算了 `ratio = exchange_sz / internal_held` 但**从未使用**
- 实际逻辑是 `kept` 逐个累加 slot，超出 `exchange_sz+threshold` 的 slot 整体清零
- 结果：`self._total_held = exchange_sz`（如0.3），但 `sum(s.fill_sz for HOLDING slots)` 可能仅 0.2
- 下次 `_sync_tp` 按 slot 累计 PnL 时，总 PnL 按 0.2 算但 TP 成交量是 0.3 → **PnL 漏算 33%**

**修复：**
改为等比缩减每个 HOLDING slot 的 `fill_sz`：
```python
ratio = exchange_sz / internal_held
for s in held:
    s.fill_sz = round(s.fill_sz * ratio, 8)
self._total_held = exchange_sz
self._vwap_value = self._vwap * exchange_sz
```
确保 `sum(fill_sz for HOLDING) == total_held == exchange_sz`，PnL 计算精准。

**附加：**
新增 `record_analysis("ghost_position_sync", ...)` 监控事件，便于复盘幽灵仓触发频率。

**效果预期：**
- 幽灵仓（部分）场景下 PnL 计算准确，不再漏算
- analysis.jsonl 中有 ghost_position_sync 事件，可统计发生频率

---

## 历史完成（节选）

### 第八十五轮（2026-05-05）
- [x] grid_pro.py: _reset_grid_state 根治 spacing 清零问题（有持仓时从 vol 恢复）
- [x] grid_pro.py: _emergency_close 补录 TP partial fill PnL

### 第八十四轮（2026-05-04）
- [x] grid_pro.py: 修复 _maybe_trail_tp 在 spacing_abs=0 时的零利润平仓 bug（防御性 guard）

### 第八十三轮（2026-05-04）
- [x] grid_pro.py: 修复 _tp_exposed_since 与 circuit_break 双路径竞争

### 第八十二轮（2026-05-04）
- [x] grid_pro.py: 修复 _emergency_close API失败无兜底

---

## 待解决问题（按优先级）

- [ ] P1: round87: 检查 _position_sync_check positive-diff 路径（untracked position 补录，line ~2397）是否缺少 record_analysis 监控事件（目前只有 log.warning，无 analysis.jsonl 记录）
- [ ] P1: round87: 验证 _adaptive_trail_trigger/offset EWMA 冷启动历史重播（_replay_tp_history）是否正确：若 analysis.jsonl 为空或格式不匹配，EWMA 无历史 → trail 参数全程用默认值；检查是否有 exception 吞掉了重播错误
- [ ] P2: 评估实盘 _refresh_fgi/_refresh_funding REST fallback 在沙盒环境的 timeout 代价（均有 except 兜底，不影响主策略；实盘无此问题）
- [ ] P3: 考虑在 analysis.jsonl 中记录每次 position_sync_check 的 diff，便于长期监控 API/内部一致性

## 下次优先行动

**round87：**
1. 读取 `_position_sync_check` positive-diff 路径（untracked position 补录，约 line 2327-2402）
   确认是否缺少 `record_analysis` → 如缺，添加 `"untracked_position_sync"` 事件
2. 读取 `_replay_tp_history`（约 line 1682-1750），检查 exception 是否被静默吞掉，
   确认格式匹配条件 `if rec.get("event") != "fill_tp": continue` 是否覆盖所有情况

## 系统评估
- **策略有效性**：9/10
  - 86 轮迭代，核心链条完整：entry→fill→vwap→TP→trail→timeout→emergency→circuit_break
  - 本轮修复 slot fill_sz 与 total_held 不一致的 PnL 精度 bug（低概率但影响账务）
  - ghost_position_sync 监控事件补全，analysis.jsonl 覆盖更完整
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法通过 API 调用验证
  2. 幽灵仓修复后 ratio 缩减路径在实盘触发频率未知（理论上 < 0.1% 的 tick）
- **累计运行轮次**：86
