# 网格交易机器人 Docker 部署指南

这是一个支持多交易所的高级网格交易机器人，目前支持 Gate.io 和 Binance，支持双向持仓模式的永续合约交易，具备智能风险控制和价差监控功能。

## ✨ 核心特性

### 🎯 交易策略
- **双向网格交易**: 同时进行多头和空头网格交易，提高市场适应性
- **动态网格调整**: 根据持仓情况和市场价格动态调整网格间距
- **智能开仓/止盈**: 自动识别持仓状态，智能挂单和止盈

### 🛡️ 风险控制
- **持仓阈值管理**: 
  - `POSITION_THRESHOLD`: 极限持仓阈值，超过后暂停新开仓
  - `POSITION_LIMIT`: 持仓监控阈值，触发双倍止盈策略
- **库存风险控制**: 双向持仓达到阈值时自动部分平仓
- **价差监控**: 实时监控买卖单价差，防止网格偏移
- **订单超时管理**: 超过300秒未成交的挂单自动取消

### 📊 实时监控
- **WebSocket 数据流**: 
  - 实时价格更新 (ticker)
  - 持仓变化监控 (positions)
  - 挂单状态更新 (orders)
  - 最佳买卖价监控 (book_ticker)
  - 账户余额变化 (balances)
- **多重数据同步**: REST API + WebSocket 双重确保数据准确性

### 🔧 智能功能
- **价差自动纠正**: 检测到价差超过阈值时自动重新对齐网格
- **订单冷却机制**: 锁仓后设置反向挂单冷却时间
- **精度自适应**: 自动获取交易对的价格和数量精度

## 🚀 快速开始

### 1. 克隆项目
```bash
git clone <your-repo-url>
cd grid-trading-bot
```

### 2. 配置环境变量
```bash
# 复制示例配置文件
cp env.example .env

# 编辑配置文件，填入你的 API 信息
nano .env
```

### 3. 启动机器人
```bash
# 构建并启动
./deploy.sh start

# 或者分步执行
./deploy.sh build    # 构建镜像
./deploy.sh start    # 启动容器
```

## 📋 配置说明

在 `.env` 文件中配置以下重要参数：

### 交易所配置
- `EXCHANGE`: 交易所选择 (gate 或 binance)
- `CONTRACT_TYPE`: 合约类型 (USDT 或 USDC，仅币安需要)

### API 配置 (必填)
- `API_KEY`: API 密钥
- `API_SECRET`: API 私钥

### 交易配置
- `COIN_NAME`: 交易币种 (默认: X)
- `GRID_SPACING`: 网格间距 (默认: 0.004，即 0.4%)
- `INITIAL_QUANTITY`: 初始交易数量 (默认: 1 张)
- `LEVERAGE`: 杠杆倍数 (默认: 20)

### 高级配置 (代码中已优化默认值)
- **风险控制阈值**: 
  - `POSITION_THRESHOLD`: 10 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100 (锁仓阈值)
  - `POSITION_LIMIT`: 5 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100 (持仓数量阈值)
- **时间控制**:
  - `ORDER_COOLDOWN_TIME`: 60秒 (锁仓后的反向挂单冷却时间)
  - `SYNC_TIME`: 3秒 (数据同步间隔)
- **价差监控**:
  - `PRICE_SPREAD_THRESHOLD`: GRID_SPACING * 0.1 (价差阈值：网格间距的10%)
  - `PRICE_SPREAD_CHECK_INTERVAL`: 30秒 (价差检查间隔)

## 🛠️ 管理命令

```bash
# 查看帮助
./deploy.sh help

# 启动服务
./deploy.sh start

# 停止服务
./deploy.sh stop

# 重启服务
./deploy.sh restart

# 查看日志
./deploy.sh logs

# 查看状态
./deploy.sh status

# 构建镜像
./deploy.sh build

# 清理资源
./deploy.sh cleanup
```

## 📊 监控和日志

### 查看实时日志
```bash
./deploy.sh logs
```

### 查看本地日志文件
```bash
# Gate.io 版本
tail -f log/grid_Gate.log

# Binance 版本
tail -f log/grid_BN.log
```

### 关键日志信息
- **配置验证**: 启动时显示配置参数和验证结果
- **持仓更新**: 实时显示多头/空头持仓变化
- **挂单状态**: 显示各类型挂单的数量和状态
- **价差警告**: 当价差超过阈值时的警告信息
- **风险控制**: 库存管理和平仓操作的日志

### 查看容器状态
```bash
./deploy.sh status
```

## 🧠 交易逻辑说明

### 网格策略
1. **初始化**: 
   - 多头持仓为0时，挂出多头开仓单
   - 空头持仓为0时，挂出空头开仓单

2. **持仓管理**:
   - 有持仓时，挂出对应的止盈单和补仓单
   - 持仓超过`POSITION_LIMIT`时，启用双倍止盈策略
   - 持仓超过`POSITION_THRESHOLD`时，暂停新开仓

3. **价差控制**:
   - 定期检查多空网格价格差异
   - 价差超过阈值时自动重新对齐网格
   - 撤销所有挂单并重新布局

4. **风险管控**:
   - 双向持仓同时达到阈值时部分平仓
   - 挂单超时自动取消
   - 冷却机制防止频繁操作

## 🐳 Docker 架构

项目使用以下 Docker 配置：

- **基础镜像**: Python 3.9 Slim
- **运行用户**: 非 root 用户 (trader)
- **资源限制**: 内存 512M, CPU 0.5 核心
- **健康检查**: 每 30 秒检查程序状态
- **自动重启**: 容器异常退出时自动重启

## 📁 目录结构

```
.
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 配置
├── deploy.sh               # 部署和管理脚本
├── requirements.txt        # Python 依赖
├── grid_Gate.py           # 主程序 (Gate.io)
├── grid_BN.py             # 主程序 (Binance)
├── grid_OK_XRP.py         # OKX 版本示例
├── env.example            # 环境变量示例
├── .dockerignore          # Docker 忽略文件
├── log/                   # 日志目录 (持久化)
└── README.md              # 说明文档
```

## ⚠️ 安全注意事项

### 1. API 密钥安全
- **权限设置**: 只开启必要的合约交易权限，禁用提现权限
- **IP 白名单**: 在交易所设置 API 的 IP 白名单
- **密钥保护**: 不要将 `.env` 文件提交到版本控制系统

### 2. 风险控制建议
- **测试环境**: 建议先在测试网或小资金环境运行
- **参数调优**: 根据币种特性调整网格间距和初始数量
- **持仓监控**: 定期检查持仓状况，避免过度集中
- **市场适应**: 在极端市场条件下考虑暂停机器人

### 3. 系统安全
- **网络隔离**: 容器运行在隔离的网络环境中
- **日志管理**: 定期清理日志文件，避免磁盘空间不足
- **权限控制**: 使用非 root 用户运行，降低安全风险

## 🔧 故障排除

### 常见问题

1. **API 连接失败**
   ```bash
   # 检查 API 密钥配置
   grep API_KEY .env
   
   # 查看错误日志
   ./deploy.sh logs
   
   # 检查网络连接
   curl -I https://api.gateio.ws
   curl -I https://fapi.binance.com
   ```

2. **容器启动失败**
   ```bash
   # 检查配置文件
   docker-compose config
   
   # 查看容器状态
   docker ps -a
   
   # 检查资源使用
   docker stats
   ```

3. **权限问题**
   ```bash
   # 检查日志目录权限
   ls -la log/
   
   # 修复权限
   chmod 755 log/
   sudo chown 1000:1000 log/
   ```

4. **价差异常**
   - 检查网络延迟是否过高
   - 确认交易对流动性是否充足
   - 考虑调整 `PRICE_SPREAD_THRESHOLD` 参数

5. **挂单失败**
   - 检查账户余额是否充足
   - 确认杠杆设置是否正确
   - 验证最小下单数量设置

### 日志级别

机器人使用 Python logging 模块，日志级别为 INFO。日志同时输出到：
- 控制台 (容器日志)
- 文件 `log/grid_Gate.log` (Gate.io版本)
- 文件 `log/grid_BN.log` (Binance版本)

### 监控指标
- **持仓状态**: 多头/空头持仓数量
- **挂单状态**: 各类型挂单的数量和价格
- **价差监控**: 买卖单价格差异百分比
- **风险指标**: 持仓是否接近阈值
- **系统状态**: WebSocket 连接状态和数据同步时间

## 🔮 版本支持

本项目还包含其他交易所的版本：

- **Binance**: `grid_BN.py` - 币安合约版本
- **OKX**: `grid_OK_XRP.py` - 欧易合约版本

每个版本都针对相应交易所的 API 特性进行了优化。

## 📞 支持

如遇到问题，请：
1. 检查日志文件获取详细错误信息
2. 确认配置参数是否正确
3. 验证交易所 API 权限设置
4. 查看网络连接和交易所服务状态

### 性能优化建议
- 适当调整 `SYNC_TIME` 以平衡实时性和性能
- 根据服务器性能调整容器资源限制
- 监控内存使用情况，必要时重启容器

---

**⚠️ 免责声明**: 本软件仅供学习和研究使用，使用者需要承担所有交易风险。网格交易在趋势行情中可能面临较大亏损，请根据自身风险承受能力谨慎使用。作者不对任何投资损失负责。

**📈 风险提示**: 
- 网格交易适合震荡行情，单边趋势行情风险较大
- 杠杆交易风险极高，可能导致全部资金损失
- 请确保充分理解交易机制后再使用
- 建议设置止损机制，避免极端情况下的重大损失
