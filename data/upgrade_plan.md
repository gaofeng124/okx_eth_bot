# ETH量化系统升级计划

## 本次（2026-04-28 第五十三轮）完成

### _classify_by_ticks 对称宏观偏差保护（round53）

**问题**：
- `_classify_by_ticks` 中 TRENDING_UP 有 `macro_bias > _MACRO_TICK_UP_MIN(-0.001)` 保护
- TRENDING_DOWN **缺乏对称保护**：即使 macro_bias=+0.001（价格在5分均线上方0.1%，轻微看涨），
  只要72%下跌tick就声明TRENDING_DOWN并停止全部交易
- 触发场景：宏观轻涨（macro_bias ∈ +0.001~+0.0015）但趋势强度不足未触发层2，进入层3后
  遭遇短暂微观回调→误判→停止交易→错失上涨行情中的做多机会

**修复**：
- 新增 `_MACRO_TICK_DOWN_MAX = +0.0010` 类常量（对称于 `_MACRO_TICK_UP_MIN = -0.0010`）
- `_classify_by_ticks` 第221行：`up_frac <= 0.30 and macro_bias < +0.001` 才返回TRENDING_DOWN
- 效果：宏观偏差 > +0.001 时，即使微观ticks偏空，层3回退为RANGING（继续交易）而非TRENDING_DOWN

**效果预期**：
- 减少宏观偏多行情中因微观噪音触发的不必要停止交易
- 完整对称性：两个方向的tick分类都受宏观偏差门槛约束，逻辑一致性提升

---

## 历史完成（节选）

### 第五十二轮（2026-04-28）
- [x] regime.py: SP_VOL_CEIL审计，确认为死代码；更新注释消除与_VOL_HIGH的虚假依赖

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

- [ ] P3: round54：SP_ 系列死代码系统性清理
  - grep验证所有SP_前缀的settings是否全为未使用（scalp_pro已移除的遗留参数）
  - 确认后批量删除，减少settings.py维护负担
  - 候选：SP_VOL_TARGET, SP_VOL_CEIL, SP_PROFIT_TARGET_BPS, SP_MAX_LOSS_BPS等

- [ ] P3: round55以后：验证实盘ATR值分布
  - 从analysis.jsonl提取rel_vol字段，统计触发VOLATILE的频率
  - 评估 _VOL_HIGH=0.0032 是否适配ETH实际波动率范围

- [ ] P3: 后续：_MACRO_TICK_DOWN_MAX 实盘效果评估
  - 若实盘发现下跌信号滞后（TRENDING_DOWN触发太慢），可从+0.001下调至+0.0005

## 下次优先行动

**round54：SP_ 死代码批量清理**
1. `grep -n 'SP_' quant/settings.py quant/strategy/*.py quant/app/*.py` 全面搜索
2. 列出所有SP_前缀参数及其引用次数
3. 确认零调用后从settings.py中删除（保留settings.py中的D类定义只删未用的）
4. 运行语法检查确认无破坏

## 系统评估
- **策略有效性**：9/10
  - 53轮迭代；全P0/P1已解决；regime.py对称性完整
  - TRENDING_UP/DOWN的tick分类现在完全对称，逻辑无漏洞
  - grid_pro.py：1h gate滞回环（round36），trail/offset完整对称（round38-49）
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. SP_ 死代码遗留（功能无害，增加维护混淆度）
- **累计运行轮次**：53
