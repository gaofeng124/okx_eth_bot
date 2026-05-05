# ETH量化系统升级计划

## 本次（2026-05-05 第八十七轮）完成

### 修复：_position_sync_check positive-diff 路径缺少 record_analysis 监控事件

**问题：**
`_position_sync_check` 有两条路径：
- `diff < -threshold`（幽灵仓）：第86轮已添加 `record_analysis("ghost_position_sync", ...)`
- `diff > threshold`（未追踪仓位补录）：**缺少 record_analysis**，只有 log.warning/log.info，无法在 analysis.jsonl 中统计触发频率

**修复：**
在 `_update_tp()` 调用前插入：
```python
try:
    from quant.detailed_daily_log import record_analysis
    record_analysis(
        "untracked_position_sync",
        exchange_sz=exchange_sz,
        internal_held=internal_held,
        diff=round(diff, 4),
        est_entry=round(est_entry, 2),
        daily_pnl_realized=round(self._pnl.realized, 4),
    )
except Exception:
    pass
```

**验证 _replay_tp_history（P1 第二项，已确认无问题）：**
- 两层 `except Exception: pass/continue` 均为主动防御设计，非意外吞错
- `ts_wall` 格式：`datetime.now().isoformat(timespec="milliseconds")` → 如 `"2026-05-05T07:00:00.123"`，Python 3.11 `fromisoformat` 可正确解析，无时区问题
- `event != "fill_tp"` 过滤条件覆盖所有旧格式，静默跳过合理
- 冷启动无历史时 EWMA 使用默认值，行为正确

**效果预期：**
- analysis.jsonl 中正/负两个方向的持仓同步事件均有记录
- 实盘可统计 untracked_position_sync vs ghost_position_sync 频率，判断 API/内部一致性趋势

---

## 历史完成（节选）

### 第八十六轮（2026-05-05）
- [x] grid_pro.py: 修复 _position_sync_check 负差路径 ratio 变量定义但未使用，改为等比缩减 slot fill_sz，PnL 计算精准
- [x] grid_pro.py: 新增 ghost_position_sync 监控事件

### 第八十五轮（2026-05-05）
- [x] grid_pro.py: _reset_grid_state 根治 spacing 清零问题
- [x] grid_pro.py: _emergency_close 补录 TP partial fill PnL

### 第八十四轮（2026-05-04）
- [x] grid_pro.py: 修复 _maybe_trail_tp spacing_abs=0 零利润平仓 bug

---

## 待解决问题（按优先级）

- [ ] P1: round88: 检查 `_update_tp` 在 total_held 从 0→正值（首次 untracked 补录）场景是否正确设置 TP 方向和价格（触发条件：机器人重启后 API 先返回持仓但 grid 尚未初始化）
- [ ] P1: round88: 检查 `_cancel_order`（line ~2384-2386）在 ENTRY_LIVE slot 撤单失败时是否有 fallback：若撤单 API 失败，slot 状态应回退还是继续补录？当前无 try/except
- [ ] P2: 评估 _refresh_fgi/_refresh_funding REST fallback 在沙盒环境 timeout 代价（均有 except 兜底，不影响主策略；实盘无此问题）
- [ ] P3: analysis.jsonl 中记录每次 position_sync_check 的 diff 摘要（不仅在异常时，也在正常时每N次记录一次），便于长期 API 一致性监控

## 下次优先行动

**round88：**
1. 读取 `_update_tp` 实现（搜索 `def _update_tp`），确认当 `self._total_held > 0` 且之前 `== 0` 时是否正确初始化 TP 订单方向
2. 读取 `_cancel_order` 调用（line 2384-2386 附近），检查 untracked 补录时撤 ENTRY_LIVE 单失败的处理逻辑

## 系统评估
- **策略有效性**：9/10
  - 87 轮迭代，双向持仓同步监控完整（ghost + untracked 均有 analysis.jsonl 记录）
  - core 链条：entry→fill→vwap→TP→trail→timeout→emergency→circuit_break 全覆盖
  - _replay_tp_history 冷启动恢复路径已验证无 exception 吞掉问题
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法通过 API 调用验证
  2. untracked 路径撤 ENTRY_LIVE 单无 try/except，API 失败可能导致 slot 状态不一致
- **累计运行轮次**：87
