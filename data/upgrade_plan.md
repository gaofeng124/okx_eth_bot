# ETH量化系统升级计划

## 本次（2026-04-29 第六十轮）完成

### grid_pro.py：修复 _grid_bias 遗漏 bug（P1）

**问题根因**：
`_place_grid` 内根据 Regime 计算 `bias`（RANGING=1.0，TRENDING=0.5），但只在局部变量使用，从未保存为实例变量。两处"补仓/检测"代码复用了 `_grid_spacing` 却遗漏了 `bias`：

- `_maybe_recenter` L1716：穿叉检测 `calc_px` 无 bias → TRENDING 模式下检测价格偏远2×，更难触发保护
- 主循环补仓 L2799：补仓价格无 bias → TRENDING 模式下补仓单下在2×spacing处而非1×（预期的0.5×2=1×）

**影响量化**：
- GRID_MIN_SPACING=0.002，bias=0.5，偏差 = 0.5 × spacing = 0.001（10bps）
- 以 ETH=$1800 为例：slot 0 补仓错误地放在 $1.80 更低处（long TRENDING）

**修复（4行）**：
1. `__init__` L415: 新增 `self._grid_bias: float = 1.0`
2. `_place_grid` L1256: 保存 `self._grid_bias = bias`
3. `_maybe_recenter` L1716: `calc_px` 公式补充 `* self._grid_bias`
4. 补仓循环 L2799: `calc_px` 公式补充 `* self._grid_bias`

**效果**：
- TRENDING 模式下补仓单现在下在正确价格（与初始下单一致）
- 穿叉保护检测基于实际挂单价格（TRENDING 时更接近 center，更敏感）
- RANGING 模式 bias=1.0 无变化（向后兼容）

---

## 历史完成（节选）

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

- [ ] P2: round61：_reset_grid_state 后 _grid_bias 重置
  - 当 _reset_grid_state 调用后 grid 重新开格，bias 会在 _place_grid 中重新赋值
  - 但若 _grid_active=False 期间补仓代码意外触发，bias 仍是上轮遗留值
  - 检查 _grid_active 是否有效阻止这种情况（看守条件已有）

- [ ] P3: status_summary 暴露 grid_bias 字段
  - 当前 status_summary 没有 grid_bias 字段，运维时看不到当前 bias 状态
  - 修复：在 status_summary 返回字典中添加 "grid_bias": self._grid_bias

- [ ] P3: 验证 1h-drop-gate / 1h-rise-gate 触发频率
  - 期望：每天 5-20 次

- [ ] P2: GRID_DRAWDOWN_FROM_PEAK_USDT=2.0 评估
  - 50U 本金约 4%，建议值 3.0（6%）；实盘数据验证后考虑提升

## 下次优先行动

**round61：status_summary 添加 grid_bias 字段 + 验证 _reset_grid_state 安全性**
1. `Read /home/user/okx_eth_bot/quant/strategy/grid_pro.py` 找到 status_summary 函数
2. 在返回字典中添加 `"grid_bias": self._grid_bias`
3. 检查 _reset_grid_state 是否需要将 _grid_bias 重置为 1.0（保守默认）

## 系统评估
- **策略有效性**：9/10
  - 60轮迭代；全P0/P1已解决；本轮修复 TRENDING 模式下系统性价格偏差 P1 bug
  - grid_pro.py：bias 一致性修复（初始下单 vs 补仓 vs 穿叉检测完全对齐）
  - regime.py：3轮清理完毕；settings.py：SP_死代码清理完毕
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. TRENDING 模式 bias 修复改变了补仓行为，实盘首日需观察 fill 频率
- **累计运行轮次**：60
