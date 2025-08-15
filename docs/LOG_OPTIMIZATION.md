# 日志优化说明

## 问题描述

原有的日志系统存在以下问题：

1. **日志文件过大**：`binance_multi_bot.log` 文件达到139MB，包含176万行日志
2. **频繁重复日志**：状态信息每30秒记录一次，阈值日志重复记录
3. **缺乏日志轮转**：部分日志文件没有按日期分割
4. **磁盘空间占用**：日志文件占用过多磁盘空间

## 优化方案

### 1. 日志轮转优化

- **按日期分割**：所有日志文件每天午夜自动分割
- **保留策略**：保留最近7天的日志文件
- **文件命名**：使用 `TimedRotatingFileHandler` 自动管理

### 2. 去重机制

- **重复过滤**：相同日志消息在1小时内最多记录3次
- **状态去重**：状态信息每天只记录一次
- **阈值状态管理**：只在状态变化时记录阈值日志

### 3. 日志分类

- **主日志**：`multi_grid_BN.log` - 系统状态和重要事件
- **币种日志**：`grid_BN_[币种].log` - 各币种的详细交易日志
- **状态汇总**：`status_summary.log` - 实时状态更新
- **每日状态**：`daily_status.log` - 每日状态记录

## 新增功能

### 1. 日志配置模块 (`logging_config.py`)

```python
# 去重过滤器
class DuplicateFilter(logging.Filter):
    # 避免重复的日志信息

# 每日状态记录器
class DailyStatusLogger:
    # 确保状态信息每天只记录一次

# 阈值状态记录器
class ThresholdStateLogger:
    # 只在状态变化时记录阈值日志
```

### 2. 日志清理工具 (`scripts/log_cleanup.py`)

```bash
# 查看日志文件大小
python3 scripts/log_cleanup.py --size

# 清理旧日志文件
python3 scripts/log_cleanup.py --cleanup --days 7

# 压缩旧日志文件
python3 scripts/log_cleanup.py --compress --days 1
```

### 3. 定时任务设置 (`scripts/setup_log_cleanup.sh`)

```bash
# 设置定时清理任务
bash scripts/setup_log_cleanup.sh
```

## 使用说明

### 1. 启动优化后的系统

```bash
# 多币种模式
python3 src/multi_bot/multi_bot.py

# 单币种模式
python3 src/single_bot/binance_bot.py
```

### 2. 监控日志

```bash
# 查看主日志
tail -f log/multi_grid_BN.log

# 查看状态汇总
tail -f log/status_summary.log

# 查看特定币种日志
tail -f log/grid_BN_BTCUSDT.log
```

### 3. 日志管理

```bash
# 查看日志文件大小
python3 scripts/log_cleanup.py --size

# 手动清理旧日志
python3 scripts/log_cleanup.py --cleanup --days 7

# 压缩旧日志
python3 scripts/log_cleanup.py --compress --days 1
```

## 优化效果

### 1. 日志大小减少

- **状态日志**：从每30秒记录一次改为每天记录一次
- **阈值日志**：只在状态变化时记录，避免重复
- **去重机制**：相同日志在1小时内最多记录3次

### 2. 磁盘空间节省

- **日志轮转**：自动删除7天前的日志文件
- **压缩存储**：旧日志文件自动压缩
- **定时清理**：每天凌晨2点自动清理

### 3. 日志质量提升

- **信息密度**：减少冗余信息，提高日志可读性
- **状态跟踪**：清晰记录状态变化过程
- **错误定位**：保留重要错误信息，便于问题排查

## 配置参数

### 1. 去重配置

```python
# 最大重复次数
max_duplicates = 3

# 超时时间（秒）
timeout = 3600  # 1小时
```

### 2. 日志轮转配置

```python
# 轮转间隔
when = 'midnight'

# 保留文件数
backupCount = 7
```

### 3. 定时任务配置

```bash
# 每天凌晨2点执行清理
0 2 * * * cd /path/to/project && python3 scripts/log_cleanup.py --cleanup --days 7 --compress
```

## 故障排除

### 1. 日志文件权限问题

```bash
# 修复权限
chmod 755 log/
chown 1000:1000 log/
```

### 2. 定时任务不执行

```bash
# 检查crontab
crontab -l

# 重新设置定时任务
bash scripts/setup_log_cleanup.sh
```

### 3. 日志配置导入失败

```bash
# 检查Python路径
python3 -c "import sys; print(sys.path)"

# 手动设置路径
export PYTHONPATH="${PYTHONPATH}:/path/to/project/src/multi_bot"
```

## 注意事项

1. **备份重要日志**：清理前请备份重要的日志文件
2. **监控磁盘空间**：定期检查磁盘空间使用情况
3. **调整保留策略**：根据实际需求调整日志保留天数
4. **测试环境验证**：在生产环境使用前先在测试环境验证

## 更新日志

- **2025-08-15**：初始版本，实现日志轮转、去重和清理功能
- **2025-08-15**：添加定时任务和压缩功能
- **2025-08-15**：完善文档和使用说明
