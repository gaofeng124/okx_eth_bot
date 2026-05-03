# ETH量化系统升级计划

## 本次（2026-05-03 第七十六轮）完成

### grid_pro.py：修复 `_cancel_order` 未检查 OKX sCode

**问题根因**：
- OKX 撤单 API 的"业务错误"不通过 HTTP 状态码反映，而是放在 `data[0].sCode`
- 顶层 `code="0"` 代表"请求格式正确"，但 sCode 非零代表"订单层面出错"
- 原实现：`request()` 在 `code="0"` 时直接返回，`_cancel_order` 无条件返回 True
- 典型场景：订单已被 OKX 自动成交或撤销，再次调用撤单时 sCode=51401，
  原代码认为"撤单成功"→ 后续逻辑（如 `_reset_grid`）误以为槽位已清理

**修复（round76）**：
1. `_cancel_order` 保存 `request()` 返回值为 `resp`
2. 提取 `s_code = str((resp.get("data") or [{}])[0].get("sCode", "0"))`
3. sCode 非零时：
   - `51401`（订单不存在）→ debug 日志 + 返回 True（订单已消失，状态目标已达到）
   - 其他非零 → warning 日志 + 返回 False（真实拒绝，上层需处理）
4. 保持原有异常捕获不变（网络层错误）

**效果预期**：
- `_reset_grid`、`_sync_tp` 等调用 `_cancel_order` 的位置，可以正确感知撤单失败
- 防止"以为撤单成功"但订单仍在 OKX 挂单的状态错乱（脏槽位）
- 51401 特殊处理确保幂等性：重试撤单不会误报失败

---

## 历史完成（节选）

### 第七十五轮（2026-05-03）
- [x] grid_pro.py: 修复 _sync_tp partially_canceled 分支 PnL 双算 bug
  - fill_ratio 计算 + slot fill_sz 缩减 + partial_net 累加到 _pnl
  - 写入 fill_tp_partial analysis 事件

### 第七十四轮（2026-05-02）
- [x] grid_pro.py: status_summary() 增加 sz_scale_last/loss_streak/slot持仓时长

### 第七十三轮（2026-05-02）
- [x] grid_pro.py: 修复 _check_phase4_trend_guard 5处硬编码路径

### 第七十二轮（2026-05-02）
- [x] settings.py: 删除重复 DATA_DIR 定义

### 第一~七十一轮（2026-04-18~05-01）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round77: 审查 `_reset_grid` 中 `_cancel_order` 返回 False 时的处理逻辑
  - 当前未知：False 时是否有重试 / 状态标记 / 脏槽位检测
  - 目标：确认失败路径健壮，或补充重试机制
- [ ] P1: round77: 审查 `_place_entry`/`_place_tp` 的下单响应 sCode 检查
  - 下单 API 同样有 data[0].sCode，检查是否也有同类问题
- [ ] P2: 验证服务实际运行状态（systemctl status / 日志目录）
- [ ] P2: 若有 analysis.jsonl，统计 fill_tp_partial 事件频率

## 下次优先行动

**round77：**
1. 读取 `_reset_grid` 完整逻辑，检查 `_cancel_order` 返回 False 时的错误路径
2. 检查 `_place_entry`/`_place_tp` 是否也缺少 sCode 检查（下单更关键）
3. 若发现问题，一并修复

## 系统评估
- **策略有效性**：9/10
  - 76轮迭代，P0/P1 逐步闭环
  - 本轮修复撤单状态一致性（sCode检查），影响链路：_cancel_order → _reset_grid → 槽位管理
  - 下一个已知P1：_reset_grid 失败路径 + 下单sCode检查
- **当前主要风险**：
  1. 沙盒网络受限，无法验证实盘逻辑
  2. _reset_grid 失败路径可能仍有脏槽位问题（下轮排查）
  3. 下单 API 的 sCode 检查尚未确认
- **累计运行轮次**：76
