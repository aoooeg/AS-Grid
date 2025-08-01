import asyncio
import websockets
import json
import logging
import hmac
import hashlib
import time
import ccxt
import math
from decimal import Decimal, ROUND_HALF_UP
import os
from dotenv import load_dotenv

# Telegram 通知配置
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # Telegram Bot Token
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # Telegram Chat ID
ENABLE_NOTIFICATIONS = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"  # 是否启用通知
NOTIFICATION_INTERVAL = int(os.getenv("NOTIFICATION_INTERVAL", "3600"))  # 定时汇总通知间隔（秒）

import aiohttp  # 添加这个导入用于发送HTTP请求

# 加载环境变量
load_dotenv()

# ==================== 配置 ====================
# 从环境变量读取重要配置
EXCHANGE = os.getenv("EXCHANGE", "binance")  # 交易所选择
CONTRACT_TYPE = os.getenv("CONTRACT_TYPE", "USDT")  # 合约类型
API_KEY = os.getenv("API_KEY", "")  # 从环境变量获取 API Key
API_SECRET = os.getenv("API_SECRET", "")  # 从环境变量获取 API Secret
COIN_NAME = os.getenv("COIN_NAME", "XRP")  # 交易币种
GRID_SPACING = float(os.getenv("GRID_SPACING", "0.001"))  # 网格间距
INITIAL_QUANTITY = int(os.getenv("INITIAL_QUANTITY", "3"))  # 初始交易数量 (币数量)
LEVERAGE = int(os.getenv("LEVERAGE", "20"))  # 杠杆倍数

# 固定配置（通常不需要修改）
WEBSOCKET_URL = "wss://fstream.binance.com/ws"  # WebSocket URL
POSITION_THRESHOLD = 10 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100  # 锁仓阈值
POSITION_LIMIT = 5 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100  # 持仓数量阈值
ORDER_COOLDOWN_TIME = 60  # 锁仓后的反向挂单冷却时间（秒）
SYNC_TIME = 3  # 同步时间（秒）
ORDER_FIRST_TIME = 2  # 首单间隔时间

# ==================== 日志配置 ====================
# 确保日志目录存在
os.makedirs("log", exist_ok=True)

# 获取当前脚本的文件名（不带扩展名）
script_name = os.path.splitext(os.path.basename(__file__))[0]

# 配置日志处理器
handlers = [logging.StreamHandler()]  # 总是包含控制台输出

# 尝试添加文件处理器
try:
    file_handler = logging.FileHandler(f"log/{script_name}.log")
    handlers.append(file_handler)
    print(f"日志将写入文件: log/{script_name}.log")
except PermissionError as e:
    print(f"警告: 无法创建日志文件 (权限不足): {e}")
    print("日志将只输出到控制台")
except Exception as e:
    print(f"警告: 无法创建日志文件: {e}")
    print("日志将只输出到控制台")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=handlers,
)
logger = logging.getLogger()

# ==================== 配置验证 ====================
def validate_config():
    """验证配置参数"""
    if not API_KEY or not API_SECRET:
        raise ValueError("API_KEY 和 API_SECRET 必须设置")
    
    if GRID_SPACING <= 0 or GRID_SPACING >= 1:
        raise ValueError("GRID_SPACING 必须在 0 到 1 之间")
    
    if INITIAL_QUANTITY <= 0:
        raise ValueError("INITIAL_QUANTITY 必须大于 0")
    
    if LEVERAGE <= 0 or LEVERAGE > 100:
        raise ValueError("LEVERAGE 必须在 1 到 100 之间")
    
    # 验证Telegram配置
    global ENABLE_NOTIFICATIONS
    if ENABLE_NOTIFICATIONS:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("Telegram通知已启用但缺少BOT_TOKEN或CHAT_ID，将禁用通知功能")
            ENABLE_NOTIFICATIONS = False
        else:
            logger.info("Telegram通知功能已启用")
    
    logger.info(f"配置验证通过 - 币种: {COIN_NAME}, 网格间距: {GRID_SPACING}, 初始数量: {INITIAL_QUANTITY}")


class CustomBinance(ccxt.binance):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        return super().fetch(url, method, headers, body)


# ==================== 网格交易机器人 ====================
class GridTradingBot:
    def __init__(self, api_key, api_secret, coin_name, contract_type, grid_spacing, initial_quantity, leverage):
        self.api_key = api_key
        self.api_secret = api_secret
        self.coin_name = coin_name
        self.contract_type = contract_type  # 合约类型：USDT 或 USDC
        self.grid_spacing = grid_spacing
        self.initial_quantity = initial_quantity
        self.leverage = leverage
        self.exchange = self._initialize_exchange()  # 初始化交易所
        self.ccxt_symbol = f"{coin_name}/{contract_type}:{contract_type}"  # 动态生成交易对

        # 获取价格精度{self.price_precision}, 数量精度: {self.amount_precision}, 最小下单数量: {self.min_order_amount}
        self._get_price_precision()

        self.long_initial_quantity = 0  # 多头下单数量
        self.short_initial_quantity = 0  # 空头下单数量
        self.long_position = 0  # 多头持仓 ws监控
        self.short_position = 0  # 空头持仓 ws监控
        self.last_long_order_time = 0  # 上次多头挂单时间
        self.last_short_order_time = 0  # 上次空头挂单时间
        self.buy_long_orders = 0.0  # 多头买入剩余挂单数量
        self.sell_long_orders = 0.0  # 多头卖出剩余挂单数量
        self.sell_short_orders = 0.0  # 空头卖出剩余挂单数量
        self.buy_short_orders = 0.0  # 空头买入剩余挂单数量
        self.last_position_update_time = 0  # 上次持仓更新时间
        self.last_orders_update_time = 0  # 上次订单更新时间
        self.last_ticker_update_time = 0  # ticker 时间限速
        self.latest_price = 0  # 最新价格
        self.best_bid_price = None  # 最佳买价
        self.best_ask_price = None  # 最佳卖价
        self.balance = {}  # 用于存储合约账户余额
        self.mid_price_long = 0  # long 中间价
        self.lower_price_long = 0  # long 网格上
        self.upper_price_long = 0  # long 网格下
        self.mid_price_short = 0  # short 中间价
        self.lower_price_short = 0  # short 网格上
        self.upper_price_short = 0  # short 网格下
        self.listenKey = self.get_listen_key()  # 获取初始 listenKey

        # 检查持仓模式，如果不是双向持仓模式则停止程序
        self.check_and_enable_hedge_mode()
        
        # Telegram通知相关变量
        self.last_summary_time = 0  # 上次汇总通知时间
        self.startup_notified = False  # 是否已发送启动通知
        self.last_balance = None  # 上次余额记录
        
        # 紧急通知状态跟踪
        self.long_threshold_alerted = False  # 多头阈值警告状态
        self.short_threshold_alerted = False  # 空头阈值警告状态
        self.risk_reduction_alerted = False  # 风险减仓警告状态
        
        # 初始化异步锁
        self.lock = asyncio.Lock()


    def _initialize_exchange(self):
        """初始化交易所 API"""
        exchange = CustomBinance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "options": {
                "defaultType": "future",  # 使用永续合约
            },
        })
        # 加载市场数据
        exchange.load_markets(reload=False)
        return exchange

    def _get_price_precision(self):
        """获取交易对的价格精度、数量精度和最小下单数量"""
        markets = self.exchange.fetch_markets()
        symbol_info = next(market for market in markets if market["symbol"] == self.ccxt_symbol)

        # 获取价格精度
        price_precision = symbol_info["precision"]["price"]
        if isinstance(price_precision, float):
            # 如果 price_precision 是浮点数（例如 0.01），计算小数点后的位数
            self.price_precision = int(abs(math.log10(price_precision)))
        elif isinstance(price_precision, int):
            # 如果 price_precision 是整数，直接使用
            self.price_precision = price_precision
        else:
            raise ValueError(f"未知的价格精度类型: {price_precision}")

        # 获取数量精度
        amount_precision = symbol_info["precision"]["amount"]
        if isinstance(amount_precision, float):
            # 如果 amount_precision 是浮点数（例如 0.001），计算小数点后的位数
            self.amount_precision = int(abs(math.log10(amount_precision)))
        elif isinstance(amount_precision, int):
            # 如果 amount_precision 是整数，直接使用
            self.amount_precision = amount_precision
        else:
            raise ValueError(f"未知的数量精度类型: {amount_precision}")

        # 获取最小下单数量
        self.min_order_amount = symbol_info["limits"]["amount"]["min"]

        logger.info(
            f"价格精度: {self.price_precision}, 数量精度: {self.amount_precision}, 最小下单数量: {self.min_order_amount}")

    def get_position(self):
        """获取当前持仓"""
        params = {
            'type': 'future'  # 永续合约
        }
        positions = self.exchange.fetch_positions(params=params)
        # print(positions)
        long_position = 0
        short_position = 0

        for position in positions:
            if position['symbol'] == self.ccxt_symbol:  # 使用动态的 symbol 变量
                contracts = position.get('contracts', 0)  # 获取合约数量
                side = position.get('side', None)  # 获取仓位方向

                # 判断是否为多头或空头
                if side == 'long':  # 多头
                    long_position = contracts
                elif side == 'short':  # 空头
                    short_position = abs(contracts)  # 使用绝对值来计算空头合约数

        # 如果没有持仓，返回 0
        if long_position == 0 and short_position == 0:
            return 0, 0

        return long_position, short_position

    # ==================== Telegram 通知功能 ====================
    async def send_telegram_message(self, message, urgent=False, silent=False):
        """发送Telegram消息"""
        if not ENABLE_NOTIFICATIONS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return

        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            
            # 添加机器人标识和时间戳
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"🤖 **{COIN_NAME}网格机器人** | {timestamp}\n\n{message}"
            
            # 如果是紧急消息，添加特殊标记
            if urgent:
                formatted_message = f"🚨 **紧急通知** 🚨\n\n{formatted_message}"
            elif silent:
                formatted_message = f"🔇 **定时汇总** 🔇\n\n{formatted_message}"
            
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "disable_notification": silent  # 静音发送参数
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    if response.status == 200:
                        notification_type = "静音" if silent else ("紧急" if urgent else "正常")
                    else:
                        logger.warning(f"Telegram消息发送失败: {response.status}")
                        
        except Exception as e:
            logger.error(f"发送Telegram消息失败: {e}")

    async def send_startup_notification(self):
        """发送启动通知"""
        if self.startup_notified:
            return
            
        message = f"""
🚀 **机器人启动成功**

📊 **交易配置**
• 币种: {COIN_NAME}
• 网格间距: {GRID_SPACING:.2%}
• 初始数量: {INITIAL_QUANTITY} 张
• 杠杆倍数: {LEVERAGE}x

🛡️ **风险控制**
• 锁仓阈值: {POSITION_THRESHOLD:.2f}
• 持仓监控阈值: {POSITION_LIMIT:.2f}

✅ 机器人已开始运行，将自动进行网格交易...
"""
        await self.send_telegram_message(message)
        self.startup_notified = True

    async def check_and_notify_position_threshold(self, side, position):
        """检查并通知持仓阈值状态"""
        is_over_threshold = position > POSITION_THRESHOLD
        
        if side == 'long':
            if is_over_threshold and not self.long_threshold_alerted:
                # 首次超过阈值，发送警告
                await self._send_threshold_alert(side, position)
                self.long_threshold_alerted = True
            elif not is_over_threshold and self.long_threshold_alerted:
                # 恢复正常，发送恢复通知
                await self._send_threshold_recovery(side, position)
                self.long_threshold_alerted = False
                
        elif side == 'short':
            if is_over_threshold and not self.short_threshold_alerted:
                # 首次超过阈值，发送警告
                await self._send_threshold_alert(side, position)
                self.short_threshold_alerted = True
            elif not is_over_threshold and self.short_threshold_alerted:
                # 恢复正常，发送恢复通知
                await self._send_threshold_recovery(side, position)
                self.short_threshold_alerted = False
    
    async def _send_threshold_alert(self, side, position):
        """发送持仓超过阈值警告"""
        message = f"""
⚠️ **持仓风险警告**

📍 **{side.upper()}持仓超过极限阈值**
• 当前{side}持仓: {position} 张
• 极限阈值: {POSITION_THRESHOLD:.2f}
• 最新价格: {self.latest_price:.8f}

🛑 **已暂停新开仓，等待持仓回落**
"""
        await self.send_telegram_message(message, urgent=True)
    
    async def _send_threshold_recovery(self, side, position):
        """发送持仓恢复正常通知"""
        message = f"""
✅ **持仓风险解除**

📍 **{side.upper()}持仓已回落至安全区间**
• 当前{side}持仓: {position} 张
• 极限阈值: {POSITION_THRESHOLD:.2f}
• 最新价格: {self.latest_price:.8f}

🟢 **已恢复正常开仓策略**
"""
        await self.send_telegram_message(message, urgent=False)

    async def check_and_notify_risk_reduction(self):
        """检查并通知风险减仓状态"""
        local_position_threshold = int(POSITION_THRESHOLD * 0.8)
        both_over_threshold = (self.long_position >= local_position_threshold and 
                              self.short_position >= local_position_threshold)
        
        if both_over_threshold and not self.risk_reduction_alerted:
            # 首次双向超过阈值，发送警告
            await self._send_risk_reduction_alert()
            self.risk_reduction_alerted = True
        elif not both_over_threshold and self.risk_reduction_alerted:
            # 恢复正常，发送恢复通知
            await self._send_risk_reduction_recovery()
            self.risk_reduction_alerted = False
    
    async def _send_risk_reduction_alert(self):
        """发送风险减仓通知"""
        message = f"""
📉 **库存风险控制**

⚖️ **双向持仓均超过阈值，执行风险减仓**
• 多头持仓: {self.long_position}
• 空头持仓: {self.short_position}
• 阈值: {int(POSITION_THRESHOLD * 0.8)}

✅ 已执行部分平仓减少库存风险
"""
        await self.send_telegram_message(message)
    
    async def _send_risk_reduction_recovery(self):
        """发送风险减仓恢复通知"""
        message = f"""
✅ **库存风险已缓解**

⚖️ **持仓状况已改善**
• 多头持仓: {self.long_position}
• 空头持仓: {self.short_position}
• 监控阈值: {int(POSITION_THRESHOLD * 0.8)}

🟢 **库存风险控制已解除**
"""
        await self.send_telegram_message(message)

    async def get_balance_info(self):
        """获取余额信息 - 同时获取钱包余额和保证金余额"""
        try:
            # 获取合约账户余额
            balance = self.exchange.fetch_balance(params={"type": "future"})
            balance_info = []
            
            # 检查是否有合约账户的详细信息
            if 'info' in balance and 'assets' in balance['info']:
                # 获取合约账户的钱包余额和保证金余额
                for asset in balance['info']['assets']:
                    asset_name = asset['asset']
                    margin_balance = float(asset.get('marginBalance', 0))
                    wallet_balance = float(asset.get('walletBalance', 0))
                    unrealized_pnl = float(asset.get('unrealizedProfit', 0))  # 修正字段名
                    
                    if margin_balance > 0 or wallet_balance > 0:
                        # 显示保证金余额
                        if margin_balance > 0:
                            balance_info.append(f"• {asset_name}保证金: {margin_balance:.2f}")
                        
                        # 显示钱包余额
                        if wallet_balance > 0:
                            balance_info.append(f"• {asset_name}钱包: {wallet_balance:.2f}")
                        
                        # 如果有未实现盈亏，也显示
                        if unrealized_pnl != 0:
                            pnl_sign = "+" if unrealized_pnl > 0 else ""
                            balance_info.append(f"• {asset_name}未实现盈亏: {pnl_sign}{unrealized_pnl:.2f}")
            
            # 如果没有找到合约账户信息，回退到原来的方法
            if not balance_info:
                # 检查 USDT 余额
                if 'USDT' in balance:
                    usdt_balance = balance['USDT']
                    total = usdt_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDT余额: {total:.2f}")
                
                # 检查 USDC 余额
                if 'USDC' in balance:
                    usdc_balance = balance['USDC']
                    total = usdc_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDC余额: {total:.2f}")
                
                # 如果没有找到任何余额，尝试获取所有非零余额
                if not balance_info:
                    for currency, info in balance.items():
                        if isinstance(info, dict) and 'total' in info:
                            total = info.get('total', 0)
                            if total > 0:
                                balance_info.append(f"• {currency}余额: {total:.2f}")
            
            if balance_info:
                return "\n".join(balance_info)
            else:
                return "• 账户余额: 暂无可用余额"
                
        except Exception as e:
            logger.warning(f"获取余额失败: {e}")
            return "• 账户余额: 数据获取中..."

    async def send_summary_notification(self):
        """发送定时汇总通知（静音）"""
        current_time = time.time()
        if current_time - self.last_summary_time < NOTIFICATION_INTERVAL:
            return
            
        # 获取当前余额
        balance_info = await self.get_balance_info()
        
        message = f"""
📊 **运行状态汇总**

💰 **账户信息**
{balance_info}

📈 **持仓情况**
• 多头持仓: {self.long_position} 张
• 空头持仓: {self.short_position} 张

📋 **挂单状态**
• 多头开仓: {self.buy_long_orders} 张
• 多头止盈: {self.sell_long_orders} 张
• 空头开仓: {self.sell_short_orders} 张
• 空头止盈: {self.buy_short_orders} 张

💹 **价格信息**
• 最新价格: {self.latest_price:.8f}
• 最佳买价: {self.best_bid_price:.8f}
• 最佳卖价: {self.best_ask_price:.8f}

🏃‍♂️ 机器人运行正常...
"""
        await self.send_telegram_message(message, urgent=False, silent=True)  # 静音发送
        self.last_summary_time = current_time

    async def send_error_notification(self, error_msg, error_type="运行错误"):
        """发送错误通知"""
        message = f"""
❌ **{error_type}**

🔍 **错误详情**
{error_msg}

⏰ **发生时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}

请检查机器人状态...
"""
        await self.send_telegram_message(message, urgent=True)

    async def monitor_orders(self):
        """监控挂单状态，超过300秒未成交的挂单自动取消"""
        while True:
            try:
                await asyncio.sleep(60)  # 每60秒检查一次
                current_time = time.time()  # 当前时间（秒）
                orders = self.exchange.fetch_open_orders(self.ccxt_symbol)

                if not orders:
                    logger.info("当前没有未成交的挂单")
                    self.buy_long_orders = 0  # 多头买入剩余挂单数量
                    self.sell_long_orders = 0  # 多头卖出剩余挂单数量
                    self.sell_short_orders = 0  # 空头卖出剩余挂单数量
                    self.buy_short_orders = 0  # 空头买入剩余挂单数量
                    continue

                for order in orders:
                    order_id = order['id']
                    order_timestamp = order.get('timestamp')  # 获取订单创建时间戳（毫秒）
                    create_time = float(order['info'].get('create_time', 0))  # 获取订单创建时间（秒）

                    # 优先使用 create_time，如果不存在则使用 timestamp
                    order_time = create_time if create_time > 0 else order_timestamp / 1000

                    if not order_time:
                        logger.warning(f"订单 {order_id} 缺少时间戳，无法检查超时")
                        continue

                    if current_time - order_time > 300:  # 超过300秒未成交
                        logger.info(f"订单 {order_id} 超过300秒未成交，取消挂单")
                        try:
                            self.cancel_order(order_id)
                        except Exception as e:
                            logger.error(f"取消订单 {order_id} 失败: {e}")

            except Exception as e:
                logger.error(f"监控挂单状态失败: {e}")

    def check_orders_status(self):
        """检查当前所有挂单的状态，并更新多头和空头的挂单数量"""
        # 获取当前所有挂单（带 symbol 参数，限制为某个交易对）
        orders = self.exchange.fetch_open_orders(symbol=self.ccxt_symbol)

        # 初始化计数器
        buy_long_orders = 0.0  # 使用浮点数
        sell_long_orders = 0.0  # 使用浮点数
        buy_short_orders = 0.0  # 使用浮点数
        sell_short_orders = 0.0  # 使用浮点数

        for order in orders:
            # 获取订单的原始委托数量（取绝对值）
            orig_quantity = abs(float(order.get('info', {}).get('origQty', 0)))  # 从 info 中获取 origQty
            side = order.get('side')  # 订单方向：buy 或 sell
            position_side = order.get('info', {}).get('positionSide')  # 仓位方向：LONG 或 SHORT

            # 判断订单类型
            if side == 'buy' and position_side == 'LONG':  # 多头买单
                buy_long_orders += orig_quantity
            elif side == 'sell' and position_side == 'LONG':  # 多头卖单
                sell_long_orders += orig_quantity
            elif side == 'buy' and position_side == 'SHORT':  # 空头买单
                buy_short_orders += orig_quantity
            elif side == 'sell' and position_side == 'SHORT':  # 空头卖单
                sell_short_orders += orig_quantity

        # 更新实例变量
        self.buy_long_orders = buy_long_orders
        self.sell_long_orders = sell_long_orders
        self.buy_short_orders = buy_short_orders
        self.sell_short_orders = sell_short_orders

    async def run(self):
        """启动 WebSocket 监听"""
        # 初始化时获取一次持仓数据
        self.long_position, self.short_position = self.get_position()
        # self.last_position_update_time = time.time()
        logger.info(f"初始化持仓: 多头 {self.long_position} 张, 空头 {self.short_position} 张")

        # 等待状态同步完成
        await asyncio.sleep(5)  # 等待 5 秒

        # 初始化时获取一次挂单状态
        self.check_orders_status()
        logger.info(
            f"初始化挂单状态: 多头开仓={self.buy_long_orders}, 多头止盈={self.sell_long_orders}, 空头开仓={self.sell_short_orders}, 空头止盈={self.buy_short_orders}")

        # 发送启动通知
        await self.send_startup_notification()

        # 启动挂单监控任务
        # asyncio.create_task(self.monitor_orders())
        # 启动 listenKey 更新任务
        asyncio.create_task(self.keep_listen_key_alive())

        while True:
            try:
                await self.connect_websocket()
            except Exception as e:
                logger.error(f"WebSocket 连接失败: {e}")
                await self.send_error_notification(str(e), "WebSocket连接失败")
                await asyncio.sleep(5)  # 等待 5 秒后重试

    async def connect_websocket(self):
        """连接 WebSocket 并订阅 ticker 和持仓数据"""
        async with websockets.connect(WEBSOCKET_URL) as websocket:
            # 订阅 ticker 数据
            await self.subscribe_ticker(websocket)
            # 订阅挂单数据
            await self.subscribe_orders(websocket)
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    # print(data)
                    if data.get("e") == "bookTicker":
                        await self.handle_ticker_update(message)
                    elif data.get("e") == "ORDER_TRADE_UPDATE":  # 处理挂单更新
                        await self.handle_order_update(message)
                except Exception as e:
                    logger.error(f"WebSocket 消息处理失败: {e}")
                    break

    async def subscribe_ticker(self, websocket):
        """订阅 ticker 数据"""
        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{self.coin_name.lower()}{self.contract_type.lower()}@bookTicker"],
            "id": 1
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送 ticker 订阅请求: {payload}")

    async def subscribe_orders(self, websocket):
        """订阅挂单数据"""
        if not self.listenKey:
            logger.error("listenKey 为空，无法订阅订单更新")
            return

        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{self.listenKey}"],  # 使用 self.listenKey 订阅
            "id": 3
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送挂单订阅请求: {payload}")

    def get_listen_key(self):
        """获取 listenKey"""
        try:
            response = self.exchange.fapiPrivatePostListenKey()
            listenKey = response.get("listenKey")
            if not listenKey:
                raise ValueError("获取的 listenKey 为空")
            logger.info(f"成功获取 listenKey: {listenKey}")
            return listenKey
        except Exception as e:
            logger.error(f"获取 listenKey 失败: {e}")
            raise e

    async def keep_listen_key_alive(self):
        """定期更新 listenKey"""
        while True:
            try:
                await asyncio.sleep(1800)  # 每 30 分钟更新一次
                self.exchange.fapiPrivatePutListenKey()
                self.listenKey = self.get_listen_key()  # 更新 self.listenKey
                logger.info(f"listenKey 已更新: {self.listenKey}")
            except Exception as e:
                logger.error(f"更新 listenKey 失败: {e}")
                await asyncio.sleep(60)  # 等待 60 秒后重试

    def _generate_sign(self, message):
        """生成 HMAC-SHA256 签名"""
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    async def handle_ticker_update(self, message):
        current_time = time.time()
        if current_time - self.last_ticker_update_time < 0.5:  # 500ms
            return  # 跳过本次更新

        self.last_ticker_update_time = current_time
        """处理 ticker 更新"""
        data = json.loads(message)
        if data.get("e") == "bookTicker":  # Binance 的 bookTicker 事件
            best_bid_price = data.get("b")
            best_ask_price = data.get("a")

            # 校验字段是否存在且有效
            if best_bid_price is None or best_ask_price is None:
                logger.warning("bookTicker 消息中缺少最佳买价或最佳卖价")
                return

            try:
                self.best_bid_price = float(best_bid_price)  # 最佳买价
                self.best_ask_price = float(best_ask_price)  # 最佳卖价
                self.latest_price = (self.best_bid_price + self.best_ask_price) / 2  # 最新价格
                # logger.info(
                #     f"最新价格: {self.latest_price}, 最佳买价: {self.best_bid_price}, 最佳卖价: {self.best_ask_price}")
            except ValueError as e:
                logger.error(f"解析价格失败: {e}")

            # 检查持仓状态是否过时
            if time.time() - self.last_position_update_time > SYNC_TIME:  # 超过 60 秒未更新
                self.long_position, self.short_position = self.get_position()
                self.last_position_update_time = time.time()

            # 检查持仓状态是否过时
            if time.time() - self.last_orders_update_time > SYNC_TIME:  # 超过 60 秒未更新
                self.check_orders_status()
                self.last_orders_update_time = time.time()

            await self.adjust_grid_strategy()
            
            # 发送定时汇总通知
            await self.send_summary_notification()

    async def handle_order_update(self, message):
        async with self.lock:
            """处理订单更新和持仓更新"""
            data = json.loads(message)
            # print(f"收到消息: {data}")  # 打印原始数据

            if data.get("e") == "ORDER_TRADE_UPDATE":  # 处理订单更新
                order = data.get("o", {})
                symbol = order.get("s")  # 交易对
                if symbol == f"{self.coin_name}{self.contract_type}":  # 匹配交易对
                    side = order.get("S")  # 订单方向：BUY 或 SELL
                    position_side = order.get("ps")  # 仓位方向：LONG 或 SHORT
                    reduce_only = order.get("R")  # 是否为平仓单
                    status = order.get("X")  # 订单状态
                    quantity = float(order.get("q", 0))  # 订单数量
                    filled = float(order.get("z", 0))  # 已成交数量
                    remaining = quantity - filled  # 剩余数量

                    if status == "NEW":
                        if side == "BUY":
                            if position_side == "LONG":  # 多头开仓单
                                self.buy_long_orders += remaining
                            elif position_side == "SHORT":  # 空头止盈单
                                self.buy_short_orders += remaining
                        elif side == "SELL":
                            if position_side == "LONG":  # 多头止盈单
                                self.sell_long_orders += remaining
                            elif position_side == "SHORT":  # 空头开仓单
                                self.sell_short_orders += remaining
                    elif status == "FILLED":  # 订单已成交
                        if side == "BUY":
                            if position_side == "LONG":  # 多头开仓单
                                self.long_position += filled  # 更新多头持仓
                                self.buy_long_orders = max(0.0, self.buy_long_orders - filled)  # 更新挂单状态
                            elif position_side == "SHORT":  # 空头止盈单
                                self.short_position = max(0.0, self.short_position - filled)  # 更新空头持仓
                                self.buy_short_orders = max(0.0, self.buy_short_orders - filled)  # 更新挂单状态
                        elif side == "SELL":
                            if position_side == "LONG":  # 多头止盈单
                                self.long_position = max(0.0, self.long_position - filled)  # 更新多头持仓
                                self.sell_long_orders = max(0.0, self.sell_long_orders - filled)  # 更新挂单状态
                            elif position_side == "SHORT":  # 空头开仓单
                                self.short_position += filled  # 更新空头持仓
                                self.sell_short_orders = max(0.0, self.sell_short_orders - filled)  # 更新挂单状态
                    elif status == "CANCELED":  # 订单已取消
                        if side == "BUY":
                            if position_side == "LONG":  # 多头开仓单
                                self.buy_long_orders = max(0.0, self.buy_long_orders - quantity)
                            elif position_side == "SHORT":  # 空头止盈单
                                self.buy_short_orders = max(0.0, self.buy_short_orders - quantity)
                        elif side == "SELL":
                            if position_side == "LONG":  # 多头止盈单
                                self.sell_long_orders = max(0.0, self.sell_long_orders - quantity)
                            elif position_side == "SHORT":  # 空头开仓单
                                self.sell_short_orders = max(0.0, self.sell_short_orders - quantity)

                    # # 打印当前挂单状态
                    # logger.info(
                    #     f"挂单状态: 多头开仓={self.buy_long_orders}, 多头止盈={self.sell_long_orders}, 空头开仓={self.sell_short_orders}, 空头止盈={self.buy_short_orders}")
                    # # 打印当前持仓状态
                    # logger.info(f"持仓状态: 多头={self.long_position}, 空头={self.short_position}")

    def get_take_profit_quantity(self, position, side):
        # print(side)

        """调整止盈单的交易数量"""
        if side == 'long':
            if position > POSITION_LIMIT:
                # logger.info(f"持仓过大超过阈值{POSITION_LIMIT}, {side}双倍止盈止损")
                self.long_initial_quantity = self.initial_quantity * 2

            # 如果 short 锁仓 long 两倍
            elif self.short_position >= POSITION_THRESHOLD:
                self.long_initial_quantity = self.initial_quantity * 2
            else:
                self.long_initial_quantity = self.initial_quantity

        elif side == 'short':
            if position > POSITION_LIMIT:
                # logger.info(f"持仓过大超过阈值{POSITION_LIMIT}, {side}双倍止盈止损")
                self.short_initial_quantity = self.initial_quantity * 2

            # 如果 long 锁仓 short 两倍
            elif self.long_position >= POSITION_THRESHOLD:
                self.short_initial_quantity = self.initial_quantity * 2
            else:
                self.short_initial_quantity = self.initial_quantity

    async def initialize_long_orders(self):
        # 检查上次挂单时间，确保 10 秒内不重复挂单
        current_time = time.time()
        if current_time - self.last_long_order_time < ORDER_FIRST_TIME:
            logger.info(f"距离上次多头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        # # 检查是否有未成交的挂单
        # orders = self.exchange.fetch_open_orders(self.ccxt_symbol)
        # if any(order['side'] == 'buy' and order['info'].get('positionSide') == 'LONG' for order in orders):
        #     logger.info("发现未成交的多头补仓单，跳过撤销和挂单")
        #     return

        self.cancel_orders_for_side('long')

        # 挂出多头开仓单
        self.place_order('buy', self.best_bid_price, self.initial_quantity, False, 'long')
        logger.info(f"挂出多头开仓单: 买入 @ {self.latest_price}")

        # 更新上次多头挂单时间
        self.last_long_order_time = time.time()
        logger.info("初始化多头挂单完成")

    async def initialize_short_orders(self):
        # 检查上次挂单时间，确保 10 秒内不重复挂单
        current_time = time.time()
        if current_time - self.last_short_order_time < ORDER_FIRST_TIME:
            logger.info(f"距离上次空头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        # 撤销所有空头挂单
        self.cancel_orders_for_side('short')

        # 挂出空头开仓单
        self.place_order('sell', self.best_ask_price, self.initial_quantity, False, 'short')
        logger.info(f"挂出空头开仓单: 卖出 @ {self.latest_price}")

        # 更新上次空头挂单时间
        self.last_short_order_time = time.time()
        logger.info("初始化空头挂单完成")

    def cancel_orders_for_side(self, position_side):
        """撤销某个方向的所有挂单"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)

        if len(orders) == 0:
            logger.info("没有找到挂单")
        else:
            try:
                for order in orders:
                    # 获取订单的方向和仓位方向
                    side = order.get('side')  # 订单方向：buy 或 sell
                    reduce_only = order.get('reduceOnly', False)  # 是否为平仓单
                    position_side_order = order.get('info', {}).get('positionSide', 'BOTH')  # 仓位方向：LONG 或 SHORT

                    if position_side == 'long':
                        # 如果是多头开仓订单：买单且 reduceOnly 为 False
                        if not reduce_only and side == 'buy' and position_side_order == 'LONG':
                            # logger.info("发现多头开仓挂单，准备撤销")
                            self.cancel_order(order['id'])  # 撤销该订单
                        # 如果是多头止盈订单：卖单且 reduceOnly 为 True
                        elif reduce_only and side == 'sell' and position_side_order == 'LONG':
                            # logger.info("发现多头止盈挂单，准备撤销")
                            self.cancel_order(order['id'])  # 撤销该订单

                    elif position_side == 'short':
                        # 如果是空头开仓订单：卖单且 reduceOnly 为 False
                        if not reduce_only and side == 'sell' and position_side_order == 'SHORT':
                            # logger.info("发现空头开仓挂单，准备撤销")
                            self.cancel_order(order['id'])  # 撤销该订单
                        # 如果是空头止盈订单：买单且 reduceOnly 为 True
                        elif reduce_only and side == 'buy' and position_side_order == 'SHORT':
                            # logger.info("发现空头止盈挂单，准备撤销")
                            self.cancel_order(order['id'])  # 撤销该订单
            except ccxt.OrderNotFound as e:
                logger.warning(f"订单 {order['id']} 不存在，无需撤销: {e}")
                self.check_orders_status()  # 强制更新挂单状态
            except Exception as e:
                logger.error(f"撤单失败: {e}")

    def cancel_order(self, order_id):
        """撤单"""
        try:
            self.exchange.cancel_order(order_id, self.ccxt_symbol)
            # logger.info(f"撤销挂单成功, 订单ID: {order_id}")
        except ccxt.BaseError as e:
            logger.error(f"撤单失败: {e}")

    def place_order(self, side, price, quantity, is_reduce_only=False, position_side=None, order_type='limit'):
        """挂单函数，增加双向持仓支持"""
        try:
            # 修正数量精度并确保不低于最小下单数量
            quantity = round(quantity, self.amount_precision)
            quantity = max(quantity, self.min_order_amount)

            # 生成唯一的 ClientOrderId
            import uuid
            client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"

            # 如果是市价单，不需要价格参数
            if order_type == 'market':
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()  # Binance 要求大写：LONG 或 SHORT
                order = self.exchange.create_order(self.ccxt_symbol, 'market', side, quantity, params=params)
                return order
            else:
                # 检查 price 是否为 None
                if price is None:
                    logger.error("限价单必须提供 price 参数")
                    return None

                # 修正价格精度
                price = round(price, self.price_precision)

                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()  # Binance 要求大写：LONG 或 SHORT
                order = self.exchange.create_order(self.ccxt_symbol, 'limit', side, quantity, price, params)
                return order

        except ccxt.BaseError as e:
            logger.error(f"下单报错: {e}")
            return None

    def place_take_profit_order(self, ccxt_symbol, side, price, quantity):
        # print('止盈单价格', price)
        # 检查是否已有相同价格的挂单
        orders = self.exchange.fetch_open_orders(ccxt_symbol)
        for order in orders:
            if (
                    order['info'].get('positionSide') == side.upper()
                    and float(order['price']) == price
                    and order['side'] == ('sell' if side == 'long' else 'buy')
            ):
                logger.info(f"已存在相同价格的 {side} 止盈单，跳过挂单")
                return
        """挂止盈单（双仓模式）"""
        try:
            # 检查持仓
            if side == 'long' and self.long_position <= 0:
                logger.warning("没有多头持仓，跳过挂出多头止盈单")
                return
            elif side == 'short' and self.short_position <= 0:
                logger.warning("没有空头持仓，跳过挂出空头止盈单")
                return
            # 修正价格精度
            price = round(price, self.price_precision)

            # 修正数量精度并确保不低于最小下单数量
            quantity = round(quantity, self.amount_precision)
            quantity = max(quantity, self.min_order_amount)

            if side == 'long':
                # 卖出多头仓位止盈，应该使用 close_long 来平仓
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'LONG'
                }
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'sell', quantity, price, params)
                logger.info(f"成功挂 long 止盈单: 卖出 {quantity} {ccxt_symbol} @ {price}")
            elif side == 'short':
                # 买入空头仓位止盈，应该使用 close_short 来平仓
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'buy', quantity, price, {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'SHORT'
                })
                logger.info(f"成功挂 short 止盈单: 买入 {quantity} {ccxt_symbol} @ {price}")
        except ccxt.BaseError as e:
            logger.error(f"挂止盈单失败: {e}")

    async def place_long_orders(self, latest_price):
        """挂多头订单"""
        try:
            self.get_take_profit_quantity(self.long_position, 'long')
            if self.long_position > 0:
                # print('多头持仓', self.long_position)
                # 检查持仓是否超过阈值
                if self.long_position > POSITION_THRESHOLD:
                    logger.info(f"持仓{self.long_position}超过极限阈值 {POSITION_THRESHOLD}，long装死")
                    if self.sell_long_orders <= 0:
                        # 防止除零错误
                        if self.short_position > 0:
                            r = float((self.long_position / self.short_position) / 100 + 1)
                        else:
                            r = 1.01  # 默认值
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.latest_price * r,
                                                     self.long_initial_quantity)  # 挂止盈
                else:
                    # 更新中间价
                    self.update_mid_price('long', latest_price)
                    self.cancel_orders_for_side('long')
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.upper_price_long,
                                                 self.long_initial_quantity)  # 挂止盈
                    self.place_order('buy', self.lower_price_long, self.long_initial_quantity, False, 'long')  # 挂补仓
                    logger.info("挂多头止盈，挂多头补仓")

        except Exception as e:
            logger.error(f"挂多头订单失败: {e}")

    async def place_short_orders(self, latest_price):
        """挂空头订单"""
        try:
            self.get_take_profit_quantity(self.short_position, 'short')
            if self.short_position > 0:
                # 检查持仓是否超过阈值
                if self.short_position > POSITION_THRESHOLD:
                    logger.info(f"持仓{self.short_position}超过极限阈值 {POSITION_THRESHOLD}，short 装死")
                    if self.buy_short_orders <= 0:
                        # 防止除零错误
                        if self.long_position > 0:
                            r = float((self.short_position / self.long_position) / 100 + 1)
                        else:
                            r = 1.01  # 默认值
                        logger.info("发现多头止盈单缺失。。需要补止盈单")
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.latest_price * r,
                                                     self.short_initial_quantity)  # 挂止盈

                else:
                    # 更新中间价
                    self.update_mid_price('short', latest_price)
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.lower_price_short,
                                                 self.short_initial_quantity)  # 挂止盈
                    self.place_order('sell', self.upper_price_short, self.short_initial_quantity, False, 'short')  # 挂补仓
                    logger.info("挂空头止盈，挂空头补仓")

        except Exception as e:
            logger.error(f"挂空头订单失败: {e}")

    def check_and_enable_hedge_mode(self):
        """检查并启用双向持仓模式，如果切换失败则停止程序"""
        try:
            # 尝试获取当前持仓模式
            try:
                position_mode = self.exchange.fetch_position_mode(symbol=self.ccxt_symbol)
                if not position_mode['hedged']:
                    # 如果当前不是双向持仓模式，尝试启用双向持仓模式
                    logger.info("当前不是双向持仓模式，尝试自动启用双向持仓模式...")
                    self.enable_hedge_mode()
                    logger.info("双向持仓模式已成功启用，程序继续运行。")
                else:
                    logger.info("当前已是双向持仓模式，程序继续运行。")
            except AttributeError:
                # 如果 fetch_position_mode 方法不存在，直接尝试启用双向持仓模式
                logger.info("无法检查当前持仓模式，尝试启用双向持仓模式...")
                self.enable_hedge_mode()
                logger.info("双向持仓模式已启用，程序继续运行。")
            except Exception as e:
                logger.warning(f"检查持仓模式时出现异常: {e}")
                # 继续运行，不强制停止程序
                logger.info("程序将继续运行，请确保已在币安手动启用双向持仓模式")
                
        except Exception as e:
            # 检查是否是"已经启用"的错误
            if "No need to change position side" in str(e):
                logger.info("双向持仓模式已经启用，程序继续运行。")
            else:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式后再运行程序")
                raise e  # 抛出异常，停止程序

    def enable_hedge_mode(self):
        """启用双向持仓模式"""
        try:
            # 使用 ccxt 的 fapiPrivatePostPositionSideDual 函数
            params = {
                'dualSidePosition': 'true',  # 启用双向持仓模式
            }
            response = self.exchange.fapiPrivatePostPositionSideDual(params)
            logger.info(f"启用双向持仓模式: {response}")
        except AttributeError:
            # 如果方法不存在，尝试使用其他方式
            try:
                # 尝试使用 fapiPrivatePostPositionSideDual 的替代方法
                response = self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
                logger.info(f"启用双向持仓模式: {response}")
            except Exception as e:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式")
                raise e
        except Exception as e:
            # 检查是否是"已经启用"的错误
            if "No need to change position side" in str(e):
                logger.info("双向持仓模式已经启用，无需切换")
                return
            else:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式")
                raise e  # 抛出异常，停止程序

    async def check_and_reduce_positions(self):
        """检查持仓并减少库存风险"""

        # 检查并通知风险减仓状态
        await self.check_and_notify_risk_reduction()

        # 设置持仓阈值
        local_position_threshold = POSITION_THRESHOLD * 0.8  # 阈值的 80%

        # 设置平仓数量
        quantity = POSITION_THRESHOLD * 0.1  # 阈值的 10%

        if self.long_position >= local_position_threshold and self.short_position >= local_position_threshold:
            logger.info(f"多头和空头持仓均超过阈值 {local_position_threshold}，开始双向平仓，减少库存风险")
            # 平仓多头（使用市价单）
            if self.long_position > 0:
                self.place_order('sell', price=None, quantity=quantity, is_reduce_only=True, position_side='long',
                                 order_type='market')
                logger.info(f"市价平仓多头 {quantity} 个")

            # 平仓空头（使用市价单）
            if self.short_position > 0:
                self.place_order('buy', price=None, quantity=quantity, is_reduce_only=True, position_side='short',
                                 order_type='market')
                logger.info(f"市价平仓空头 {quantity} 个")

    def update_mid_price(self, side, price):
        """更新中间价"""
        if side == 'long':
            self.mid_price_long = price  # 更新多头中间价
            # 计算上下网格价格 加上价格精度，price_precision
            self.upper_price_long = self.mid_price_long * (1 + self.grid_spacing)
            self.lower_price_long = self.mid_price_long * (1 - self.grid_spacing)
            logger.info("更新 long 中间价")

        elif side == 'short':
            self.mid_price_short = price  # 更新空头中间价
            # 计算上下网格价格
            self.upper_price_short = self.mid_price_short * (1 + self.grid_spacing)
            self.lower_price_short = self.mid_price_short * (1 - self.grid_spacing)
            logger.info("更新 short 中间价")

    # ==================== 策略逻辑 ====================
    async def adjust_grid_strategy(self):
        """根据最新价格和持仓调整网格策略"""
        # 检查持仓阈值状态并发送通知
        await self.check_and_notify_position_threshold('long', self.long_position)
        await self.check_and_notify_position_threshold('short', self.short_position)
        
        # 检查双向仓位库存，如果同时达到，就统一部分平仓减少库存风险，提高保证金使用率
        await self.check_and_reduce_positions()
        


        # # order推流不准没解决，rest请求确认下
        # if (self.buy_long_orders != INITIAL_QUANTITY or self.sell_long_orders != INITIAL_QUANTITY or self.sell_short_orders != INITIAL_QUANTITY or self.buy_short_orders != INITIAL_QUANTITY):
        #     self.buy_long_orders, self.sell_long_orders, self.sell_short_orders, self.buy_short_orders = self.check_orders_status()
        #
        # print('ticker的挂单状态', self.buy_long_orders, self.sell_long_orders, self.sell_short_orders,
        #       self.buy_short_orders)

        current_time = time.time()
        # 检测多头持仓
        if self.long_position == 0:
            logger.info(f"检测到没有多头持仓{self.long_position}，初始化多头挂单@ ticker")
            await self.initialize_long_orders()
        else:
            if not (0 < self.buy_long_orders <= self.long_initial_quantity) or not (0 < self.sell_long_orders <= self.long_initial_quantity):
                if self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"距离上次 long 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 long 挂单@ ticker")
                else:
                    await self.place_long_orders(self.latest_price)

        # 检测空头持仓
        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (0 < self.sell_short_orders <= self.short_initial_quantity) or not (0 < self.buy_short_orders <= self.short_initial_quantity):
                if self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"距离上次 short 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 short 挂单@ ticker")
                else:
                    await self.place_short_orders(self.latest_price)


# ==================== 主程序 ====================
async def main():
    try:
        # 验证配置
        validate_config()
        
        # 创建并启动交易机器人
        bot = GridTradingBot(API_KEY, API_SECRET, COIN_NAME, CONTRACT_TYPE, GRID_SPACING, INITIAL_QUANTITY, LEVERAGE)
        logger.info("网格交易机器人启动中...")
        await bot.run()
        
    except ValueError as e:
        logger.error(f"配置错误: {e}")
        # 发送配置错误通知
        bot = GridTradingBot(API_KEY, API_SECRET, COIN_NAME, CONTRACT_TYPE, GRID_SPACING, INITIAL_QUANTITY, LEVERAGE)
        await bot.send_error_notification(str(e), "配置错误")
        exit(1)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭机器人...")
        # 发送停止通知
        if 'bot' in locals():
            await bot.send_telegram_message("🛑 **机器人已手动停止**\n\n用户主动停止了网格交易机器人", urgent=False, silent=True)
    except Exception as e:
        logger.error(f"运行时错误: {e}")
        # 发送运行错误通知
        if 'bot' in locals():
            await bot.send_error_notification(str(e), "运行时错误")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
