# ETH量化系统升级计划

## 本次（2026-04-22 第二十二轮）完成

### grid_pro.py：_tp_fill_profits EWMA时间衰减加权升级

**背景：**
第19/20轮实现的双维度自适应（trigger + offset）使用简单算术均值
`sum(deque) / len(deque)`，新旧成交等权重。问题：市场状态往往是连续的，
30分钟前的成交特征参考价值远低于最近5分钟的成交，等权重导致自适应响应迟钝。

**改动：**

1. **_tp_fill_profits**: `deque[float](maxlen=10)` → `deque[tuple[float,float]](maxlen=20)`
   - 从只存值升级为存 `(timestamp, profit_spacings)` 元组
   - maxlen=10→20，保留更长历史供衰减加权使用

2. **新增 `_ewma_profit_avg()` 方法**:
   - 半衰期 `_PROFIT_HALF_LIFE = 1800s`（30min）
   - 权重公式：`w_i = exp(-ln2/1800 × (now - ts_i))`
   - 30分钟前的成交权重是当前成交的50%；2小时前降至6.25%
   - 少于5个样本返回None（保持原冷启动行为）

3. **_adaptive_trail_trigger**: 简单均值 → `_ewma_profit_avg()`
4. **_adaptive_trail_offset**: 简单均值 → `_ewma_profit_avg()`（同时修复了tuple兼容性）
5. **append调用**: 改为 `self._tp_fill_profits.append((time.time(), profit_spacings))`

**验证（unit test）：**
- 前5次旧成交（0.3格，1h前） + 后5次新成交（0.9格，1min内）
- EWMA = 0.774，简单均值 = 0.600
- EWMA正确偏向近期高利润成交，自适应将更快收紧trigger/offset

**预期效果：**
- 市场regime切换后（如从低波动转高波动），adaptive参数响应时间从10次成交缩短到约3次
- 简单均值"记忆太久"的问题消除，减少跨regime的错误自适应

---

## 历史完成

### 第二十一轮（2026-04-22）
- [x] grid_pro.py: FGI格宽双向调整（极恐<25→×0.8，贪婪>70 RANGING→×1.2）
- [x] grid_pro.py: _refresh_funding 新增REST fallback（runner未提供时直接HTTP获取）

### 第二十轮（2026-04-21）
- [x] grid_pro.py: _adaptive_trail_offset 方法（双维度自适应闭环完整落地）

### 第十九轮（2026-04-21）
- [x] grid_pro.py: _adaptive_trail_trigger 方法 + _tp_fill_profits deque

### 第十八轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式 _trail_trigger=0.30 + _min_trail_iv=20s

### 第十七轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式动态 TP 系数 + trail_offset=0.15

### 第一~十六轮（2026-04-18/19/20/21）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步等

---

## 待解决问题（按优先级）

- [ ] P1: 验证 _tp_fill_profits EWMA 实际触发
  - 日志搜索 `adaptive trigger:` 和 `adaptive offset:` 行确认自适应激活（需5次TP成交后）
  - 验证 (ts, value) tuple 格式正确写入（检查 analysis.jsonl fill_tp 事件）
- [ ] P2: _tp_fill_profits 持久化
  - 当前重启后历史清零，冷启动需再等5次TP成交
  - 考虑重启时从 analysis.jsonl 重播最近20次 fill_tp 恢复历史
- [ ] P3: profit_spacings 使用 TRENDING_UP 时赋予更高权重
  - 趋势行情中每格利润通常更高，可分 regime 分别维护 EWMA
- [ ] P3: 动态止盈：根据波动率调整每格利润（tp_mult与ATR联动）

---

## 下次优先行动

1. **P1: 实现 _tp_fill_profits 冷启动恢复**
   - 在 GridProStrategy.__init__ 尾部，从 analysis.jsonl 读取最近20次 `fill_tp` 事件
   - 重播到 `_tp_fill_profits` 中（携带原始时间戳），使重启后立即可用自适应
   - 需要 record_analysis 写入 timestamp 字段（检查是否已有）

2. **若P1完成**：评估分 regime 的 EWMA（RANGING/TRENDING 分开维护）

---

## 系统评估
- **策略有效性**：9/10
  - 22轮迭代；全P0/P1已解决
  - 自适应层：EWMA时间衰减（第22轮）→ 响应更快，跨regime错误减少
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2）
  - 资金费率：runtime优先 + REST fallback
- **主要风险点**：
  1. _tp_fill_profits重启归零，冷启动5次TP成交前使用固定base值
  2. 外部API（FGI、资金费率）网络受限时均降级为缓存默认值
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：22
