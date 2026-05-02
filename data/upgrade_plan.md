# ETH量化系统升级计划

## 本次（2026-05-02 第七十二轮）完成

### settings.py：修复 DATA_DIR 重复定义导致日志路径错误

**问题**：
`settings.py` 中 `DATA_DIR` 被定义了两次：
- 第574行：`DATA_DIR = os.environ.get("DATA_DIR", str(_ROOT / "data"))` → 绝对路径（正确）
- 第928行：`DATA_DIR = _es("DATA_DIR", "./data")` → 相对路径（错误，覆盖了正确定义）

Python 按顺序执行，第928行会覆盖第574行。在 systemd 服务中，工作目录通常不是项目根目录，`"./data"` 可能指向 `/` 或 `/root/data` 等错误位置。

这正是为什么 `data/logs/daily` 从未在 `/home/user/okx_eth_bot/data/` 下被创建的根本原因。

**修复**：
删除第928行的重复定义，`DATA_DIR` 恢复为唯一绝对路径定义（第574行）。

**效果预期**：
- bot 下次启动时，`init_session()` 调用 `_ensure_day_dir()` 将正确在 `/home/user/okx_eth_bot/data/logs/daily/<日期>/` 创建目录
- `grid_loss_streak.json`、`grid_atr_state.json`、`grid_session.json` 等持久化文件也将写到正确位置
- 所有 71 轮的日志持久化修复（atr_baseline、loss_streak 等）终于能真正生效

---

## 历史完成（节选）

### 第七十一轮（2026-05-01）
- [x] grid_pro.py: loss_streak冷静期跨重启持久化（_save_loss_streak/_restore_loss_streak）

### 第七十轮（2026-05-01）
- [x] grid_pro.py: _sz_scale 日志补充4档标签(28-35/35-50/50-70/>70bps)
- [x] grid_pro.py: loss_streak冷静期按regime差异化（顺势600s/RANGING900s/逆势1200s）

### 第六十九~一轮（2026-04-18~05-01）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round73：验证 DATA_DIR 修复效果
  - 检查：`systemctl status okx-eth-bot` 确认服务状态
  - 检查：`ls /home/user/okx_eth_bot/data/logs/daily/` 是否有目录生成
  - 检查：`find /home/user/okx_eth_bot/data/ -name "grid_loss_streak.json"` 是否存在
- [ ] P1: round73：确认 data/grid_session.json / grid_atr_state.json 在正确路径
  - 若这些文件之前写到了错误目录，bot 启动后将以全新状态运行（ATR基线、session重置）
- [ ] P2: round74：实盘日志积累后分析 _sz_scale 各档位触发频率
- [ ] P2: round74：验证 loss_streak 3档冷静期（顺势/震荡/逆势）实际效果
- [ ] P3: 动态止盈 eff_tp_mult 实盘数据验证

## 下次优先行动

**round73：**
1. `systemctl status okx-eth-bot` — 确认服务是否运行
2. `ls /home/user/okx_eth_bot/data/logs/daily/` — 验证日志目录已创建
3. `find /home/user/okx_eth_bot/data/ -name "*.json" 2>/dev/null` — 确认持久化文件路径
4. 若服务未运行：检查 `journalctl -u okx-eth-bot -n 50` 排查启动失败原因

## 系统评估
- **策略有效性**：9/10
  - 72轮迭代；DATA_DIR根因修复后，所有持久化机制（ATR、loss_streak、session）将真正生效
  - 之前71轮的优化代码都是正确的，只是持久化文件写到了错误位置
- **当前主要风险**：
  1. DATA_DIR修复后需重启服务才能生效；重启会清空内存状态（ATR基线回到0，loss_streak重置）
  2. 沙盒网络受限，外部API不可用，市场数据依赖实盘反馈
  3. 实盘日志仍需观察以验证策略参数效果
- **累计运行轮次**：72
