# ETH量化系统升级计划

## 本次（2026-04-28 第五十六轮）完成

### allowed_channels 属性死代码删除（round56）

**问题**：
- regime.py `RegimeDetector.allowed_channels` property（3行：@property装饰器 + def + return）
- 全代码库 grep `\.allowed_channels` 结果：**零匹配**——property 从未被外部调用
- `REGIME_ALLOWED_CHANNELS` 字典本身在 `_transition` 方法（line 219）直接引用，须保留
- 与 round55 删除的 `size_factor` 性质相同：只定义、从不使用的死代码 wrapper

**修复**：
- 删除 regime.py `RegimeDetector.allowed_channels` property（3行）
- `REGIME_ALLOWED_CHANNELS` 模块级字典（lines 54-61）**保留**
- 语法检查：settings.py + grid_pro.py + regime.py + runner.py 全部通过

**效果预期**：
- RegimeDetector 公开接口更精简（只剩 `current` property + `update` / `reset` 方法）
- 无歧义：不再暴露"可查询 allowed_channels"的接口，使用者必须直接读 `REGIME_ALLOWED_CHANNELS` 字典
- 约 3 行代码减少；无运行风险（删除的代码从未被调用）

---

## 历史完成（节选）

### 第五十五轮（2026-04-28）
- [x] regime.py: 删除 REGIME_SIZE_FACTOR 字典（8行）+ size_factor property（3行）
- [x] 完成 LEV5_ 全量审计（236个参数全部有效，无死代码）

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

- [ ] P3: round57：regime.py line177 冗余条件评估
  - `elif macro_bias < _MACRO_DOWN_STOP and macro_bias > _MACRO_DOWN_KILL:` 中
  - `macro_bias > _MACRO_DOWN_KILL` 在 elif 链中为恒真（已被上方 elif 排除更大值）
  - **候选操作**：阅读 line164-185 的完整 elif 链，确认恒真性后，评估：
    - 选项A：删除恒真条件 `and macro_bias > _MACRO_DOWN_KILL`（更简洁）
    - 选项B：保留冗余作为防御设计（防未来常量改动导致逻辑漏洞）
  - 需要读取 regime.py lines 155-190 做完整判断

- [ ] P3: round58以后：_MACRO_TICK_DOWN_MAX 实盘效果评估
  - 需要实盘日志中 regime 分布数据，沙盒环境下无法验证

- [ ] P3: long-term：_VOL_HIGH=0.0032 阈值适配性
  - 待真实成交后从 logs/daily/*/analysis.jsonl 提取 rel_vol 统计

## 下次优先行动

**round57：regime.py line177 冗余条件精确审计**
1. `Read /home/user/okx_eth_bot/quant/strategy/regime.py lines 155-190`
2. 画出完整 elif 链，验证 `macro_bias > _MACRO_DOWN_KILL` 在 elif 上下文中是否恒真
3. 若恒真：
   - 选项A（简洁）：删除冗余条件
   - 选项B（防御）：添加注释说明为不变量
   - 根据周边代码的防御性设计风格决定
4. 语法检查 + 提交

## 系统评估
- **策略有效性**：9/10
  - 56轮迭代；全P0/P1已解决；代码整洁度持续提升
  - regime.py：公开接口精简（current + update + reset），死代码已系统性清理
  - grid_pro.py：1h gate滞回环（round36），trail/offset完整对称（round38-49）
  - settings.py + regime.py：死代码系统性清理（SP_41行 round54，REGIME_SIZE_FACTOR 11行 round55，allowed_channels 3行 round56）
  - LEV5_审计：全236个参数被实际使用，无设计债务
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032 阈值在ETH实际运行中的适配性未知
- **累计运行轮次**：56
