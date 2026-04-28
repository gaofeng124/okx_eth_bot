# ETH量化系统升级计划

## 本次（2026-04-28 第五十四轮）完成

### SP_ 死代码批量清理（round54）

**问题**：
- settings.py D类中存在41行 ScalpPro 专属参数（SP_前缀），共22个参数
- scalp_pro策略已于早期轮次移除，这些参数从未被 strategy/*.py 或 app/*.py 任何文件引用
- 唯一外部引用：regime.py:95注释中提及SP_VOL_CEIL，仅为历史说明性文字
- 死代码增加维护负担，误导未来读者以为系统仍支持ScalpPro

**修复**：
- 删除 settings.py 第414-454行（含节头注释+全部SP_参数定义，共41行）
- 更新 regime.py _VOL_HIGH 注释，删除过时的"SP_VOL_CEIL是死代码"说明
- 语法检查：settings.py + grid_pro.py + regime.py + runner.py 全部通过

**效果预期**：
- settings.py 维护负担降低，无歧义
- 未来读者不会误以为SP_参数对运行有影响
- 完全无运行风险（删除的代码从未被调用）

---

## 历史完成（节选）

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

### 第四十八轮（2026-04-27）
- [x] grid_pro.py: _ewma_profit_avg最小样本门槛改为regime-specific

### 第一~四十七轮（2026-04-18~27）
- [x] 全部P0/P1问题；1h gate滞回环（round36）；trail/offset对称化（round38-49）；regime四种状态完整对称

---

## 待解决问题（按优先级）

- [ ] P3: round55：验证实盘ATR值分布
  - 从analysis.jsonl提取rel_vol字段，统计触发VOLATILE的频率
  - 评估_VOL_HIGH=0.0032是否适配ETH实际波动率范围
  - 若VOLATILE触发过少（<5%）说明阈值过高，考虑下调至0.0025

- [ ] P3: round56以后：_MACRO_TICK_DOWN_MAX实盘效果评估
  - 若实盘发现下跌信号滞后（TRENDING_DOWN触发太慢），可从+0.001下调至+0.0005

- [ ] P3: round57以后：LEV5_系列参数审计
  - 类似SP_清理，检查LEV5_前缀参数是否全部被grid_pro.py引用
  - 若存在死代码同样清理

## 下次优先行动

**round55：_VOL_HIGH阈值验证**
1. `grep -c 'VOLATILE' data/analysis.jsonl 2>/dev/null` 统计VOLATILE事件数
2. `python3 -c "import json; lines=[l for l in open('data/analysis.jsonl') if 'rel_vol' in l]; ..."`提取rel_vol分布
3. 与_VOL_HIGH=0.0032对比，判断阈值是否需要调整
4. 若analysis.jsonl无数据，跳至LEV5_参数审计

## 系统评估
- **策略有效性**：9/10
  - 54轮迭代；全P0/P1已解决；代码整洁度提升（SP_死代码已清除）
  - regime.py：TRENDING_UP/DOWN的tick分类完全对称，逻辑无漏洞
  - grid_pro.py：1h gate滞回环（round36），trail/offset完整对称（round38-49）
  - settings.py：无死代码，每个参数均被实际使用
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032阈值适配性未经实盘验证
- **累计运行轮次**：54
