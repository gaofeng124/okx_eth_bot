# ETH量化系统升级计划

## 本次（2026-04-29 第五十七轮）完成

### grid_pro.py：1h缓存API调用阻塞修复（P3）

**问题**：`_price_1h_cache`刷新使用`urlopen(timeout=5)`，API不可达时：
- 每60秒阻塞异步事件循环长达5秒（策略完全冻结5秒）
- P4守卫每10min也调一次，同样5秒阻塞
- 固定60s重试间隔在持续故障时无意义（反复阻塞）

**修复**：
1. `_price_1h_cache`刷新：timeout=5 → timeout=2（最大阻塞2s）
2. 成功时重置`_price_1h_fail_count=0`
3. 失败时指数退避：fail=1→60s，fail=2→120s，fail=3→240s，fail≥4→300s（5min上限）
4. P4守卫：timeout=5 → timeout=2

**效果**：
- API故障时最大阻塞从5s降至2s（节省60%阻塞时间）
- 持续故障时重试间隔从60s指数增长到5min，大幅减少无效阻塞
- 实盘API正常时：首次成功后重置计数，5min正常刷新不受影响

---

## 历史完成（节选）

### 第五十六轮（2026-04-28）
- [x] regime.py: 删除RegimeDetector.allowed_channels property（3行死代码）

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

### 第三十六轮（2026-04-24）
- [x] grid_pro.py: 1h方向gate添加滞回环（entry=0.99/1.01，exit=0.995/1.005），防单阈值震荡

### 第一~三十五轮（2026-04-18~24）
- [x] 全部P0/P1问题；WS重连指数退避；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate

---

## 待解决问题（按优先级）

- [ ] P3: round58：regime.py line177 冗余条件评估
  - `elif macro_bias < _MACRO_DOWN_STOP and macro_bias > _MACRO_DOWN_KILL:` 中
  - `macro_bias > _MACRO_DOWN_KILL` 在 elif 链中为恒真（已被上方 elif 排除更大值）
  - **候选操作**：读取 line164-185 完整 elif 链，确认恒真性后评估删除或加注释

- [ ] P3: round59以后：_MACRO_TICK_DOWN_MAX 实盘效果评估
  - 需要实盘日志中 regime 分布数据，沙盒环境下无法验证

- [ ] P3: 验证1h-drop-gate + 1h-rise-gate触发频率
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 期望：每天5-20次（<5次=阈值1%过严；>50次考虑放宽到0.985/1.015）

- [ ] P3: 验证urlopen指数退避实际效果
  - 期望：实盘正常时fail_count始终为0；网络抖动时fail_count>0但<4

## 下次优先行动

**round58：regime.py line177 冗余条件精确审计**
1. `Read /home/user/okx_eth_bot/quant/strategy/regime.py lines 155-190`
2. 画出完整 elif 链，验证 `macro_bias > _MACRO_DOWN_KILL` 是否恒真
3. 若恒真：选项A删除（更简洁）或选项B加注释（防御性设计）
4. 语法检查 + 提交

## 系统评估
- **策略有效性**：9/10
  - 57轮迭代；全P0/P1已解决；代码整洁度与运行稳定性持续提升
  - regime.py：公开接口精简，死代码系统性清理（SP_41行+REGIME_11行+allowed_channels 3行）
  - grid_pro.py：1h gate滞回环、trail/offset完整对称、urlopen指数退避
  - LEV5_审计：全236个参数被实际使用，无设计债务
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（优化均未经实盘数据验证）
  3. _VOL_HIGH=0.0032 阈值在ETH实际运行中的适配性未知
- **累计运行轮次**：57
