#!/usr/bin/env python3
"""
量化程序自动修复脚本
功能：自动检测并修复网格策略不激活问题
"""

import os
import sys
import time
import subprocess
from pathlib import Path

# 配置
PROJECT_DIR = "/Users/gaofeng/Documents/okx_eth_bot"
STRATEGY_FILE = "quant/strategy/grid_pro.py"
BACKUP_FILE = "quant/strategy/grid_pro.py.backup"
LOG_FILE = "restart.log"

def check_program_running():
    """检查程序是否运行"""
    try:
        result = subprocess.run(["pgrep", "-f", "okx_eth_bot"], 
                              capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        print(f"检查程序运行状态失败: {e}")
        return False

def stop_program():
    """停止程序"""
    print("停止量化程序...")
    subprocess.run(["pkill", "-f", "okx_eth_bot"])
    time.sleep(2)  # 等待程序停止
    
    # 确认已停止
    if check_program_running():
        print("警告：程序仍在运行，强制停止")
        subprocess.run(["pkill", "-9", "-f", "okx_eth_bot"])
        time.sleep(1)

def backup_strategy_file():
    """备份策略文件"""
    src = os.path.join(PROJECT_DIR, STRATEGY_FILE)
    dst = os.path.join(PROJECT_DIR, BACKUP_FILE)
    
    if os.path.exists(src):
        print(f"备份策略文件: {src} -> {dst}")
        subprocess.run(["cp", src, dst])
        return True
    else:
        print(f"错误：策略文件不存在: {src}")
        return False

def apply_fixes():
    """应用修复补丁"""
    print("应用修复补丁...")
    
    # 修复内容
    fixes = [
        # 修复点1：warmup详细日志
        {
            'old': '        if self._warmup_ticks < self._warmup_need:\n            return None',
            'new': '''        if self._warmup_ticks < self._warmup_need:
            log.info(f"[grid] warmup进度: {self._warmup_ticks}/{self._warmup_need}")
            if self._warmup_ticks >= 100:  # 如果超过100个tick仍卡住
                log.warning(f"[grid] warmup可能卡住，强制跳过")
                self._warmup_ticks = self._warmup_need
            return None'''
        },
        
        # 修复点2：Regime检测日志
        {
            'old': '        regime = self._regime.update(feat, now)',
            'new': '''        regime = self._regime.update(feat, now)
        log.info(f"[grid] Regime: {regime.value}, 趋势强度: {feat['trend_strength']:.4f}")'''
        },
        
        # 修复点3：市场条件日志
        {
            'old': '        market_ok, market_reason = self._market_ok_to_enter(runtime, mid, now, bid=bid, ask=ask)',
            'new': '''        market_ok, market_reason = self._market_ok_to_enter(runtime, mid, now, bid=bid, ask=ask)
        log.info(f"[grid] 市场条件: {market_ok}, 原因: {market_reason}")'''
        },
        
        # 修复点4：网格激活检查
        {
            'old': '''        # ── 11. 激活网格 ───────────────────────────────────────────────────
        if not self._grid_active and market_ok and regime in (Regime.RANGING, Regime.TRENDING_UP):
            self._profit_protect_logged = False
            self._place_grid(mid, regime, now)
            return None''',
            'new': '''        # ── 11. 激活网格 ───────────────────────────────────────────────────
        if not self._grid_active:
            log.info(f"[grid] 网格未激活，原因检查:")
            log.info(f"  - market_ok: {market_ok}")
            log.info(f"  - regime: {regime.value}")
            log.info(f"  - 允许的regime: {regime in (Regime.RANGING, Regime.TRENDING_UP)}")
            log.info(f"  - 宏观偏空: {macro_bearish}")
        
        if not self._grid_active and market_ok and regime in (Regime.RANGING, Regime.TRENDING_UP):
            self._profit_protect_logged = False
            self._place_grid(mid, regime, now)
            return None'''
        }
    ]
    
    # 读取文件
    file_path = os.path.join(PROJECT_DIR, STRATEGY_FILE)
    with open(file_path, 'r') as f:
        content = f.read()
    
    # 应用修复
    original_content = content
    for fix in fixes:
        if fix['old'] in content:
            content = content.replace(fix['old'], fix['new'])
            print(f"应用修复: {fix['old'][:50]}...")
        else:
            print(f"警告：未找到修复点: {fix['old'][:50]}...")
    
    # 写入文件
    if content != original_content:
        with open(file_path, 'w') as f:
            f.write(content)
        print("修复已应用")
        return True
    else:
        print("未应用任何修复")
        return False

def restart_program():
    """重启程序"""
    print("重启量化程序...")
    
    log_path = os.path.join(PROJECT_DIR, LOG_FILE)
    
    # 切换到项目目录并启动程序
    cmd = f"cd {PROJECT_DIR} && python main.py > {log_path} 2>&1 &"
    
    print(f"执行命令: {cmd}")
    subprocess.run(cmd, shell=True, executable="/bin/bash")
    
    # 等待程序启动
    time.sleep(3)
    
    # 检查是否启动成功
    if check_program_running():
        print("程序启动成功")
        return True
    else:
        print("程序启动失败")
        return False

def monitor_logs():
    """监控日志"""
    print("\n开始监控日志（10秒）...")
    log_path = os.path.join(PROJECT_DIR, LOG_FILE)
    
    if os.path.exists(log_path):
        try:
            # 显示最后20行日志
            subprocess.run(["tail", "-20", log_path])
            
            # 持续监控关键信息
            print("\n监控关键日志（按Ctrl+C停止）...")
            cmd = f"tail -f {log_path} | grep -E '(warmup|Regime|网格|market_ok|intent)'"
            subprocess.run(cmd, shell=True, timeout=10)
        except subprocess.TimeoutExpired:
            print("\n监控结束")
        except KeyboardInterrupt:
            print("\n用户中断监控")
    else:
        print(f"日志文件不存在: {log_path}")

def main():
    """主函数"""
    print("=" * 60)
    print("量化程序自动修复脚本")
    print("=" * 60)
    
    # 检查项目目录
    if not os.path.exists(PROJECT_DIR):
        print(f"错误：项目目录不存在: {PROJECT_DIR}")
        return 1
    
    # 1. 检查程序状态
    if check_program_running():
        print("程序正在运行，准备停止...")
        stop_program()
    else:
        print("程序未运行")
    
    # 2. 备份文件
    if not backup_strategy_file():
        return 1
    
    # 3. 应用修复
    if not apply_fixes():
        print("继续执行重启...")
    
    # 4. 重启程序
    if not restart_program():
        return 1
    
    # 5. 监控日志
    monitor_logs()
    
    print("\n修复完成！")
    print(f"请检查日志文件: {os.path.join(PROJECT_DIR, LOG_FILE)}")
    print(f"原文件已备份到: {os.path.join(PROJECT_DIR, BACKUP_FILE)}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())