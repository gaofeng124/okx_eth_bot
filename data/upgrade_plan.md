# ETH量化系统升级计划

## 本次（2026-04-29 第五十八轮）完成

### regime.py line160：删除elif链中的恒真冗余条件（P3）

**问题**：line160 原代码：
```python
elif macro_bias < self._MACRO_DOWN_STOP and macro_bias > self._MACRO_DOWN_KILL:
```
`macro_bias > self._MACRO_DOWN_KILL` 在 elif 链中是**恒真条件**：
- line147: `elif macro_bias <= self._MACRO_DOWN_KILL` 失败 → 进入后续分支时已保证 `macro_bias > -0.003`
- line160 中再写 `and macro_bias > self._MACRO_DOWN_KILL(-0.003)` 永远不会过滤任何值

**修复**：
1. 删除恒真条件 `and macro_bias > self._MACRO_DOWN_KILL`
2. 保留 `elif macro_bias < self._MACRO_DOWN_STOP:` （有效条件，过滤 < -0.002）
3. 注释中明确标注：区间来源于 elif 链逻辑（_MACRO_DOWN_KILL < macro_bias < _MACRO_DOWN_STOP）

**效果**：
- 代码更清晰，消除"这个条件有什么用？"的困惑
- 语义完整保留：实际判断范围 (-0.003, -0.002) 不变
- 语法检查通过

---

## 历史完成（节选）

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

- [ ] P3: round59：_classify_by_ticks 方法完整审计
  - 验证 _MACRO_TICK_DOWN_MAX / _MACRO_TICK_UP_MIN 的 elif 链是否有类似冗余
  - 方法：Read regime.py lines 184-250，画出完整逻辑流

- [ ] P3: _MACRO_TICK_DOWN_MAX 实盘效果评估
  - 需要实盘日志中 regime 分布数据，沙盒环境下无法验证
  - 期望：TRENDING_DOWN 误判率降低

- [ ] P3: 验证1h-drop-gate + 1h-rise-gate触发频率
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 期望：每天5-20次（<5次=阈值1%过严；>50次考虑放宽到0.985/1.015）

- [ ] P3: 验证urlopen指数退避实际效果
  - 期望：实盘正常时fail_count始终为0；网络抖动时fail_count>0但<4

## 下次优先行动

**round59：_classify_by_ticks 方法完整逻辑审计**
1. `Read /home/user/okx_eth_bot/quant/strategy/regime.py lines 184-260`
2. 检查 _MACRO_TICK_UP_MIN / _MACRO_TICK_DOWN_MAX 条件的 elif 链是否有冗余
3. 若有冗余：删除并更新注释；若无冗余：确认并记录，转向下一优化点

## 系统评估
- **策略有效性**：9/10
  - 58轮迭代；全P0/P1已解决；代码整洁度与运行稳定性持续提升
  - regime.py：elif链冗余条件清理，代码逻辑可读性提升
  - 历史：SP_死代码41行、REGIME_11行、allowed_channels 3行 系统性清理完毕
  - grid_pro.py：1h gate滞回环、trail/offset完整对称、urlopen指数退避
  - LEV5_审计：全236个参数被实际使用，无设计债务
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032 阈值在ETH实际运行中的适配性未知
- **累计运行轮次**：58
