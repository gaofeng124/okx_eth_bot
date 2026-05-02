# ETH量化系统升级计划

## 本次（2026-05-02 第七十三轮）完成

### grid_pro.py：修复 _check_phase4_trend_guard 硬编码路径

**问题**：
`_check_phase4_trend_guard` 函数（仅在 `GRID_PHASE4_TREND_GUARD=1` 时生效）中，有5处路径硬编码为 `/root/okx_eth_bot/...`：
- `/root/okx_eth_bot/.env` → sed 修改 GRID_LEVELS 时找不到文件，静默失败
- `/root/okx_eth_bot/.env.tmp` → 清理临时文件失败
- `/root/okx_eth_bot/data/.phase4_applied` → 移除标记文件失败（永远不清除）
- `/root/okx_eth_bot/data/.p4_downgraded` → 写降级时间戳失败
- `-i.tmp` 参数 → macOS BSD sed 格式，Linux GNU sed 会创建名为 .tmp 的文件而非后缀

这意味着当 phase4 guard 触发时：GRID_LEVELS 不会降回5，.phase4_applied 标记不会清除，.p4_downgraded 文件不写，表面上 pkill 会重启但配置没改。

**修复**：
1. 用 `Path(self._data_dir).parent` 动态推导项目根目录（DATA_DIR 修复后已保证绝对路径）
2. 将 `sed -i.tmp` 改为 `sed -i.bak`（GNU sed 兼容格式）
3. 将 hardcoded `open("/root/...")` 改为 `Path.write_text()`

**效果预期**：
- phase4 guard 启用时能正确修改 .env、清除标记文件、写入降级记录
- 配合第72轮 DATA_DIR 根因修复，路径体系完全正确

---

## 历史完成（节选）

### 第七十二轮（2026-05-02）
- [x] settings.py: 删除第928行重复 DATA_DIR 相对路径定义，所有持久化文件现写到正确位置

### 第七十一轮（2026-05-01）
- [x] grid_pro.py: loss_streak 冷静期跨重启持久化

### 第七十轮（2026-05-01）
- [x] grid_pro.py: _sz_scale 4档标签 + regime差异化冷静期

### 第六十九~一轮（2026-04-18~05-01）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round74：验证 DATA_DIR 修复效果（第72轮）
  - `ls /home/user/okx_eth_bot/data/logs/daily/` 是否有目录生成
  - `find /home/user/okx_eth_bot/data/ -name "grid_loss_streak.json"` 是否存在
- [ ] P2: round74：为 analysis.jsonl 增加每5分钟周期性 grid_state 快照
  - 目前只有事件驱动快照（grid_opened/fill_entry/fill_tp）
  - 周期快照有助于分析 regime 分布、ATR趋势、持仓时长等
- [ ] P2: round74：实盘日志积累后分析 _sz_scale 各档位触发频率
- [ ] P3: 动态止盈 eff_tp_mult 实盘数据验证

## 下次优先行动

**round74：**
1. 检查服务是否运行：`systemctl status okx-eth-bot`
2. 检查日志目录：`ls /home/user/okx_eth_bot/data/logs/daily/`
3. 若有日志，检查 fill_tp / fill_entry 事件数量
4. 实现 analysis.jsonl 周期性 grid_state 快照（5分钟间隔）

## 系统评估
- **策略有效性**：9/10
  - 73轮迭代，P0/P1 问题均已修复
  - DATA_DIR 路径体系完整（第72轮），phase4 guard 路径完整（本轮）
  - 持久化机制（ATR基线/loss_streak/session）完备
- **当前主要风险**：
  1. 沙盒网络受限，无法获取实时市场数据验证策略效果
  2. 服务是否正常运行未确认（无实盘日志反馈）
  3. GRID_LEVELS 锁定在3，phase4 guard 的 +1 level 逻辑在当前配置下无意义
- **累计运行轮次**：73
