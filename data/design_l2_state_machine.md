# L2 状态同步架构重构 — 设计文档

**背景**：Loss Ledger 记录了 8 条 L2 家族 bug（占总数 62%），根因都是同一个 —— grid 内部状态 vs OKX 实际状态不一致。当前周期性轮询 sync 机制存在竞态，修了又犯。

**目标**：把"grid 内部状态"从**权威源**降级为**缓存**，OKX WS 推送作为**唯一真相源**。

---

## 一、现有架构问题（必须打破的幻觉）

### 1.1 状态双轨
```
grid_pro 内部:           self._slots (长度 4)
                         self._total_held
                         self._tp_order_id
                         self._vwap
                             ↕ 周期性轮询 (每 30-60s) ↕
OKX 实际:                /api/v5/account/positions
                         /api/v5/trade/orders-pending
```

### 1.2 问题场景（8 条 L2 来源）
- **事件丢失**：TP 成交瞬间 grid 刚好重启 → 外部成交没进 session log → 幽灵仓
- **竞态**：`_place_entry` 发单到 `_sync_orders` 轮询之间 5s，填充发生 → state 乱
- **周期盲区**：TP post_only 被拒 → grid 不知道 → 仓位裸奔
- **状态转移不全**：slot 从 ENTRY_LIVE → 外部成交时，转 HOLDING 的代码路径只有 `_sync_orders` 能触发

---

## 二、新架构（事件驱动 state machine）

### 2.1 核心思想
```
OKX WS channels ──推送──> EventBus ──分发──> StateMachine ──更新──> SlotState
                                                │
                                                └──> ActionQueue ──> REST API
```

### 2.2 订阅的 WS channels
- `orders`：订单状态变化（placed / filled / partial / canceled / rejected）
- `positions`：持仓变化（open / close / size_change）
- `account`：余额变化
- 现有的 `books5` + `tickers` 保留

### 2.3 SlotState 状态机（显式转移）
```
         ┌─────────┐
         │  EMPTY  │
         └────┬────┘
              │ place_order(OK) ← event: order_placed
              ▼
       ┌─────────────┐
       │ ENTRY_LIVE  │
       └──────┬──────┘
              │
    ┌─────────┴──────────┬────────────┐
    │ order_filled       │ order_cancelled  │ order_rejected
    │                    │                  │
    ▼                    ▼                  ▼
┌─────────┐         ┌────────┐         ┌────────┐
│ HOLDING │         │ EMPTY  │         │ RETRY  │
└────┬────┘         └────────┘         └────────┘
     │
     │ tp_filled / per_slot_stop
     ▼
  ┌───────┐
  │ EMPTY │
  └───────┘
```

**关键不变量**：
- `sum(slot.fill_sz for slot in HOLDING) == okx.long_pos`（长仓侧）
- `sum(slot.fill_sz for slot in HOLDING) == okx.short_pos`（短仓侧）
- 任何违反 → 立即告警 + 触发 `_reconcile()`

### 2.4 事件处理器（每个 event 都有唯一 handler）
```python
class OrderEventHandler:
    def on_order_placed(self, evt): ...      # slot: EMPTY → ENTRY_LIVE
    def on_order_filled(self, evt): ...      # slot: ENTRY_LIVE → HOLDING, or HOLDING → EMPTY (if TP)
    def on_order_cancelled(self, evt): ...   # slot: ENTRY_LIVE → EMPTY
    def on_order_rejected(self, evt): ...    # slot: ENTRY_LIVE → RETRY
    def on_partial_fill(self, evt): ...      # slot: update fill_sz, state unchanged

class PositionEventHandler:
    def on_position_change(self, evt): ...   # 验证 invariant，若违反则 _reconcile

class AccountEventHandler:
    def on_balance_change(self, evt): ...    # 更新 margin/equity 缓存
```

---

## 三、实施路径（影子模式 shadow mode）

### Phase 1：WS 订阅 + EventBus（~4h）
- [ ] 在 `quant/exchange/` 加 `ws_private.py`（OKX private WS auth + subscribe）
- [ ] 订阅 `orders` / `positions` / `account`
- [ ] 所有事件投递到 `asyncio.Queue`，不影响现有 grid_pro 逻辑

### Phase 2：state machine 骨架（~6h）
- [ ] `quant/strategy/state_machine.py` 新文件
- [ ] 不替换 grid_pro，作为**观察者**运行
- [ ] 每个 event 映射到虚拟的 `ShadowSlotState`
- [ ] 日志记录：每个事件的 before/after state

### Phase 3：shadow 验证（~24h 连续运行）
- [ ] shadow state 每 60s 打一次 snapshot
- [ ] 和 grid_pro 内部 `_slots` 对比，记录 divergence
- [ ] 预期：**正常 = 100% 一致**；出现不一致说明旧 grid_pro 有 bug（我们正是要抓这个）
- [ ] 每出现一次 divergence → 分析根因 → 归到 Loss Ledger

### Phase 4：flip 切换（需主人手动）
- [ ] shadow 完全一致 48h 后
- [ ] 加环境变量 `GRID_STATE_MACHINE=shadow|active|off`
- [ ] active 模式下 state machine 是权威，grid_pro `_slots` 只是缓存
- [ ] 旧的 `_sync_orders` / `_position_sync_check` 关闭（但保留为回退）

### Phase 5：清理
- [ ] flip 后再跑 72h 稳定
- [ ] 删除旧 sync 代码
- [ ] Loss Ledger L2-001 到 L2-008 全部标记为 **架构层彻底防护 ✅**

---

## 四、风险与回退

### 风险
- **WS 私有 channel 连接稳定性**：OKX 私有 WS 需要 login + 每 30s ping，断连后要自动重订阅
- **事件丢失**：如果 WS 掉了而 orders 在服务器上变化了 → 重连时必须强制 `_reconcile()`（拉快照对齐）
- **shadow 模式性能**：双写不应有性能压力，但需要在日志里确认

### 回退开关
- `.env GRID_STATE_MACHINE=off` 即刻回到纯轮询模式
- 任何实盘 bug → 1 秒回退

---

## 五、验收标准

- [ ] shadow 模式 24h，divergence 数 ≤ 1（且每次都能解释）
- [ ] 48h 后 divergence = 0
- [ ] flip 后 72h，0 个 L2 新 bug
- [ ] 所有 L2-001 ~ L2-008 的重现场景都能正确处理（单元测试或手动触发）

---

**预期总工期**：2-3 天（AI daemon 按上述 Phase 顺序逐轮推进）
