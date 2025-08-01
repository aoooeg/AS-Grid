# 多币种版本测试验证指南

## 测试目标

验证多币种网格交易机器人的以下功能：
1. 多币种并行运行正常
2. 日志系统独立且完整
3. 网格策略与风控逻辑与原版一致
4. 健康检查与监控功能正常

## 测试环境准备

### 1. 测试配置

创建测试用的 `symbols.yaml`：

```yaml
symbols:
  # 测试币种 1: BTCUSDT
  - name: BTCUSDT
    grid_spacing: 0.004
    initial_quantity: 0.001
    leverage: 20
    contract_type: USDT
    
  # 测试币种 2: ETHUSDT
  - name: ETHUSDT
    grid_spacing: 0.005
    initial_quantity: 0.01
    leverage: 20
    contract_type: USDT
```

### 2. 环境检查

```bash
# 检查 Python 环境
python3 --version

# 检查依赖包
pip list | grep -E "(ccxt|websockets|yaml|aiohttp)"

# 检查配置文件
ls -la symbols.yaml
ls -la .env

# 检查日志目录
mkdir -p log
ls -la log/
```

## 测试步骤

### 步骤 1: 启动测试

```bash
# 方式一：直接运行
python3 multi_src/single_bot/binance_bot.py

# 方式二：使用启动脚本
./scripts/start.sh multi

# 方式三：使用 Docker
./scripts/deploy.sh multi-start
```

### 步骤 2: 验证启动状态

```bash
# 1. 检查主日志
tail -f log/multi_grid_BN.log

# 预期输出：
# 2024-01-15 10:30:00,123 - main - INFO - 多币种网格交易机器人启动中...
# 2024-01-15 10:30:01,456 - main - INFO - 成功加载配置文件: symbols.yaml
# 2024-01-15 10:30:01,789 - main - INFO - 配置了 2 个币种: ['BTCUSDT', 'ETHUSDT']
```

### 步骤 3: 验证状态汇总日志

```bash
# 检查状态汇总日志
tail -f log/status_summary.log

# 预期输出：
# [2024-01-15 10:30:02] Active Bots: BTCUSDT=Running, ETHUSDT=Running
# [2024-01-15 10:30:32] Active Bots: BTCUSDT=Running, ETHUSDT=Running
```

### 步骤 4: 验证币种独立日志

```bash
# 检查 BTC 币种日志
tail -f log/grid_BN_BTCUSDT.log

# 预期输出：
# 2024-01-15 10:30:02,123 - INFO - 启动 BTCUSDT 网格机器人
# 2024-01-15 10:30:02,456 - INFO - 配置: 网格间距=0.004, 初始数量=0.001, 杠杆=20
# 2024-01-15 10:30:03,789 - INFO - 价格精度: 1, 数量精度: 3, 最小下单数量: 0.001

# 检查 ETH 币种日志
tail -f log/grid_BN_ETHUSDT.log

# 预期输出：
# 2024-01-15 10:30:02,123 - INFO - 启动 ETHUSDT 网格机器人
# 2024-01-15 10:30:02,456 - INFO - 配置: 网格间距=0.005, 初始数量=0.01, 杠杆=20
```

### 步骤 5: 验证健康检查

```bash
# 运行健康检查
python3 health_check.py

# 预期输出：
# 2024-01-15 10:30:05,123 - INFO - 开始健康检查...
# 2024-01-15 10:30:05,456 - INFO - 检查: 状态汇总日志
# 2024-01-15 10:30:05,789 - INFO - 状态汇总日志: 通过
# 2024-01-15 10:30:06,123 - INFO - 检查: 主日志文件
# 2024-01-15 10:30:06,456 - INFO - 主日志文件: 通过
# 2024-01-15 10:30:06,789 - INFO - 检查: 币种日志文件
# 2024-01-15 10:30:07,123 - INFO - 币种日志检查完成: 2/2 正常
# 2024-01-15 10:30:07,456 - INFO - 健康检查完成: 4/4 项通过
# 2024-01-15 10:30:07,789 - INFO - 系统状态: 健康
```

### 步骤 6: 验证 Docker 健康检查

```bash
# 如果使用 Docker，检查容器健康状态
docker inspect grid-trader --format='{{.State.Health.Status}}'

# 预期输出：healthy

# 查看健康检查日志
docker inspect grid-trader --format='{{.State.Health.Log}}'
```

## 功能验证清单

### ✅ 基础功能验证

- [ ] **多币种启动**: 两个币种都能正常启动
- [ ] **独立日志**: 每个币种有独立的日志文件
- [ ] **状态汇总**: 状态汇总日志正常更新
- [ ] **健康检查**: 健康检查脚本运行正常
- [ ] **错误隔离**: 单个币种错误不影响其他币种

### ✅ 网格策略验证

- [ ] **网格间距**: 每个币种使用正确的网格间距
- [ ] **初始数量**: 每个币种使用正确的初始数量
- [ ] **杠杆设置**: 每个币种使用正确的杠杆倍数
- [ ] **双向持仓**: 双向持仓模式正常启用
- [ ] **挂单逻辑**: 开仓和止盈挂单逻辑正常

### ✅ 风控机制验证

- [ ] **持仓阈值**: 持仓阈值计算正确
- [ ] **风险减仓**: 风险减仓机制正常
- [ ] **超时取消**: 挂单超时取消机制正常
- [ ] **通知系统**: Telegram 通知正常发送

### ✅ 日志系统验证

- [ ] **日志轮转**: 日志按日期自动轮转
- [ ] **日志格式**: 日志格式统一且完整
- [ ] **错误记录**: 错误信息正确记录
- [ ] **状态记录**: 状态变化正确记录

## 性能测试

### 1. 资源使用监控

```bash
# 监控内存使用
watch -n 5 'ps aux | grep python | grep multi_grid_BN'

# 监控 CPU 使用
top -p $(pgrep -f multi_grid_BN)

# 监控网络连接
netstat -an | grep :443 | wc -l
```

### 2. 日志性能测试

```bash
# 检查日志文件大小
du -sh log/*.log

# 检查日志写入速度
tail -f log/multi_grid_BN.log | wc -l
```

### 3. 并发测试

```bash
# 测试多个币种同时运行
# 在 symbols.yaml 中添加更多币种进行测试

# 监控线程状态
ps -eLf | grep multi_grid_BN
```

## 故障模拟测试

### 1. 网络中断测试

```bash
# 模拟网络中断
sudo iptables -A OUTPUT -p tcp --dport 443 -j DROP

# 观察错误处理
tail -f log/multi_grid_BN.log

# 恢复网络
sudo iptables -D OUTPUT -p tcp --dport 443 -j DROP
```

### 2. API 错误测试

```bash
# 修改 API 密钥为错误值
# 观察错误处理和重试机制
```

### 3. 配置文件错误测试

```bash
# 修改 symbols.yaml 为错误格式
# 观察配置验证和错误提示
```

## 测试报告模板

### 测试环境信息

- **测试时间**: 2024-01-15 10:30:00
- **测试版本**: 多币种版本 v1.0
- **测试币种**: BTCUSDT, ETHUSDT
- **测试环境**: Ubuntu 20.04, Python 3.8

### 测试结果

| 测试项目 | 预期结果 | 实际结果 | 状态 |
|----------|----------|----------|------|
| 多币种启动 | 两个币种正常启动 | ✅ 正常 | 通过 |
| 独立日志 | 每个币种独立日志 | ✅ 正常 | 通过 |
| 状态汇总 | 状态汇总正常更新 | ✅ 正常 | 通过 |
| 健康检查 | 健康检查通过 | ✅ 正常 | 通过 |
| 网格策略 | 网格策略正常 | ✅ 正常 | 通过 |
| 风控机制 | 风控机制正常 | ✅ 正常 | 通过 |

### 性能指标

- **内存使用**: 约 200MB
- **CPU 使用**: 约 15%
- **网络连接**: 6 个 WebSocket 连接
- **日志大小**: 主日志 1KB，币种日志各 2KB

### 问题记录

- **问题 1**: 无
- **问题 2**: 无
- **问题 3**: 无

### 测试结论

✅ **测试通过**: 多币种版本功能正常，可以投入生产使用

## 持续监控

### 1. 日常监控命令

```bash
# 查看运行状态
tail -1 log/status_summary.log

# 查看错误日志
grep ERROR log/multi_grid_BN.log

# 查看健康状态
python3 health_check.py
```

### 2. 定期检查

```bash
# 每日检查
./scripts/deploy.sh status
python3 health_check.py

# 每周检查
du -sh log/*.log
find log/ -name "*.log.*" -mtime +7 -delete
```

### 3. 告警设置

```bash
# 设置日志监控
tail -f log/multi_grid_BN.log | grep -E "(ERROR|CRITICAL)"

# 设置状态监控
watch -n 30 'tail -1 log/status_summary.log'
```

---

**注意**: 本测试指南适用于多币种版本的完整功能验证。建议在测试环境充分验证后再部署到生产环境。 