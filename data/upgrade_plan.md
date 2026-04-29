# ETH量化系统升级计划

## 本次（2026-04-29 第六十一轮）完成

### grid_pro.py：_reset_grid_state 补加 bias 重置 + status_summary 暴露 grid_bias

**问题1：_reset_grid_state 未重置 _grid_bias（P2）**

场景：策略在 TRENDING Regime 运行（bias=0.5）→ 触发整体止损 → `_reset_grid_state` 被调用 → `_grid_bias` 残留 0.5 → 冷静期内若有代码路径绕过 `_grid_active` 守卫，补仓公式会以错误 bias 下单。

虽然现有 `_grid_active` 守卫理论上已阻止这种情况，但防御性重置更安全：
- 修复位置：L1686，在 `_grid_spacing = 0.0` 之后
- 修复内容：`self._grid_bias = 1.0`（RANGING 默认值）
- 效果：网格每次重启前 bias 明确归一，由 `_place_grid` 根据新 Regime 重新赋值

**问题2：status_summary 缺少 grid_bias 字段（P3）**

运维时无法从日志观察当前 Regime 对应的 bias 值，TRENDING/RANGING 切换不可见。
- 修复位置：`status_summary` 返回字典，`eff_tp_mult` 之后
- 修复内容：`"grid_bias": self._grid_bias`
- 效果：每次 status_summary 输出均含 bias（0.5=TRENDING，1.0=RANGING），方便排查

---

## 历史完成（节选）

### 第六十轮（2026-04-29）
- [x] grid_pro.py: 修复 _grid_bias 未保存导致 TRENDING 模式补仓/越叉检测价格偏差 P1 bug

### 第五十九轮（2026-04-29）
- [x] regime.py: _classify_by_ticks 删除未使用参数 trend_up/trend_down

### 第五十八轮（2026-04-29）
- [x] regime.py line160：删除恒真冗余条件

### 第五十七轮（2026-04-29）
- [x] grid_pro.py: 1h缓存API调用超时 5s→2s + 指数退避(60s→120s→240s→300s)

### 第五十六轮（2026-04-28）
- [x] regime.py: 删除 allowed_channels property（3行死代码）

### 第五十五轮（2026-04-28）
- [x] regime.py: 删除 REGIME_SIZE_FACTOR 字典 + size_factor property

### 第五十四轮（2026-04-28）
- [x] settings.py: 删除全部41行SP_（ScalpPro）死代码

### 第一~五十三轮（2026-04-18~28）
- [x] 全部P0/P1问题；WS重连指数退避；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate

---

## 待解决问题（按优先级）

- [ ] P2: round62：验证 1h-drop-gate / 1h-rise-gate 触发频率
  - 检查 analysis.jsonl 中 gate_blocked/1h_drop/1h_rise 事件数量
  - 期望：每天 5-20 次；若从未触发，可能逻辑有误

- [ ] P2: GRID_DRAWDOWN_FROM_PEAK_USDT locked=6.0 评估
  - 当前 locked 值 6.0（50U 本金 12%），偏高，建议降至 3.0（6%）
  - 但需实盘数据支撑才能改

- [ ] P3: 动态止盈：根据波动率调整每格利润（eff_tp_mult 已有，待验证灵敏度）

- [ ] P3: maker 手续费精度：确认 0.02% 而非 0.04%

## 下次优先行动

**round62：检查 1h gate 触发情况 + 考察 analysis.jsonl fill 事件密度**
1. `Read /home/user/okx_eth_bot/data/analysis.jsonl` 统计事件类型分布
2. 若 gate_blocked 事件为 0，grep grid_pro.py 的 1h_drop_gate 逻辑确认条件
3. 若 fill 事件密度异常低（<10/天），排查 tp 设置是否过远

## 系统评估
- **策略有效性**：9/10
  - 61轮迭代；全P0/P1已解决；bias 生命周期现已完整（init→place_grid→reset_grid_state）
  - status_summary 现在暴露 grid_bias，运维可视性提升
  - regime.py/settings.py 清理完毕；grid_pro.py 核心逻辑稳固
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. GRID_DRAWDOWN locked=6.0 偏高，大行情下保护不足
- **累计运行轮次**：61
