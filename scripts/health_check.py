#!/usr/bin/env python3
"""
健康检查脚本
检查多币种网格机器人的运行状态
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def check_status_summary():
    """
    检查状态汇总日志
    """
    status_file = 'log/status_summary.log'
    
    if not os.path.exists(status_file):
        logger.error("状态汇总日志文件不存在")
        return False
    
    try:
        # 读取最后一行状态
        with open(status_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if not lines:
                logger.error("状态汇总日志文件为空")
                return False
            
            last_line = lines[-1].strip()
            logger.info(f"最新状态: {last_line}")
            
            # 解析时间戳
            if '[202' in last_line:  # 简单的时间戳检查
                timestamp_str = last_line[1:20]  # 提取时间戳部分
                try:
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                    now = datetime.now()
                    
                    # 检查是否在30秒内有更新
                    if now - timestamp > timedelta(seconds=60):
                        logger.error(f"状态汇总日志超过60秒未更新，最后更新时间: {timestamp}")
                        return False
                    
                    logger.info(f"状态汇总日志正常，最后更新时间: {timestamp}")
                    return True
                    
                except ValueError as e:
                    logger.error(f"解析时间戳失败: {e}")
                    return False
            else:
                logger.error("状态汇总日志格式异常")
                return False
                
    except Exception as e:
        logger.error(f"读取状态汇总日志失败: {e}")
        return False

def check_main_log():
    """
    检查主日志文件
    """
    main_log_file = 'log/multi_grid_BN.log'
    
    if not os.path.exists(main_log_file):
        logger.error("主日志文件不存在")
        return False
    
    try:
        # 检查文件大小
        file_size = os.path.getsize(main_log_file)
        if file_size == 0:
            logger.error("主日志文件为空")
            return False
        
        # 读取最后几行检查是否有错误
        with open(main_log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if not lines:
                logger.error("主日志文件为空")
                return False
            
            # 检查最后10行是否有ERROR
            last_lines = lines[-10:]
            for line in last_lines:
                if 'ERROR' in line:
                    logger.warning(f"发现错误日志: {line.strip()}")
                    # 不直接返回False，因为可能是临时错误
        
        logger.info("主日志文件正常")
        return True
        
    except Exception as e:
        logger.error(f"检查主日志文件失败: {e}")
        return False

def check_bot_logs():
    """
    检查各个币种的日志文件
    """
    log_dir = 'log'
    if not os.path.exists(log_dir):
        logger.error("日志目录不存在")
        return False
    
    bot_logs = []
    for file in os.listdir(log_dir):
        if file.startswith('grid_BN_') and file.endswith('.log'):
            bot_logs.append(file)
    
    if not bot_logs:
        logger.warning("未找到币种日志文件")
        return True  # 不视为错误，可能是刚启动
    
    healthy_bots = 0
    total_bots = len(bot_logs)
    
    for log_file in bot_logs:
        try:
            file_path = os.path.join(log_dir, log_file)
            file_size = os.path.getsize(file_path)
            
            if file_size == 0:
                logger.warning(f"币种日志文件为空: {log_file}")
                continue
            
            # 检查最后更新时间
            mtime = os.path.getmtime(file_path)
            last_update = datetime.fromtimestamp(mtime)
            now = datetime.now()
            
            if now - last_update > timedelta(minutes=5):
                logger.warning(f"币种日志文件超过5分钟未更新: {log_file}")
                continue
            
            # 检查是否有严重错误
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    if 'ERROR' in last_line and '启动失败' in last_line:
                        logger.error(f"币种启动失败: {log_file}")
                        continue
            
            healthy_bots += 1
            logger.info(f"币种日志正常: {log_file}")
            
        except Exception as e:
            logger.error(f"检查币种日志失败 {log_file}: {e}")
    
    logger.info(f"币种日志检查完成: {healthy_bots}/{total_bots} 正常")
    return healthy_bots > 0  # 至少有一个币种正常运行

def check_process_status():
    """
    检查进程状态（通过检查PID文件）
    """
    pid_file = 'grid_bot.pid'
    
    if not os.path.exists(pid_file):
        logger.warning("PID文件不存在，可能是首次启动")
        return True
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        # 检查进程是否存在
        try:
            os.kill(pid, 0)  # 发送信号0检查进程是否存在
            logger.info(f"主进程运行正常，PID: {pid}")
            return True
        except OSError:
            logger.error(f"主进程不存在，PID: {pid}")
            return False
            
    except Exception as e:
        logger.error(f"检查进程状态失败: {e}")
        return False

def main():
    """
    主健康检查函数
    """
    logger.info("开始健康检查...")
    
    checks = [
        ("状态汇总日志", check_status_summary),
        ("主日志文件", check_main_log),
        ("币种日志文件", check_bot_logs),
        ("进程状态", check_process_status)
    ]
    
    results = []
    for check_name, check_func in checks:
        logger.info(f"检查: {check_name}")
        try:
            result = check_func()
            results.append((check_name, result))
            status = "通过" if result else "失败"
            logger.info(f"{check_name}: {status}")
        except Exception as e:
            logger.error(f"{check_name} 检查异常: {e}")
            results.append((check_name, False))
    
    # 汇总结果
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    logger.info(f"健康检查完成: {passed}/{total} 项通过")
    
    # 返回退出码
    if passed >= total * 0.75:  # 75%以上通过视为健康
        logger.info("系统状态: 健康")
        exit(0)
    else:
        logger.error("系统状态: 异常")
        exit(1)

if __name__ == "__main__":
    main() 