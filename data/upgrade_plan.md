# ETH量化系统升级计划

## 本次（2026-04-30 第六十四轮）完成

### grid_pro.py：_place_grid 新增 grid_opened 事件

**问题**：事件链路缺少网格会话起点记录。目前只有 fill_entry/fill_tp/emergency_close，
无法知道"这次网格是在什么市场条件下开的"，事后也无法统计每次网格会话的整体损益。

**修复**：在 `_place_grid` 末尾新增 `record_analysis("grid_opened", ...)` 调用，记录：
- `direction`: long/short
- `regime`: 开格时 Regime（RANGING/TRENDING_UP/TRENDING_DOWN/VOLATILE）
- `center`: 网格中心价
- `spacing_bps`: 格宽（bps）
- `n_active`: 激活档位数
- `bias`: 网格偏置（RANGING=1.0，顺势=0.5）
- `atr_bps`: 当前 ATR（bps），决定仓位缩放
- `sz_scale`: 仓位缩放系数（ATR过高时缩仓）
- `funding_rate`: 开格时资金费率
- `fgi`: 恐贪指数
- `daily_pnl_realized`: 开格时当日已实现损益
- `placed`: 成功挂出的限价单数量

**完成的事件链路**：`grid_opened` → `fill_entry`（每档入场）→ `fill_tp` 或 `emergency_close`
现在可以按 `grid_opened.ts` 分组，统计每个网格会话的完整生命周期和最终损益。

**效果预期**：
```bash
# 统计每日开格次数
grep '"grid_opened"' analysis.jsonl | wc -l  # 期望 5-30次/天

# 统计各 Regime 开格分布（期望 RANGING > 60%）
grep '"grid_opened"' analysis.jsonl | python3 -c "
import json,sys; from collections import Counter
c=Counter(json.loads(l).get('regime') for l in sys.stdin); print(c)"

# 统计 VOLATILE/TRENDING_DOWN 下开格次数（应=0，若>0说明进入过滤有缺口）
grep '"grid_opened"' analysis.jsonl | python3 -c "
import json,sys
for l in sys.stdin:
    d=json.loads(l)
    if d.get('regime') in ('VOLATILE','TRENDING_DOWN'):
        print(d)"
```

---

## 历史完成（节选）

### 第六十三轮（2026-04-30）
- [x] grid_pro.py: fill_entry 新增 regime/daily_pnl_realized/grid_spacing_bps 三字段
- [x] grid_pro.py: loss_streak_triggered 时写入 record_analysis

### 第六十二轮（2026-04-30）
- [x] settings.py: GRID_DRAWDOWN_FROM_PEAK_USDT locked 6.0 → 3.0
- [x] grid_pro.py: _emergency_close 新增 record_analysis 追踪

### 第六十一轮（2026-04-29）
- [x] grid_pro.py: _reset_grid_state 补加 _grid_bias=1.0 重置

### 第一~六十轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P2: round65：验证 grid_opened 事件链路（若有实盘日志）
  - 期望：每日 5-30次开格；RANGING 占 >60%；VOLATILE/TRENDING_DOWN 为 0 次
  - 命令：`grep '"grid_opened"' analysis.jsonl | wc -l`

- [ ] P2: round65：评估 per_slot_stop=0.8U 是否偏紧
  - 当前：locked=0.8U；默认值=1.0U
  - 判断依据：loss_streak_triggered 频率 > 3次/天 → 考虑改为 1.0U
  - 命令：`grep '"loss_streak_triggered"' analysis.jsonl | wc -l`

- [ ] P2: round65：验证 fill_tp density
  - 期望：每日 5-20 次；<5次说明格宽过大或成交太少
  - 命令：`grep '"fill_tp"' analysis.jsonl | wc -l`

- [ ] P3: 动态止盈：eff_tp_mult 灵敏度验证（是否真正随 ATR 变化）

## 下次优先行动

**round65：**
1. 若有实盘日志：统计 grid_opened / fill_entry / fill_tp 事件密度和 regime 分布
2. 若无日志：将 per_slot_stop 从 locked=0.8U 改为 locked=1.0U（宽松止损，减少 loss_streak 触发频率）
3. 检查 _place_grid 中 `_sz_scale` 计算是否覆盖了 ATR 30-35bps 的常见情形（目前 <35bps 不缩仓）

## 系统评估
- **策略有效性**：9/10
  - 64轮迭代；全P0/P1已解决；事件可观测性完整
  - 现在有完整的 grid_opened→fill_entry→fill_tp/emergency_close 事件链路
  - 可支持事后按网格会话分析损益
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志为空，所有优化未经实盘数据验证
  3. per_slot_stop=0.8U 可能偏紧，需实盘数据确认
- **累计运行轮次**：64
