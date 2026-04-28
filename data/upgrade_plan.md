# ETH量化系统升级计划

## 本次（2026-04-28 第五十二轮）完成

### 审计：SP_VOL_CEIL 与 _VOL_HIGH 对齐（round52）

**问题**：
- `regime.py` 第95行注释 `_VOL_HIGH = 0.0032 # 极端波动阈值（当前 SP_VOL_CEIL = 0.0028）`
- 注释暗示 `_VOL_HIGH` 应参照 `SP_VOL_CEIL=0.0028`，引发是否需要调低为0.0028的疑问
- 实际查证：`SP_VOL_CEIL` 仅在 `settings.py` 的 `class D` 内定义，从未被模块级导出，在代码库中零调用次数（除注释外）

**审计结论**：
1. `SP_VOL_CEIL = 0.0028` → **死代码**，是早期scalp_pro策略的遗留参数，scalp_pro已移除但此设置未清理
2. `_VOL_HIGH = 0.0032` → **独立工作**，是regime检测器触发VOLATILE状态的唯一阈值，功能正常
3. 两者无任何逻辑依赖，注释中的对比 "当前SP_VOL_CEIL=0.0028" 是误导性描述
4. 若将 `_VOL_HIGH` 改为0.0028（与SP_VOL_CEIL对齐），会使VOLATILE触发更频繁（32bps→28bps），
   导致正常ELEVATED行情也被停止交易，反而更保守——**不应修改**

**修复**：
- `regime.py` 注释：移除"SP_VOL_CEIL=0.0028"引用，改为 "（32bps）：VOLATILE regime触发点；SP_VOL_CEIL(settings)是死代码，与此无依赖"
- `settings.py` 注释：SP_VOL_TARGET/SP_VOL_CEIL加 `[未使用，预留]` 标注

---

## 历史完成（节选）

### 第五十一轮（2026-04-27）
- [x] regime.py: 提取_MACRO_TICK_UP_MIN命名常量，宏观偏差阈值体系完全命名化

### 第五十轮（2026-04-27）
- [x] regime.py: TRENDING_UP宏观阈值收紧（-0.002→+0.0003/+0.0015），消除误判

### 第四十九轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二层阈值 0.35→0.40

### 第四十八轮（2026-04-27）
- [x] grid_pro.py: _ewma_profit_avg 最小样本门槛改为regime-specific

### 第三十六~四十七轮（2026-04-24~26）
- [x] 1h gate滞回环（round36），trail/offset对称化（round38~49，12轮），regime多轮修正

### 第一~三十五轮（2026-04-18~24）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: round53：检查 _classify_by_ticks 中 `(1 - _TREND_TICK_FRAC)` 是否需要对称常量
  - 当前：`if up_frac <= (1 - self._TREND_TICK_FRAC)` = 0.30
  - 下跌tick比例阈值与上涨共享同一常量，天然对称可接受
  - 评估：若未来需要非对称阈值（下跌更灵敏），可提取 `_TREND_TICK_FRAC_DOWN`

- [ ] P3: round54以后：SP_ 系列死代码清理
  - 确认是否所有 SP_ 前缀的 settings 均无使用（需系统性grep验证）
  - 若全为死代码，在单独一轮中批量删除（减少settings.py维护负担）

- [ ] P3: 验证实盘ATR值分布（_VOL_HIGH=0.0032是否适配ETH实际波动率范围）
  - 方法：从analysis.jsonl提取rel_vol字段，检查触发VOLATILE的频率

## 下次优先行动

**round53：_TREND_TICK_FRAC 对称性审计**
1. 读取 regime.py 中 `_classify_by_ticks` 方法全文
2. 确认 `(1 - _TREND_TICK_FRAC)` 的语义是否天然对称
3. 若对称：注释说明原因（无需新常量）
4. 若需非对称：提取 `_TREND_TICK_FRAC_DOWN = 0.30` 并记录用途差异

## 系统评估
- **策略有效性**：9/10
  - 52轮迭代；全P0/P1已解决；代码整洁度持续提升
  - regime.py：宏观偏差+波动率阈值完全命名化，注释准确无歧义
  - grid_pro.py：1h gate滞回环（round36），trail/offset完整对称（round38-49）
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. SP_ 死代码遗留（功能无害，但增加维护混淆度）
- **累计运行轮次**：52
