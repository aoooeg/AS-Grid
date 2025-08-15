#!/bin/bash

# 日志清理定时任务设置脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "设置日志清理定时任务..."

# 创建日志清理脚本的完整路径
CLEANUP_SCRIPT="$SCRIPT_DIR/log_cleanup.py"

# 确保脚本有执行权限
chmod +x "$CLEANUP_SCRIPT"

# 创建crontab条目
CRON_JOB="0 2 * * * cd $PROJECT_DIR && python3 $CLEANUP_SCRIPT --cleanup --days 7 --compress"

# 检查是否已经存在相同的crontab条目
if crontab -l 2>/dev/null | grep -q "log_cleanup.py"; then
    echo "日志清理定时任务已存在，跳过设置"
else
    # 添加新的crontab条目
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "已添加日志清理定时任务 (每天凌晨2点执行)"
fi

# 显示当前的crontab条目
echo "当前的定时任务:"
crontab -l 2>/dev/null | grep "log_cleanup.py" || echo "未找到日志清理定时任务"

echo "设置完成！"
echo ""
echo "手动执行日志清理:"
echo "  python3 $CLEANUP_SCRIPT --cleanup --days 7"
echo ""
echo "查看日志文件大小:"
echo "  python3 $CLEANUP_SCRIPT --size"
echo ""
echo "压缩旧日志文件:"
echo "  python3 $CLEANUP_SCRIPT --compress --days 1"
