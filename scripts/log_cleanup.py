#!/usr/bin/env python3
"""
日志清理脚本
定期清理旧的日志文件，防止磁盘空间不足
"""

import os
import glob
import time
import argparse
from datetime import datetime, timedelta
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src', 'multi_bot'))
try:
    from logging_config import cleanup_old_logs
except ImportError:
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

def get_log_file_sizes():
    """获取日志文件大小信息"""
    log_dir = "log"
    if not os.path.exists(log_dir):
        print("日志目录不存在")
        return
    
    total_size = 0
    file_info = []
    
    for log_file in glob.glob(os.path.join(log_dir, "*.log*")):
        try:
            size = os.path.getsize(log_file)
            mtime = os.path.getmtime(log_file)
            mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            file_info.append((log_file, size, mtime_str))
            total_size += size
        except Exception as e:
            print(f"获取文件信息失败 {log_file}: {e}")
    
    # 按大小排序
    file_info.sort(key=lambda x: x[1], reverse=True)
    
    print(f"\n日志文件大小统计 (总计: {total_size / (1024*1024):.2f} MB):")
    print("-" * 80)
    print(f"{'文件名':<40} {'大小(MB)':<10} {'修改时间':<20}")
    print("-" * 80)
    
    for file_path, size, mtime in file_info:
        filename = os.path.basename(file_path)
        size_mb = size / (1024*1024)
        print(f"{filename:<40} {size_mb:<10.2f} {mtime:<20}")
    
    return total_size, file_info

def compress_old_logs(days=1):
    """压缩旧的日志文件"""
    import gzip
    import shutil
    
    log_dir = "log"
    cutoff_time = time.time() - (days * 24 * 3600)
    
    compressed_count = 0
    for log_file in glob.glob(os.path.join(log_dir, "*.log.*")):
        try:
            if os.path.getmtime(log_file) < cutoff_time:
                # 检查是否已经压缩
                if not log_file.endswith('.gz'):
                    with open(log_file, 'rb') as f_in:
                        with gzip.open(log_file + '.gz', 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    os.remove(log_file)
                    compressed_count += 1
                    print(f"已压缩: {os.path.basename(log_file)}")
        except Exception as e:
            print(f"压缩文件失败 {log_file}: {e}")
    
    if compressed_count > 0:
        print(f"共压缩了 {compressed_count} 个日志文件")
    else:
        print("没有需要压缩的日志文件")

def main():
    parser = argparse.ArgumentParser(description='日志清理工具')
    parser.add_argument('--cleanup', action='store_true', help='清理超过指定天数的旧日志文件')
    parser.add_argument('--compress', action='store_true', help='压缩超过指定天数的旧日志文件')
    parser.add_argument('--days', type=int, default=7, help='保留天数 (默认: 7天)')
    parser.add_argument('--size', action='store_true', help='显示日志文件大小统计')
    
    args = parser.parse_args()
    
    if args.size:
        get_log_file_sizes()
    
    if args.cleanup:
        print(f"清理超过 {args.days} 天的旧日志文件...")
        cleanup_old_logs(args.days)
    
    if args.compress:
        print(f"压缩超过 {args.days} 天的旧日志文件...")
        compress_old_logs(args.days)
    
    if not any([args.cleanup, args.compress, args.size]):
        # 默认显示统计信息
        get_log_file_sizes()

if __name__ == "__main__":
    main()
