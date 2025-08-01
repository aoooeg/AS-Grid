# 单币种到多币种版本迁移指南

## 概述

本指南将帮助您从原有的单币种网格交易机器人升级到多币种版本。多币种版本完全向后兼容，原有的单币种功能保持不变。

## 迁移前准备

### 1. 备份现有配置

```bash
# 备份原有配置
cp .env .env.backup
cp src/single_bot/binance_bot.py src/single_bot/binance_bot.py.backup

# 备份日志（可选）
cp -r log log.backup
```

### 2. 检查现有环境

```bash
# 检查 Python 版本
python3 --version

# 检查依赖包
pip list | grep -E "(ccxt|websockets|yaml|aiohttp)"

# 检查现有配置
cat .env
```

## 迁移步骤

### 步骤 1: 安装新依赖

```bash
# 安装多币种版本需要的额外依赖
pip install pyyaml

# 验证安装
python3 -c "import yaml; print('YAML 支持已安装')"
```

### 步骤 2: 创建多币种配置文件

创建 `symbols.yaml` 文件：

```yaml
symbols:
  # 将原有的单币种配置迁移过来
  - name: XRPUSDT  # 替换为您的币种
    grid_spacing: 0.001  # 使用原有的网格间距
    initial_quantity: 3  # 使用原有的初始数量
    leverage: 20  # 使用原有的杠杆倍数
    contract_type: USDT
```

### 步骤 3: 验证配置

```bash
# 验证 YAML 格式
python3 -c "import yaml; yaml.safe_load(open('symbols.yaml'))"

# 验证环境变量
python3 -c "from dotenv import load_dotenv; load_dotenv(); import os; print('API_KEY:', '已设置' if os.getenv('API_KEY') else '未设置')"
```

### 步骤 4: 测试多币种版本

```bash
# 启动多币种版本进行测试
python3 multi_src/single_bot/binance_bot.py

# 或使用 Docker
./scripts/deploy.sh multi-start
```

## 配置对比

### 原有单币种配置 (.env)

```bash
# 原有配置
COIN_NAME=XRP
GRID_SPACING=0.001
INITIAL_QUANTITY=3
LEVERAGE=20
CONTRACT_TYPE=USDT
```

### 新的多币种配置 (symbols.yaml)

```yaml
symbols:
  - name: XRPUSDT  # 对应 COIN_NAME
    grid_spacing: 0.001  # 对应 GRID_SPACING
    initial_quantity: 3  # 对应 INITIAL_QUANTITY
    leverage: 20  # 对应 LEVERAGE
    contract_type: USDT  # 对应 CONTRACT_TYPE
```

## 功能对比

| 功能 | 单币种版本 | 多币种版本 | 说明 |
|------|------------|------------|------|
| 启动方式 | `python3 src/single_bot/binance_bot.py` | `python3 multi_src/single_bot/binance_bot.py` | 多币种支持 |
| 配置文件 | `.env` | `.env` + `symbols.yaml` | 分离配置 |
| 日志文件 | `log/grid_BN.log` | `log/multi_grid_BN.log` + `log/grid_BN_*.log` | 独立日志 |
| 状态监控 | 无 | `log/status_summary.log` | 状态汇总 |
| 健康检查 | 无 | `health_check.py` | 健康监控 |
| Docker 支持 | 基础 | 增强 | 完整支持 |

## 升级路径

### 路径 1: 渐进式升级

1. **保持单币种运行**
   ```bash
   # 继续使用原有方式
   python3 src/single_bot/binance_bot.py
   ```

2. **并行测试多币种**
   ```bash
   # 在测试环境运行多币种
   python3 multi_src/single_bot/binance_bot.py
   ```

3. **逐步迁移**
   ```bash
   # 确认多币种稳定后，停止单币种
   # 切换到多币种模式
   ```

### 路径 2: 直接升级

1. **备份现有配置**
   ```bash
   cp .env .env.backup
   ```

2. **创建多币种配置**
   ```bash
   # 根据原有配置创建 symbols.yaml
   ```

3. **启动多币种版本**
   ```bash
   python3 multi_src/single_bot/binance_bot.py
   ```

## 测试验证

### 测试清单

- [ ] 多币种版本能正常启动
- [ ] 日志文件正常生成
- [ ] Telegram 通知正常
- [ ] 网格交易逻辑正常
- [ ] 风控机制正常
- [ ] 健康检查通过

### 验证命令

```bash
# 1. 检查启动状态
tail -f log/multi_grid_BN.log

# 2. 检查状态汇总
tail -f log/status_summary.log

# 3. 检查币种日志
tail -f log/grid_BN_XRPUSDT.log

# 4. 健康检查
python3 health_check.py

# 5. 检查 Docker 状态（如果使用）
./scripts/deploy.sh status
```

## 回退方案

### 如果多币种版本有问题

1. **停止多币种版本**
   ```bash
   # 如果使用 Docker
   ./scripts/deploy.sh stop
   
   # 如果直接运行
   # Ctrl+C 停止进程
   ```

2. **恢复单币种版本**
   ```bash
   # 使用原有的启动方式
   python3 src/single_bot/binance_bot.py
   
   # 或使用 Docker
   ./scripts/deploy.sh start
   ```

3. **恢复配置**
   ```bash
   # 恢复原有配置
   cp .env.backup .env
   ```

## 常见问题

### Q1: 多币种版本会影响原有的单币种功能吗？

**A**: 不会。多币种版本完全向后兼容，原有的 `src/single_bot/binance_bot.py` 可以继续正常使用。

### Q2: 如何添加新的币种？

**A**: 在 `symbols.yaml` 文件中添加新的币种配置：

```yaml
symbols:
  - name: BTCUSDT
    grid_spacing: 0.004
    initial_quantity: 0.001
    leverage: 20
    contract_type: USDT
  - name: ETHUSDT  # 新增币种
    grid_spacing: 0.005
    initial_quantity: 0.01
    leverage: 20
    contract_type: USDT
```

### Q3: 多币种版本会消耗更多资源吗？

**A**: 会，但影响有限。每个币种运行在独立线程中，主要增加：
- 内存使用：每个币种约 50-100MB
- CPU 使用：每个币种约 5-10%
- 网络连接：每个币种 2-3 个 WebSocket 连接

### Q4: 如何监控多币种版本的运行状态？

**A**: 使用以下方式：

```bash
# 查看状态汇总
tail -f log/status_summary.log

# 查看主日志
tail -f log/multi_grid_BN.log

# 查看特定币种日志
tail -f log/grid_BN_BTCUSDT.log

# 健康检查
python3 health_check.py
```

### Q5: 多币种版本支持哪些配置文件格式？

**A**: 支持两种格式：
- YAML 格式：`symbols.yaml`（推荐）
- JSON 格式：`symbols.json`

### Q6: 如何从多币种版本回退到单币种版本？

**A**: 有两种方式：

1. **使用原有的单币种启动方式**
   ```bash
   python3 src/single_bot/binance_bot.py
   ```

2. **使用 Docker 单币种模式**
   ```bash
   ./scripts/deploy.sh start
   ```

## 性能优化建议

### 1. 币种数量控制

- **建议币种数量**: 2-5 个币种
- **最大币种数量**: 10 个币种
- **资源监控**: 定期检查内存和 CPU 使用

### 2. 日志管理

```bash
# 定期清理旧日志
find log/ -name "*.log.*" -mtime +7 -delete

# 压缩日志文件
gzip log/grid_BN_*.log.*
```

### 3. 监控设置

```bash
# 设置日志轮转
# 已在代码中配置，每天自动轮转

# 设置健康检查
# 已在 Docker 中配置，每30秒检查一次
```

## 故障排除

### 问题 1: 多币种版本启动失败

**解决方案**:
```bash
# 检查配置文件
python3 -c "import yaml; yaml.safe_load(open('symbols.yaml'))"

# 检查环境变量
python3 -c "from dotenv import load_dotenv; load_dotenv(); import os; print('API_KEY:', os.getenv('API_KEY'))"

# 检查依赖
pip install ccxt websockets python-dotenv pyyaml aiohttp
```

### 问题 2: 某个币种运行异常

**解决方案**:
```bash
# 查看特定币种日志
tail -f log/grid_BN_[币种].log

# 检查币种配置
grep -A 5 "[币种]" symbols.yaml

# 重启特定币种（需要重启整个服务）
./scripts/deploy.sh restart
```

### 问题 3: 日志文件过大

**解决方案**:
```bash
# 检查日志文件大小
du -sh log/*.log

# 清理旧日志
find log/ -name "*.log.*" -mtime +7 -delete

# 压缩日志
gzip log/grid_BN_*.log.*
```

## 总结

多币种版本是对原有单币种版本的增强，提供了：

1. **完全向后兼容**: 原有功能保持不变
2. **多币种支持**: 同时运行多个币种
3. **增强的监控**: 状态汇总和健康检查
4. **更好的日志管理**: 独立日志和自动轮转
5. **完整的 Docker 支持**: 生产环境部署

建议采用渐进式升级方式，先在测试环境验证多币种版本的稳定性，确认无误后再在生产环境使用。 