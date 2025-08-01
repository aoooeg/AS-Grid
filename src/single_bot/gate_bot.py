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
EXCHANGE = os.getenv("EXCHANGE", "gate")  # 交易所选择
API_KEY = os.getenv("API_KEY", "")  # 从环境变量获取 API Key
API_SECRET = os.getenv("API_SECRET", "")  # 从环境变量获取 API Secret
COIN_NAME = os.getenv("COIN_NAME", "X")  # 交易币种
GRID_SPACING = float(os.getenv("GRID_SPACING", "0.004"))  # 网格间距
INITIAL_QUANTITY = int(os.getenv("INITIAL_QUANTITY", "1"))  # 初始交易数量 (张数)
LEVERAGE = int(os.getenv("LEVERAGE", "20"))  # 杠杆倍数

# 固定配置（通常不需要修改）
WEBSOCKET_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"  # WebSocket URL
POSITION_THRESHOLD = 10 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100  # 锁仓阈值
POSITION_LIMIT = 5 * INITIAL_QUANTITY / GRID_SPACING * 2 / 100  # 持仓数量阈值
ORDER_COOLDOWN_TIME = 60  # 锁仓后的反向挂单冷却时间（秒）
SYNC_TIME = 3  # 同步时间（秒）
ORDER_FIRST_TIME = 1  # 首单间隔时间


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


class CustomGate(ccxt.gate):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        headers['X-Gate-Channel-Id'] = 'laohuoji'
        headers['Accept'] = 'application/json'
        headers['Content-Type'] = 'application/json'
        return super().fetch(url, method, headers, body)


# ==================== 网格交易机器人 ====================
class GridTradingBot:
    def __init__(self, api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage):
        self.api_key = api_key
        self.api_secret = api_secret
        self.coin_name = coin_name
        self.grid_spacing = grid_spacing
        self.initial_quantity = initial_quantity
        self.leverage = leverage
        self.exchange = self._initialize_exchange()  # 初始化交易所
        self.ccxt_symbol = f"{coin_name}/USDT:USDT"  # CCXT 格式的交易对
        self.ws_symbol = f"{coin_name}_USDT"  # WebSocket 格式的交易对
        self.price_precision = self._get_price_precision()  # 价格精度

        self.long_initial_quantity = 0  # 多头下单数量
        self.short_initial_quantity = 0  # 空头下单数量
        self.long_position = 0  # 多头持仓 ws监控
        self.short_position = 0  # 空头持仓 ws监控
        self.last_long_order_time = 0  # 上次多头挂单时间
        self.last_short_order_time = 0  # 上次空头挂单时间
        self.buy_long_orders = 0  # 多头买入剩余挂单数量
        self.sell_long_orders = 0  # 多头卖出剩余挂单数量
        self.sell_short_orders = 0  # 空头卖出剩余挂单数量
        self.buy_short_orders = 0  # 空头买入剩余挂单数量
        self.last_position_update_time = 0  # 上次持仓更新时间
        self.last_orders_update_time = 0  # 上次订单更新时间
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
        

        
        # Telegram通知相关变量
        self.last_summary_time = 0  # 上次汇总通知时间
        self.startup_notified = False  # 是否已发送启动通知
        self.last_balance = None  # 上次余额记录
        
        # 紧急通知状态跟踪
        self.long_threshold_alerted = False  # 多头阈值警告状态
        self.short_threshold_alerted = False  # 空头阈值警告状态
        self.risk_reduction_alerted = False  # 风险减仓警告状态

    def _initialize_exchange(self):
        """初始化交易所 API"""
        exchange = CustomGate({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "options": {
                "defaultType": "future",  # 使用永续合约
            },
        })
        return exchange

    def _get_price_precision(self):
        """获取交易对的价格精度"""
        markets = self.exchange.fetch_markets()
        symbol_info = next(market for market in markets if market["symbol"] == self.ccxt_symbol)
        return int(-math.log10(float(symbol_info["precision"]["price"])))

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
        """获取余额信息"""
        # 首先尝试使用WebSocket数据
        if "USDT" in self.balance and self.balance["USDT"]:
            balance_amount = self.balance["USDT"].get("balance", 0)
            balance_change = self.balance["USDT"].get("change", 0)
            if balance_amount > 0:
                balance_info = f"• USDT余额: {balance_amount:.2f}"
                if balance_change != 0:
                    balance_info += f" (变化: {balance_change:+.2f})"
                return balance_info
        
        # 如果WebSocket数据不可用，使用REST API获取
        try:
            balance = self.exchange.fetch_balance()
            if 'USDT' in balance:
                usdt_balance = balance['USDT']
                total = usdt_balance.get('total', 0)
                if total > 0:
                    return f"• USDT余额: {total:.2f} (REST)"
        except Exception as e:
            logger.warning(f"获取REST余额失败: {e}")
        
        return "• USDT余额: 数据获取中..."

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

    def get_position(self):
        """获取当前持仓"""
        params = {
            'settle': 'usdt',  # 设置结算货币为 USDT
            'type': 'swap'  # 永续合约
        }
        positions = self.exchange.fetch_positions(params=params)
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
        """检查当前所有挂单的状态"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)  # 获取所有未成交订单
        # print(orders)
        buy_long_orders_count = 0
        sell_long_orders_count = 0
        sell_short_orders_count = 0
        buy_short_orders_count = 0

        for order in orders:
            # 确保 order 中有 'info' 字段，并且 'info' 中有 'left' 字段
            if not order.get('info') or 'left' not in order['info']:
                continue  # 跳过不符合条件的订单

            # 获取未成交数量，如果 'left' 不存在则默认为 '0'
            left_amount = abs(float(order['info'].get('left', '0')))

            # 如果是多头止盈订单：卖单且仓位方向是多头的平仓单
            if order.get('reduceOnly') == True and order.get('side') == 'sell' and order.get('status') == 'open':
                sell_long_orders_count = left_amount
            # 如果是空头买入挂单：买单且仓位方向是空头的平仓单（空头止盈）
            elif order.get('reduceOnly') == True and order.get('side') == 'buy' and order.get('status') == 'open':
                buy_short_orders_count = left_amount
            # 如果是多头开仓订单：买单且 reduceOnly 为 False（开多仓）
            elif order.get('reduceOnly') == False and order.get('side') == 'buy' and order.get('status') == 'open':
                buy_long_orders_count = left_amount
            # 如果是空头开仓订单：卖单且 reduceOnly 为 False（开空仓）
            elif order.get('reduceOnly') == False and order.get('side') == 'sell' and order.get('status') == 'open':
                sell_short_orders_count = left_amount

        return buy_long_orders_count, sell_long_orders_count, sell_short_orders_count, buy_short_orders_count

    async def run(self):
        """启动 WebSocket 监听"""
        # 初始化时获取一次持仓数据
        self.long_position, self.short_position = self.get_position()
        # self.last_position_update_time = time.time()
        logger.info(f"初始化持仓: 多头 {self.long_position} 张, 空头 {self.short_position} 张")

        # 初始化时获取一次挂单状态
        self.buy_long_orders, self.sell_long_orders, self.sell_short_orders, self.buy_short_orders = self.check_orders_status()
        logger.info(
            f"初始化挂单状态: 多头开仓={self.buy_long_orders}, 多头止盈={self.sell_long_orders}, 空头开仓={self.sell_short_orders}, 空头止盈={self.buy_short_orders}")

        # 发送启动通知
        await self.send_startup_notification()

        # 启动挂单监控任务
        # asyncio.create_task(self.monitor_orders())

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
            await self.subscribe_ticker(websocket)
            await self.subscribe_positions(websocket)
            await self.subscribe_orders(websocket)  # 订阅挂单更新
            await self.subscribe_book_ticker(websocket)  # 订阅 book_ticker
            await self.subscribe_balances(websocket)  # 订阅余额
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    
                    # 添加调试信息，记录所有收到的channel类型
                    channel = data.get("channel", "unknown")
                    event = data.get("event", "unknown")
                    
                    if data.get("channel") == "futures.tickers":
                        await self.handle_ticker_update(message)
                    elif data.get("channel") == "futures.positions":
                        await self.handle_position_update(message)
                    elif data.get("channel") == "futures.orders":  # 处理挂单更新
                        await self.handle_order_update(message)
                    elif data.get("channel") == "futures.book_ticker":  # 处理 book_ticker 更新
                        await self.handle_book_ticker_update(message)
                    elif data.get("channel") == "futures.balances":  # 处理余额更新
                        await self.handle_balance_update(message)
                    else:
                        # 记录未处理的消息类型（只记录前几次避免日志过多）
                        if not hasattr(self, '_unknown_channels'):
                            self._unknown_channels = set()
                        if channel not in self._unknown_channels and len(self._unknown_channels) < 5:
                            logger.debug(f"收到未处理的消息类型: channel={channel}, event={event}")
                            self._unknown_channels.add(channel)
                            
                except Exception as e:
                    logger.error(f"WebSocket 消息处理失败: {e}")
                    break

    async def subscribe_balances(self, websocket):
        """订阅合约账户余额频道"""
        current_time = int(time.time())
        message = f"channel=futures.balances&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.balances",
            "event": "subscribe",
            "payload": [],  # 订阅所有余额，空数组表示订阅所有
            "auth": {
                "method": "api_key",
                "KEY": self.api_key,
                "SIGN": sign,
            },
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送余额订阅请求: {payload}")

    async def subscribe_ticker(self, websocket):
        """订阅 ticker 数据"""
        current_time = int(time.time())
        message = f"channel=futures.tickers&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {
                "method": "api_key",
                "KEY": self.api_key,
                "SIGN": sign,
            },
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送 ticker 订阅请求: {payload}")

    async def subscribe_book_ticker(self, websocket):
        """订阅 book_ticker 数据"""
        current_time = int(time.time())
        message = f"channel=futures.book_ticker&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.book_ticker",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {
                "method": "api_key",
                "KEY": self.api_key,
                "SIGN": sign,
            },
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送 book_ticker 订阅请求: {payload}")

    async def subscribe_orders(self, websocket):
        """订阅挂单更新频道"""
        current_time = int(time.time())
        message = f"channel=futures.orders&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.orders",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {
                "method": "api_key",
                "KEY": self.api_key,
                "SIGN": sign,
            },
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送挂单订阅请求: {payload}")

    async def handle_balance_update(self, message):
        """处理余额更新"""
        try:
            data = json.loads(message)
            if data.get("channel") == "futures.balances" and data.get("event") == "update":
                balances = data.get("result", [])
                if not balances:
                    logger.debug("收到空的余额更新数据")
                    return
                    
                for balance in balances:
                    currency = balance.get("currency", "UNKNOWN")  # 币种，默认值为 "UNKNOWN"
                    balance_amount = float(balance.get("balance", 0))  # 余额最终数量，默认值为 0
                    change = float(balance.get("change", 0))  # 余额变化数量，默认值为 0
                    text = balance.get("text", "")  # 附带信息，默认值为空字符串
                    balance_time = balance.get("time", 0)  # 时间（秒），默认值为 0
                    balance_time_ms = balance.get("time_ms", 0)  # 时间（毫秒），默认值为 0
                    balance_type = balance.get("type", "UNKNOWN")  # 类型，默认值为 "UNKNOWN"
                    user = balance.get("user", "UNKNOWN")  # 用户 ID，默认值为 "UNKNOWN"

                    # 更新余额数据（统一转换为大写格式以便查找）
                    currency_upper = currency.upper()
                    self.balance[currency_upper] = {
                        "balance": balance_amount,
                        "change": change,
                        "text": text,
                        "time": balance_time,
                        "time_ms": balance_time_ms,
                        "type": balance_type,
                        "user": user,
                    }
                    # 也保留原始格式以防需要
                    if currency != currency_upper:
                        self.balance[currency] = self.balance[currency_upper]
                    logger.info(
                        f"余额更新: 币种={currency}, 余额={balance_amount}, 变化={change}"
                    )
            else:
                logger.debug(f"忽略非余额更新消息: channel={data.get('channel')}, event={data.get('event')}")
        except Exception as e:
            logger.error(f"处理余额更新失败: {e}, 消息: {message}")


    async def subscribe_positions(self, websocket):
        """订阅持仓数据"""
        current_time = int(time.time())
        message = f"channel=futures.positions&event=subscribe&time={current_time}"
        sign = self._generate_sign(message)
        payload = {
            "time": current_time,
            "channel": "futures.positions",
            "event": "subscribe",
            "payload": [self.ws_symbol],
            "auth": {
                "method": "api_key",
                "KEY": self.api_key,
                "SIGN": sign,
            },
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送持仓订阅请求: {payload}")

    def _generate_sign(self, message):
        """生成 HMAC-SHA512 签名"""
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha512).hexdigest()

    async def handle_ticker_update(self, message):
        """处理 ticker 更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            self.latest_price = float(data["result"][0]["last"])
            print(f"最新价格: {self.latest_price:.8f}")

            # 检查持仓状态是否过时
            if time.time() - self.last_position_update_time > SYNC_TIME:  # 超过 60 秒未更新
                self.long_position, self.short_position = self.get_position()
                self.last_position_update_time = time.time()
                print(f"同步 position: 多头 {self.long_position} 张, 空头 {self.short_position} 张 @ ticker")

            # 检查持仓状态是否过时
            if time.time() - self.last_orders_update_time > SYNC_TIME:  # 超过 60 秒未更新
                self.buy_long_orders, self.sell_long_orders, self.sell_short_orders, self.buy_short_orders = self.check_orders_status()
                self.last_orders_update_time = time.time()
                print(f"同步 orders: 多头买单 {self.buy_long_orders} 张, 多头卖单 {self.sell_long_orders} 张,空头卖单 {self.sell_short_orders} 张, 空头买单 {self.buy_short_orders} 张 @ ticker")

            await self.adjust_grid_strategy()
            
            # 发送定时汇总通知
            await self.send_summary_notification()

    async def handle_book_ticker_update(self, message):
        """处理 book_ticker 更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            ticker_data = data["result"]
            # print('book数据', ticker_data)
            if len(ticker_data) > 0:
                ticker = ticker_data
                self.best_bid_price = float(ticker.get("b", 0))  # 最佳买价
                self.best_ask_price = float(ticker.get("a", 0))  # 最佳卖价
                # logger.info(f"最佳买价: {self.best_bid_price}, 最佳卖价: {self.best_ask_price}")

    async def handle_position_update(self, message):
        """处理持仓更新"""
        data = json.loads(message)
        if data.get("event") == "update":
            position_data = data["result"]
            if isinstance(position_data, list) and len(position_data) > 0:
                position = position_data[0]
                if position.get("mode") == "dual_long":
                    self.long_position = abs(float(position.get("size", 0)))  # 更新多头持仓
                    logger.info(f"更新多头持仓: {self.long_position}")
                    # 直接用ticker 数据监控挂单是否有就好了，不需要监控持仓
                    # await self.handle_long_position_update(position)
                else:
                    self.short_position = abs(float(position.get("size", 0)))  # 更新空头持仓
                    logger.info(f"更新空头持仓: {self.short_position}")
                    # await self.handle_short_position_update(position)
                # self.last_position_update_time = time.time()  # 更新持仓时间

    async def handle_order_update(self, message):
        """处理挂单更新"""
        data = json.loads(message)
        # print(data)
        if data.get("event") == "update":
            order_data = data["result"]
            if isinstance(order_data, list) and len(order_data) > 0:
                for order in order_data:
                    # 检查是否包含必要字段
                    if 'is_reduce_only' not in order or 'size' not in order:
                        logger.warning(f"订单 {order.get('id')} 缺少必要字段，跳过处理")
                        continue

                    # 从 order 中提取 size 和 is_reduce_only
                    size = order.get('size', 0)
                    is_reduce_only = order.get('is_reduce_only', False)

                    # 根据 size 和 is_reduce_only 推断订单类型
                    if size > 0:  # 买入
                        if is_reduce_only:
                            order_type = "多头止盈"  # 买入平仓（平掉多头仓位）
                            self.buy_short_orders = abs(order.get('left', 0))  # 空头止盈是买入
                        else:
                            order_type = "多头开仓"  # 买入开仓（建立多头仓位）
                            self.buy_long_orders = abs(order.get('left', 0))
                    else:  # 卖出
                        if is_reduce_only:
                            order_type = "空头止盈"  # 卖出平仓（平掉空头仓位）
                            self.sell_long_orders = abs(order.get('left', 0))  # 多头止盈是卖出
                        else:
                            order_type = "空头开仓"  # 卖出开仓（建立空头仓位）
                            self.sell_short_orders = abs(order.get('left', 0))

                    # print(
                        # f"订单推: {order_type}, size: {size}{is_reduce_only}, left={order.get('left', 0)}")
                    # self.last_orders_update_time = time.time()  # 更新挂单时间

    # async def adjust_long_strategy(self, long_position):
    #     """根据多头持仓调整策略"""
    #
    #     if long_position == 0:
    #         logger.info("多头持仓为 0，初始化多头挂单")
    #         await self.initialize_long_orders()
    #     else:
    #         logger.info("多头持仓不为 0，撤单并重新挂单")
    #         await self.place_long_orders(self.latest_price)
    #
    # async def adjust_short_strategy(self, short_position):
    #     """根据空头持仓调整策略"""
    #
    #     if short_position == 0:
    #         logger.info("空头持仓为 0，初始化空头挂单")
    #         await self.initialize_short_orders()
    #     else:
    #         logger.info("空头持仓不为 0，撤单并重新挂单")
    #         await self.place_short_orders(self.latest_price)

    def get_take_profit_quantity(self, position, side):
        # print(side)

        """调整止盈单的交易数量"""
        if side == 'long' and POSITION_LIMIT < position:
            # logger.info(f"持仓过大超过阈值{POSITION_LIMIT}, {side}双倍止盈止损")
            self.long_initial_quantity = self.initial_quantity * 2

        elif side == 'short' and POSITION_LIMIT < position:
            # logger.info(f"持仓过大超过阈值{POSITION_LIMIT}, {side}双倍止盈止损")
            self.short_initial_quantity = self.initial_quantity * 2

        else:
            self.long_initial_quantity = self.initial_quantity
            self.short_initial_quantity = self.initial_quantity

    async def initialize_long_orders(self):
        # 检查上次挂单时间，确保 10 秒内不重复挂单
        current_time = time.time()
        if current_time - self.last_long_order_time < ORDER_FIRST_TIME:
            logger.info(f"距离上次多头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        self.cancel_orders_for_side('long')

        # 挂出多头开仓单
        self.place_order('buy', (self.best_bid_price + self.best_ask_price) / 2, self.initial_quantity, False, 'long')
        logger.info(f"挂出多头开仓单: 买入 @ {self.latest_price}")

        # 更新上次多头挂单时间
        self.last_long_order_time = time.time()
        logger.info("初始化多头挂单完成")

    async def initialize_short_orders(self):
        # 检查上次挂单时间，确保 10 秒内不重复挂单
        current_time = time.time()
        if current_time - self.last_short_order_time < ORDER_FIRST_TIME:
            print(f"距离上次空头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        # 撤销所有空头挂单
        self.cancel_orders_for_side('short')

        # 挂出空头开仓单
        self.place_order('sell', (self.best_bid_price + self.best_ask_price) / 2, self.initial_quantity, False, 'short')
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
            for order in orders:
                if position_side == 'long':
                    # 如果是多头开仓订单：买单且 reduceOnly 为 False
                    if order['reduceOnly'] == False and order['side'] == 'buy' and order['status'] == 'open':
                        # logger.info("发现多头开仓挂单，准备撤销")
                        self.cancel_order(order['id'])  # 撤销该订单
                    # 如果是多头止盈订单：卖单且仓位方向是多头的平仓单
                    elif order['reduceOnly'] == True and order['side'] == 'sell' and order['status'] == 'open':
                        # logger.info("发现多头止盈挂单，准备撤销")
                        self.cancel_order(order['id'])  # 撤销该订单

                elif position_side == 'short':
                    # 如果是空头开仓订单：卖单且 reduceOnly 为 False
                    if order['reduceOnly'] == False and order['side'] == 'sell' and order['status'] == 'open':
                        # logger.info("发现空头开仓挂单，准备撤销")
                        self.cancel_order(order['id'])  # 撤销该订单
                    # 如果是空头止盈订单：买单且仓位方向是空头的平仓单
                    elif order['reduceOnly'] == True and order['side'] == 'buy' and order['status'] == 'open':
                        # logger.info("发现空头止盈挂单，准备撤销")
                        self.cancel_order(order['id'])  # 撤销该订单

    def cancel_order(self, order_id):
        """撤单"""
        try:
            self.exchange.cancel_order(order_id, self.ccxt_symbol)
            # logger.info(f"撤销挂单成功, 订单ID: {order_id}")
        except ccxt.BaseError as e:
            logger.error(f"撤单失败: {e}")

    def place_order(self, side, price, quantity, is_reduce_only=False, position_side=None):
        """挂单函数，增加双向持仓支持"""
        try:
            params = {
                # 'tif': 'poc',
                'reduce_only': is_reduce_only,
                # 'position_side': position_side,  # 'long' 或 'short'
            }
            order = self.exchange.create_order(self.ccxt_symbol, 'limit', side, quantity, price, params)
            # logger.info(
            #     f"挂单成功: {side} {quantity} {self.ccxt_symbol} @ {price}, reduceOnly={is_reduce_only}, position_side={position_side}")
            return order
        except ccxt.BaseError as e:
            logger.error(f"下单报错: {e}")
            return None

    def place_take_profit_order(self, ccxt_symbol, side, price, quantity):
        """挂止盈单（双仓模式）"""
        try:
            if side == 'long':
                # 卖出多头仓位止盈，应该使用 close_long 来平仓
                params = {
                    'reduce_only': True,
                }
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'sell', quantity, price, params)
                logger.info(f"成功挂 long 止盈单: 卖出 {quantity} {ccxt_symbol} @ {price}")
            elif side == 'short':
                # 买入空头仓位止盈，应该使用 close_short 来平仓
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'buy', quantity, price, {
                    'reduce_only': True,
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
                    print(f"持仓{self.long_position}超过极限阈值 {POSITION_THRESHOLD}，long装死")
                    # 检查并发送持仓阈值通知
                    await self.check_and_notify_position_threshold('long', self.long_position)
                    # print('多头止盈单', self.sell_long_orders)
                    if self.sell_long_orders <= 0:
                        r = float((int(self.long_position / self.short_position) / 100) + 1)
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.latest_price * r,
                                                     self.long_initial_quantity)  # 挂止盈
                else:
                    # 检查上次挂单时间，确保 60 秒内不重复挂单
                    # print(f"持仓没超过库存阈值")
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
                    print(f"持仓{self.short_position}超过极限阈值 {POSITION_THRESHOLD}，short 装死")
                    # 检查并发送持仓阈值通知
                    await self.check_and_notify_position_threshold('short', self.short_position)
                    if self.buy_short_orders <= 0:
                        r = float((int(self.short_position / self.long_position) / 100) + 1)
                        logger.info("发现多头止盈单缺失。。需要补止盈单")
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.latest_price * r,
                                                     self.short_initial_quantity)  # 挂止盈
                    # self.cancel_orders_for_side('short')
                    #
                else:
                    # 更新中间价
                    self.update_mid_price('short', latest_price)
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.lower_price_short,
                                                 self.short_initial_quantity)  # 挂止盈
                    self.place_order('sell', self.upper_price_short, self.short_initial_quantity, False, 'short')  # 挂补仓
                    # logger.info("挂空头止盈，挂空头补仓")

        except Exception as e:
            logger.error(f"挂空头订单失败: {e}")

    async def check_and_reduce_positions(self):
        """检查持仓并减少库存风险"""

        # 设置持仓阈值（保持原逻辑）
        local_position_threshold = int(POSITION_THRESHOLD * 0.8)  # 阈值的 80%

        # 设置平仓数量（保持原逻辑）
        REDUCE_QUANTITY = int(POSITION_THRESHOLD * 0.1)  # 阈值的 10%

        # 检查并通知风险减仓状态（新增通知逻辑）
        await self.check_and_notify_risk_reduction()

        # 保持原本的判断和执行逻辑
        if self.long_position >= local_position_threshold and self.short_position >= local_position_threshold:
            logger.info(f"多头和空头持仓均超过阈值 {local_position_threshold}，开始双向平仓，减少库存风险")

            # 平仓多头
            if self.long_position > 0:
                self.place_order('sell', self.latest_price, REDUCE_QUANTITY, True, 'long')
                logger.info(f"平仓多头 {REDUCE_QUANTITY} 张")

            # 平仓空头
            if self.short_position > 0:
                self.place_order('buy', self.latest_price, REDUCE_QUANTITY, True, 'short')
                logger.info(f"平仓空头 {REDUCE_QUANTITY} 张")

    def update_mid_price(self, side, price):
        """更新中间价"""
        if side == 'long':
            self.mid_price_long = price  # 更新多头中间价
            # 计算上下网格价格 加上价格精度，price_precision
            self.upper_price_long = self.mid_price_long * (1 + self.grid_spacing)
            self.lower_price_long = self.mid_price_long * (1 - self.grid_spacing)
            print("更新 long 中间价")

        elif side == 'short':
            self.mid_price_short = price  # 更新空头中间价
            # 计算上下网格价格
            self.upper_price_short = self.mid_price_short * (1 + self.grid_spacing)
            self.lower_price_short = self.mid_price_short * (1 - self.grid_spacing)
            print("更新 short 中间价")



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
            print(f"检测到没有多头持仓{self.long_position}，初始化多头挂单@ ticker")
            await self.initialize_long_orders()
        else:
            if not (0 < self.buy_long_orders <= self.long_initial_quantity) or not (0 < self.sell_long_orders <= self.long_initial_quantity):
                if self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
                    print(f"距离上次 long 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 long 挂单@ ticker")
                else:
                    await self.place_long_orders(self.latest_price)

        # 检测空头持仓
        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (0 < self.sell_short_orders <= self.short_initial_quantity) or not (0 < self.buy_short_orders <= self.short_initial_quantity):
                if self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
                    print(f"距离上次 short 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 short 挂单@ ticker")
                else:
                    await self.place_short_orders(self.latest_price)


# ==================== 主程序 ====================
async def main():
    try:
        # 验证配置
        validate_config()
        
        # 创建并启动交易机器人
        bot = GridTradingBot(API_KEY, API_SECRET, COIN_NAME, GRID_SPACING, INITIAL_QUANTITY, LEVERAGE)
        logger.info("网格交易机器人启动中...")
        await bot.run()
        
    except ValueError as e:
        logger.error(f"配置错误: {e}")
        # 发送配置错误通知
        bot = GridTradingBot(API_KEY, API_SECRET, COIN_NAME, GRID_SPACING, INITIAL_QUANTITY, LEVERAGE)
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
