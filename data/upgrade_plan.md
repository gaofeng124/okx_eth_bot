# ETH量化系统升级计划

## 本次（2026-04-29 第五十九轮）完成

### regime.py：删除 _classify_by_ticks 的两个未使用参数（P3）

**问题**：`_classify_by_ticks(self, macro_bias, trend_up, trend_down)` 中：
- `trend_up: bool` 在函数体中从未被引用
- `trend_down: bool` 在函数体中从未被引用
- 函数体实际只使用 `macro_bias` 和内部的 `up_frac`（从 tick 价格序列计算）

**修复**：
1. 函数签名简化为 `_classify_by_ticks(self, macro_bias: float) -> Regime`
2. 调用点 line172 同步简化为 `self._classify_by_ticks(macro_bias)`

**效果**：
- 消除"为什么传入 trend_up/trend_down 但不用？"的困惑
- 函数依赖关系更清晰（只依赖 macro_bias + tick 价格窗口）
- 语法检查通过

---

## 历史完成（节选）

### 第五十八轮（2026-04-29）
- [x] regime.py line160：删除恒真冗余条件 `macro_bias > self._MACRO_DOWN_KILL`

### 第五十七轮（2026-04-29）
- [x] grid_pro.py: 1h缓存API调用超时 5s→2s + 指数退避(60s→120s→240s→300s)

### 第五十六轮（2026-04-28）
- [x] regime.py: 删除RegimeDetector.allowed_channels property（3行死代码）

### 第五十五轮（2026-04-28）
- [x] regime.py: 删除 REGIME_SIZE_FACTOR 字典（8行）+ size_factor property（3行）

### 第五十四轮（2026-04-28）
- [x] settings.py: 删除全部41行SP_（ScalpPro）死代码

### 第五十三轮（2026-04-28）
- [x] regime.py: _classify_by_ticks 对称宏观偏差保护，新增_MACRO_TICK_DOWN_MAX=+0.001

### 第一~五十二轮（2026-04-18~28）
- [x] 全部P0/P1问题；WS重连指数退避；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate

---

## 待解决问题（按优先级）

- [ ] P3: round60：审计 grid_pro.py _open_grid_orders 函数
  - 验证 GRID_LEVELS 参数与实际挂单数量完全对应
  - 检查档位计算是否有 off-by-one 错误

- [ ] P3: 验证1h-drop-gate + 1h-rise-gate触发频率
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 期望：每天5-20次（<5次=阈值1%过严；>50次考虑放宽到0.985/1.015）

- [ ] P3: 验证urlopen指数退避实际效果
  - 期望：实盘正常时fail_count始终为0；网络抖动时fail_count>0但<4

- [ ] P2: GRID_DRAWDOWN_FROM_PEAK_USDT=2.0 评估
  - 当前50U本金约4%，建议值3.0（6%）；实盘数据验证后考虑提升

## 下次优先行动

**round60：grid_pro.py _open_grid_orders 档位逻辑审计**
1. `Read /home/user/okx_eth_bot/quant/strategy/grid_pro.py` 找到 _open_grid_orders 函数
2. 检查 GRID_LEVELS 与实际下单数量的对应关系
3. 检查价格档位计算是否有边界错误

## 系统评估
- **策略有效性**：9/10
  - 59轮迭代；全P0/P1已解决；代码整洁度与运行稳定性持续提升
  - regime.py：3个清理轮次（allowed_channels、REGIME_SIZE_FACTOR、恒真条件、死参数）
  - settings.py：SP_死代码41行系统性清理完毕
  - grid_pro.py：1h gate滞回环、trail/offset完整对称、urlopen指数退避
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032 阈值在ETH实际运行中的适配性未知
- **累计运行轮次**：59
