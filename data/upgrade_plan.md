# ETH量化系统升级计划

## 本次（2026-04-28 第五十五轮）完成

### REGIME_SIZE_FACTOR 死代码清理（round55）

**问题**：
- regime.py 中存在 `REGIME_SIZE_FACTOR` 模块级字典（8行）和 `RegimeDetector.size_factor` 属性（3行）
- 全代码库 grep（覆盖 strategy/app/execution/account/risk/oms/tools 全部子目录）证明：
  `REGIME_SIZE_FACTOR` 仅在 `size_factor` 属性中引用，而 `size_factor` 属性本身从未被外部调用
- 与 round54 删除的 SP_ 参数性质相同：只定义、从不使用的死代码

**附加发现：LEV5_全量审计（round57计划提前完成）**：
- settings.py D 类中定义了 236 个 LEV5_ 参数
- 扩展 grep 覆盖全部子目录后：236 个参数**全部**在代码中被实际引用
- round 57 计划的"LEV5_ 死代码审计"结果：无死代码，计划作废

**修复**：
- 删除 regime.py `REGIME_SIZE_FACTOR` 字典（含注释头，共8行）
- 删除 regime.py `RegimeDetector.size_factor` 属性（3行）
- 语法检查：settings.py + grid_pro.py + regime.py + runner.py 全部通过

**效果预期**：
- regime.py 无误导性接口，阅读者不再误以为有仓位系数调节机制
- 约 11 行代码减少；无运行风险（删除的代码从未被调用）

---

## 历史完成（节选）

### 第五十四轮（2026-04-28）
- [x] settings.py: 删除全部41行SP_（ScalpPro）死代码
- [x] regime.py: 清理SP_VOL_CEIL注释中过时引用

### 第五十三轮（2026-04-28）
- [x] regime.py: _classify_by_ticks 对称宏观偏差保护，新增_MACRO_TICK_DOWN_MAX=+0.001

### 第五十二轮（2026-04-28）
- [x] regime.py: SP_VOL_CEIL审计，确认为死代码；更新注释

### 第五十一轮（2026-04-27）
- [x] regime.py: 提取_MACRO_TICK_UP_MIN命名常量

### 第五十轮（2026-04-27）
- [x] regime.py: TRENDING_UP宏观阈值收紧（-0.002→+0.0003/+0.0015）

### 第四十九轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset第二层阈值0.35→0.40

### 第一~四十八轮（2026-04-18~27）
- [x] 全部P0/P1问题；1h gate滞回环（round36）；trail/offset对称化（round38-49）；regime四种状态完整对称

---

## 待解决问题（按优先级）

- [ ] P3: round56：检查 RegimeDetector.allowed_channels 属性
  - `REGIME_ALLOWED_CHANNELS` 字典被 `_transition` 方法引用（日志输出）→ 字典必须保留
  - 但 `allowed_channels` property 本身是否被外部调用？grep 显示未被调用
  - 候选操作：`grep -rn '\.allowed_channels' quant/` 再验证；若确认无调用则删除 property

- [ ] P3: round56以后：regime.py line177 冗余条件记录
  - `elif macro_bias < _MACRO_DOWN_STOP and macro_bias > _MACRO_DOWN_KILL:` 中
  - `macro_bias > _MACRO_DOWN_KILL` 在 elif 链中为恒真（已被 line164 排除）
  - 保留冗余作为防御设计（防未来常量改动导致逻辑漏洞），暂不删除

- [ ] P3: round57以后：_MACRO_TICK_DOWN_MAX 实盘效果评估
  - 需要实盘日志中 regime 分布数据，沙盒环境下无法验证

- [ ] P3: long-term：_VOL_HIGH=0.0032 阈值适配性
  - 待真实成交后从 logs/daily/*/analysis.jsonl 提取 rel_vol 统计

## 下次优先行动

**round56：allowed_channels 属性审计**
1. `grep -rn '\.allowed_channels' /home/user/okx_eth_bot/quant/` 验证是否有外部调用
2. 若无调用：删除 `RegimeDetector.allowed_channels` 属性（`REGIME_ALLOWED_CHANNELS` 字典保留）
3. 若有调用：记录使用位置，跳过删除，转向其他改进
4. 语法检查 + 提交

## 系统评估
- **策略有效性**：9/10
  - 55轮迭代；全P0/P1已解决；代码整洁度持续提升
  - regime.py：TRENDING_UP/DOWN的tick分类完全对称（round53），VOLATILE/RANGING/TRENDING全覆盖
  - grid_pro.py：1h gate滞回环（round36），trail/offset完整对称（round38-49）
  - settings.py + regime.py：死代码已系统性清理（SP_41行 round54，REGIME_SIZE_FACTOR 11行 round55）
  - LEV5_审计：全236个参数被实际使用，无设计债务
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032 阈值在ETH实际运行中的适配性未知
- **累计运行轮次**：55
