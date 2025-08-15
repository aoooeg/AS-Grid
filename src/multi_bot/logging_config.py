import os
import logging
import time
from datetime import datetime, date
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler
from collections import defaultdict

class DuplicateFilter(logging.Filter):
    """去重过滤器，避免重复的日志信息"""
    
    def __init__(self, name='', max_duplicates=3, timeout=3600):
        super().__init__(name)
        self.max_duplicates = max_duplicates
        self.timeout = timeout
        self.duplicate_count = defaultdict(int)
        self.last_log_time = defaultdict(float)
    
    def filter(self, record):
        # 创建日志消息的唯一标识
        message_key = f"{record.levelname}:{record.getMessage()}"
        current_time = time.time()
        
        # 检查是否超时，如果超时则重置计数
        if current_time - self.last_log_time[message_key] > self.timeout:
            self.duplicate_count[message_key] = 0
        
        # 增加计数
        self.duplicate_count[message_key] += 1
        self.last_log_time[message_key] = current_time
        
        # 如果超过最大重复次数，则过滤掉
        if self.duplicate_count[message_key] > self.max_duplicates:
            return False
        
        return True

class DailyStatusLogger:
    """每日状态记录器，确保状态信息每天只记录一次"""
    
    def __init__(self, logger, log_file='log/daily_status.log'):
        self.logger = logger
        self.log_file = log_file
        self.last_status_date = None
        self.last_status_message = None
        
        # 确保日志目录存在
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    
    def log_status(self, message):
        """记录状态信息，每天只记录一次"""
        current_date = date.today()
        
        # 如果是新的一天或者消息发生变化，则记录
        if (self.last_status_date != current_date or 
            self.last_status_message != message):
            
            self.logger.info(message)
            self.last_status_date = current_date
            self.last_status_message = message

class ThresholdStateLogger:
    """阈值状态记录器，只在状态变化时记录"""
    
    def __init__(self, logger):
        self.logger = logger
        self.threshold_states = {}  # 记录每个币种的阈值状态
    
    def log_threshold_status(self, symbol, side, position, threshold, is_over_threshold):
        """记录阈值状态，只在状态变化时记录"""
        state_key = f"{symbol}_{side}"
        
        # 检查状态是否发生变化
        if (state_key not in self.threshold_states or 
            self.threshold_states[state_key] != is_over_threshold):
            
            if is_over_threshold:
                self.logger.info(f"持仓{position}超过极限阈值 {threshold}，{side} 装死")
            else:
                self.logger.info(f"持仓{position}已低于极限阈值 {threshold}，{side} 恢复正常")
            
            self.threshold_states[state_key] = is_over_threshold

def setup_logging():
    """设置优化的日志配置"""
    
    # 确保日志目录存在
    os.makedirs("log", exist_ok=True)
    
    # 创建主日志记录器
    main_logger = logging.getLogger('main')
    main_logger.setLevel(logging.INFO)
    
    # 清除现有的处理器
    main_logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    main_logger.addHandler(console_handler)
    
    # 文件处理器 - 按日期分割，限制文件大小
    file_handler = TimedRotatingFileHandler(
        'log/multi_grid_BN.log',
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    
    # 添加去重过滤器
    duplicate_filter = DuplicateFilter(max_duplicates=3, timeout=3600)
    file_handler.addFilter(duplicate_filter)
    
    main_logger.addHandler(file_handler)
    
    return main_logger

def create_bot_logger(symbol):
    """为每个币种创建独立的日志记录器"""
    
    logger = logging.getLogger(f'bot_{symbol}')
    logger.setLevel(logging.INFO)
    
    # 避免重复添加处理器
    if logger.handlers:
        return logger
    
    # 文件处理器 - 按日期分割，限制文件大小
    file_handler = TimedRotatingFileHandler(
        f'log/grid_BN_{symbol}.log',
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    # 添加去重过滤器
    duplicate_filter = DuplicateFilter(max_duplicates=3, timeout=3600)
    file_handler.addFilter(duplicate_filter)
    
    logger.addHandler(file_handler)
    
    return logger

def setup_binance_multi_bot_logging():
    """设置币安多币种机器人的日志配置"""
    
    # 确保日志目录存在
    os.makedirs("log", exist_ok=True)
    
    # 检查是否从单币种脚本调用
    import inspect
    import sys
    
    # 遍历调用栈，查找调用者
    log_filename = None
    for frame_info in inspect.stack():
        frame = frame_info.frame
        filename = frame.f_globals.get('__file__', '')
        if filename and 'single_bot' in filename and 'binance_bot.py' in filename:
            log_filename = "binance_single_bot.log"
            break
    
    if not log_filename:
        import os
        script_name = os.path.splitext(os.path.basename(__file__))[0]
        log_filename = f"{script_name}.log"
    
    handlers = [logging.StreamHandler()]
    
    try:
        # 使用轮转文件处理器替代普通文件处理器
        file_handler = TimedRotatingFileHandler(
            f"log/{log_filename}",
            when='midnight',
            interval=1,
            backupCount=7,
            encoding='utf-8'
        )
        handlers.append(file_handler)
        print(f"日志将写入文件: log/{log_filename}")
    except PermissionError as e:
        print(f"警告: 无法创建日志文件 (权限不足): {e}")
        print("日志将只输出到控制台")
    except Exception as e:
        print(f"警告: 无法创建日志文件: {e}")
        print("日志将只输出到控制台")
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    
    logger = logging.getLogger()
    
    # 为文件处理器添加去重过滤器
    for handler in handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            duplicate_filter = DuplicateFilter(max_duplicates=3, timeout=3600)
            handler.addFilter(duplicate_filter)
    
    return logger

def cleanup_old_logs(days=7):
    """清理旧的日志文件"""
    import glob
    import os
    from datetime import datetime, timedelta
    
    log_dir = "log"
    cutoff_date = datetime.now() - timedelta(days=days)
    
    # 查找所有日志文件
    log_patterns = [
        "*.log.*",  # 轮转的日志文件
        "*.log.gz",  # 压缩的日志文件
    ]
    
    for pattern in log_patterns:
        for log_file in glob.glob(os.path.join(log_dir, pattern)):
            try:
                # 获取文件修改时间
                file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
                if file_mtime < cutoff_date:
                    os.remove(log_file)
                    print(f"已删除旧日志文件: {log_file}")
            except Exception as e:
                print(f"删除日志文件失败 {log_file}: {e}")
