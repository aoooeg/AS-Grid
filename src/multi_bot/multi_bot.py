import asyncio
import os
import sys
import signal
import threading
import time
import yaml
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import sys
import os
sys.path.append(os.path.dirname(__file__))
from binance_multi_bot import BinanceGridBot
from logging_config import setup_logging, create_bot_logger, DailyStatusLogger

# 加载环境变量
load_dotenv()

# 全局变量用于控制所有机器人
running_bots = {}
stop_event = threading.Event()

# 配置优化的日志系统
main_logger = setup_logging()
daily_status_logger = DailyStatusLogger(main_logger)

def load_config(config_file='config/symbols.yaml'):
    """
    加载配置文件
    
    Args:
        config_file: 配置文件路径，支持 yaml 和 json 格式
        
    Returns:
        dict: 配置字典
    """
    if not os.path.exists(config_file):
        main_logger.error(f"配置文件 {config_file} 不存在")
        return None
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            if config_file.endswith('.yaml') or config_file.endswith('.yml'):
                config = yaml.safe_load(f)
            elif config_file.endswith('.json'):
                config = json.load(f)
            else:
                main_logger.error(f"不支持的配置文件格式: {config_file}")
                return None
        
        # 验证配置格式
        if 'symbols' not in config:
            main_logger.error("配置文件中缺少 'symbols' 字段")
            return None
        
        if not isinstance(config['symbols'], list):
            main_logger.error("'symbols' 字段必须是列表格式")
            return None
        
        # 验证每个币种配置
        for i, symbol_config in enumerate(config['symbols']):
            if 'name' not in symbol_config:
                main_logger.error(f"第 {i+1} 个币种配置缺少 'name' 字段")
                return None
            
            # 设置默认值
            if 'grid_spacing' not in symbol_config:
                symbol_config['grid_spacing'] = 0.001
            if 'initial_quantity' not in symbol_config:
                symbol_config['initial_quantity'] = 3
            if 'leverage' not in symbol_config:
                symbol_config['leverage'] = 20
            if 'contract_type' not in symbol_config:
                symbol_config['contract_type'] = 'USDT'
        
        main_logger.info(f"成功加载配置文件: {config_file}")
        return config
    
    except Exception as e:
        main_logger.error(f"加载配置文件失败: {e}")
        return None

def validate_environment():
    """
    验证环境变量
    
    Returns:
        tuple: (api_key, api_secret) 或 (None, None)
    """
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    
    if not api_key or not api_secret:
        main_logger.error("API_KEY 和 API_SECRET 必须设置在 .env 文件中")
        return None, None
    
    # 验证其他可选配置
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enable_notifications = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
    
    if enable_notifications:
        if not telegram_bot_token or not telegram_chat_id:
            main_logger.warning("Telegram通知已启用但缺少BOT_TOKEN或CHAT_ID，将禁用通知功能")
        else:
            main_logger.info("Telegram通知功能已启用")
    
    return api_key, api_secret

def create_bot_logger(symbol):
    """
    为每个币种创建独立的日志记录器
    
    Args:
        symbol: 币种符号
        
    Returns:
        logging.Logger: 日志记录器
    """
    return create_bot_logger(symbol)

def run_single_bot(symbol_config, api_key, api_secret):
    """
    运行单个币种的网格机器人
    
    Args:
        symbol_config: 币种配置字典
        api_key: API密钥
        api_secret: API密钥
        
    Returns:
        tuple: (symbol, success, error_message)
    """
    symbol = symbol_config['name']
    logger = create_bot_logger(symbol)
    
    try:
        # 构建配置字典
        config = {
            'grid_spacing': symbol_config['grid_spacing'],
            'initial_quantity': symbol_config['initial_quantity'],
            'leverage': symbol_config['leverage'],
            'contract_type': symbol_config['contract_type']
        }
        
        logger.info(f"启动 {symbol} 网格机器人")
        logger.info(f"配置: 网格间距={config['grid_spacing']:.3f}, 初始数量={config['initial_quantity']}, 杠杆={config['leverage']}")
        
        # 创建机器人实例
        bot = BinanceGridBot(symbol=symbol, api_key=api_key, api_secret=api_secret, config=config)
        
        # 存储机器人实例（用于停止）
        running_bots[symbol] = bot
        
        # 在新线程中创建事件循环并运行机器人
        def run_bot_with_loop():
            try:
                # 创建新的事件循环
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # 运行机器人
                loop.run_until_complete(bot.start())
            except Exception as e:
                logger.error(f"机器人运行异常: {e}")
            finally:
                try:
                    loop.close()
                except:
                    pass
        
        # 在新线程中运行
        import threading
        bot_thread = threading.Thread(target=run_bot_with_loop, name=f"bot-{symbol}")
        bot_thread.daemon = True
        bot_thread.start()
        
        # 等待一小段时间确保线程启动
        import time
        time.sleep(0.1)
        
        # 等待机器人真正启动
        max_wait_time = 30  # 最多等待30秒
        wait_time = 0
        while wait_time < max_wait_time:
            if symbol in running_bots:
                # 检查机器人是否真正运行
                bot = running_bots[symbol]
                if hasattr(bot, 'running') and bot.running:
                    logger.info(f"{symbol} 机器人已添加到运行列表")
                    return symbol, True, None
            time.sleep(1)
            wait_time += 1
        
        # 如果超时，从运行列表中移除
        if symbol in running_bots:
            del running_bots[symbol]
        
        return symbol, False, "机器人启动超时"
        
    except Exception as e:
        error_msg = f"启动 {symbol} 机器人失败: {str(e)}"
        logger.error(error_msg)
        return symbol, False, error_msg

def signal_handler(signum, frame):
    """
    信号处理器，用于优雅停止所有机器人
    """
    main_logger.info("收到停止信号，正在停止所有机器人...")
    stop_event.set()
    
    # 停止所有机器人
    for symbol, bot in running_bots.items():
        try:
            bot.stop()
            main_logger.info(f"已停止 {symbol} 机器人")
        except Exception as e:
            main_logger.error(f"停止 {symbol} 机器人失败: {e}")
    
    sys.exit(0)

def print_status():
    """
    打印当前运行状态并写入状态汇总日志
    """
    while not stop_event.is_set():
        try:
            active_bots = len(running_bots)
            if active_bots > 0:
                symbols = list(running_bots.keys())
                status_info = f"当前活跃机器人: {active_bots} 个 - {', '.join(symbols)}"
                # 使用每日状态记录器，每天只记录一次
                daily_status_logger.log_status(status_info)
                
                # 写入状态汇总日志（保持原有的实时更新）
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                status_summary = f"[{timestamp}] Active Bots: {', '.join([f'{s}=Running' for s in symbols])}"
                
                # 写入状态汇总文件
                try:
                    with open('log/status_summary.log', 'a', encoding='utf-8') as f:
                        f.write(status_summary + '\n')
                except Exception as e:
                    main_logger.error(f"写入状态汇总日志失败: {e}")
            else:
                daily_status_logger.log_status("当前没有活跃的机器人")
                
                # 写入状态汇总文件
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                status_summary = f"[{timestamp}] Active Bots: None"
                try:
                    with open('log/status_summary.log', 'a', encoding='utf-8') as f:
                        f.write(status_summary + '\n')
                except Exception as e:
                    main_logger.error(f"写入状态汇总日志失败: {e}")
                    
            time.sleep(30)  # 每30秒检查一次状态
        except KeyboardInterrupt:
            break

def main():
    """
    主函数
    """
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    main_logger.info("多币种网格交易机器人启动中...")
    
    # 验证环境变量
    api_key, api_secret = validate_environment()
    if not api_key or not api_secret:
        main_logger.error("环境变量验证失败，程序退出")
        sys.exit(1)
    
    # 加载配置文件
    config = load_config()
    if not config:
        main_logger.error("配置文件加载失败，程序退出")
        sys.exit(1)
    
    symbols = config['symbols']
    main_logger.info(f"配置了 {len(symbols)} 个币种: {[s['name'] for s in symbols]}")
    
    # 启动状态监控线程
    status_thread = threading.Thread(target=print_status, daemon=True)
    status_thread.start()
    
    # 直接运行所有机器人，不使用线程池
    bot_threads = {}
    for symbol_config in symbols:
        symbol = symbol_config['name']
        main_logger.info(f"启动 {symbol} 网格机器人")
        
        # 创建机器人线程
        bot_thread = threading.Thread(
            target=run_single_bot, 
            args=(symbol_config, api_key, api_secret),
            name=f"bot-{symbol}",
            daemon=True
        )
        bot_thread.start()
        bot_threads[symbol] = bot_thread
    
    # 等待所有机器人启动完成
    main_logger.info("等待所有机器人启动完成...")
    for symbol, thread in bot_threads.items():
        thread.join(timeout=60)  # 最多等待60秒
    
    # 主循环：监控机器人状态
    try:
        while not stop_event.is_set():
            active_bots = len(running_bots)
            if active_bots > 0:
                symbols = list(running_bots.keys())
                # 使用每日状态记录器，每天只记录一次
                daily_status_logger.log_status(f"当前活跃机器人: {active_bots} 个 - {', '.join(symbols)}")
            else:
                daily_status_logger.log_status("当前没有活跃的机器人")
            
            time.sleep(30)  # 每30秒检查一次状态
    except KeyboardInterrupt:
        main_logger.info("收到中断信号，正在停止所有机器人...")
        stop_event.set()
        
        # 停止所有机器人
        for symbol, bot in running_bots.items():
            try:
                bot.stop()
                main_logger.info(f"已停止 {symbol} 机器人")
            except Exception as e:
                main_logger.error(f"停止 {symbol} 机器人失败: {e}")

if __name__ == "__main__":
    main() 