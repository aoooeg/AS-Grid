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
import aiohttp

# 加载环境变量
load_dotenv()

# Telegram 通知配置
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_NOTIFICATIONS = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
NOTIFICATION_INTERVAL = int(os.getenv("NOTIFICATION_INTERVAL", "3600"))

# 固定配置
WEBSOCKET_URL = "wss://fstream.binance.com/ws"
ORDER_COOLDOWN_TIME = 60
SYNC_TIME = 3
ORDER_FIRST_TIME = 1

# 日志配置
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
    script_name = os.path.splitext(os.path.basename(__file__))[0]
    log_filename = f"{script_name}.log"

handlers = [logging.StreamHandler()]
try:
    file_handler = logging.FileHandler(f"log/{log_filename}")
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


class CustomBinance(ccxt.binance):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        return super().fetch(url, method, headers, body)


class BinanceGridBot:
    def __init__(self, symbol, api_key, api_secret, config):
        """
        初始化 BinanceGridBot
        
        Args:
            symbol: 交易对符号 (如 "XRPUSDT")
            api_key: API密钥
            api_secret: API密钥
            config: 配置字典，包含以下键:
                - grid_spacing: 网格间距
                - initial_quantity: 初始交易数量
                - leverage: 杠杆倍数
                - contract_type: 合约类型 (USDT/USDC)
        """
        self.symbol = symbol
        self.api_key = api_key
        self.api_secret = api_secret
        self.config = config
        
        # 从配置中提取参数
        self.grid_spacing = config.get('grid_spacing', 0.001)
        self.initial_quantity = config.get('initial_quantity', 3)
        self.leverage = config.get('leverage', 20)
        self.contract_type = config.get('contract_type', 'USDT')
        
        # 计算阈值
        self.position_threshold = 10 * self.initial_quantity / self.grid_spacing * 2 / 100
        self.position_limit = 5 * self.initial_quantity / self.grid_spacing * 2 / 100
        
        # 初始化交易所
        self.exchange = self._init_exchange()
        self.ccxt_symbol = f"{symbol.replace('USDT', '').replace('USDC', '')}/{self.contract_type}:{self.contract_type}"
        
        # 获取价格精度
        self._get_price_precision()
        
        # 初始化状态变量
        self.long_initial_quantity = 0
        self.short_initial_quantity = 0
        self.long_position = 0
        self.short_position = 0
        self.last_long_order_time = 0
        self.last_short_order_time = 0
        self.buy_long_orders = 0.0
        self.sell_long_orders = 0.0
        self.sell_short_orders = 0.0
        self.buy_short_orders = 0.0
        self.last_position_update_time = 0
        self.last_orders_update_time = 0
        self.last_ticker_update_time = 0
        self.latest_price = 0
        self.best_bid_price = None
        self.best_ask_price = None
        self.balance = {}
        self.mid_price_long = 0
        self.lower_price_long = 0
        self.upper_price_long = 0
        self.mid_price_short = 0
        self.lower_price_short = 0
        self.upper_price_short = 0
        self.listenKey = self._get_listen_key()
        
        # 检查持仓模式
        self._check_and_enable_hedge_mode()
        
        # Telegram通知相关变量
        self.last_summary_time = 0
        self.startup_notified = False
        self.last_balance = None
        
        # 紧急通知状态跟踪
        self.long_threshold_alerted = False
        self.short_threshold_alerted = False
        self.risk_reduction_alerted = False
        
        # 双倍止盈止损通知状态跟踪
        self.long_double_profit_alerted = False
        self.short_double_profit_alerted = False
        
        # 初始化异步锁（延迟创建，避免在没有事件循环时创建）
        self.lock = None
        
        # 运行状态
        self.running = False

    def _init_exchange(self):
        """初始化交易所 API"""
        exchange = CustomBinance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "options": {
                "defaultType": "future",
            },
        })
        exchange.load_markets(reload=False)
        return exchange

    def _get_price_precision(self):
        """获取交易对的价格精度、数量精度和最小下单数量"""
        markets = self.exchange.fetch_markets()
        symbol_info = next(market for market in markets if market["symbol"] == self.ccxt_symbol)

        # 获取价格精度
        price_precision = symbol_info["precision"]["price"]
        if isinstance(price_precision, float):
            self.price_precision = int(abs(math.log10(price_precision)))
        elif isinstance(price_precision, int):
            self.price_precision = price_precision
        else:
            raise ValueError(f"未知的价格精度类型: {price_precision}")

        # 获取数量精度
        amount_precision = symbol_info["precision"]["amount"]
        if isinstance(amount_precision, float):
            self.amount_precision = int(abs(math.log10(amount_precision)))
        elif isinstance(amount_precision, int):
            self.amount_precision = amount_precision
        else:
            raise ValueError(f"未知的数量精度类型: {amount_precision}")

        # 获取最小下单数量
        self.min_order_amount = symbol_info["limits"]["amount"]["min"]

        logger.info(
            f"价格精度: {self.price_precision}, 数量精度: {self.amount_precision}, 最小下单数量: {self.min_order_amount}")

    def _get_position(self):
        """获取当前持仓"""
        params = {
            'type': 'future'
        }
        positions = self.exchange.fetch_positions(params=params)
        long_position = 0
        short_position = 0

        for position in positions:
            if position['symbol'] == self.ccxt_symbol:
                contracts = position.get('contracts', 0)
                side = position.get('side', None)

                if side == 'long':
                    long_position = contracts
                elif side == 'short':
                    short_position = abs(contracts)

        if long_position == 0 and short_position == 0:
            return 0, 0

        return long_position, short_position

    def _get_listen_key(self):
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

    def _check_and_enable_hedge_mode(self):
        """检查并启用双向持仓模式"""
        try:
            try:
                position_mode = self.exchange.fetch_position_mode(symbol=self.ccxt_symbol)
                if not position_mode['hedged']:
                    logger.info("当前不是双向持仓模式，尝试自动启用双向持仓模式...")
                    self._enable_hedge_mode()
                    logger.info("双向持仓模式已成功启用，程序继续运行。")
                else:
                    logger.info("当前已是双向持仓模式，程序继续运行。")
            except AttributeError:
                logger.info("无法检查当前持仓模式，尝试启用双向持仓模式...")
                self._enable_hedge_mode()
                logger.info("双向持仓模式已启用，程序继续运行。")
            except Exception as e:
                logger.warning(f"检查持仓模式时出现异常: {e}")
                logger.info("程序将继续运行，请确保已在币安手动启用双向持仓模式")
                
        except Exception as e:
            if "No need to change position side" in str(e):
                logger.info("双向持仓模式已经启用，程序继续运行。")
            else:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式后再运行程序")
                raise e

    def _enable_hedge_mode(self):
        """启用双向持仓模式"""
        try:
            params = {
                'dualSidePosition': 'true',
            }
            response = self.exchange.fapiPrivatePostPositionSideDual(params)
            logger.info(f"启用双向持仓模式: {response}")
        except AttributeError:
            try:
                response = self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
                logger.info(f"启用双向持仓模式: {response}")
            except Exception as e:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式")
                raise e
        except Exception as e:
            if "No need to change position side" in str(e):
                logger.info("双向持仓模式已经启用，无需切换")
                return
            else:
                logger.error(f"启用双向持仓模式失败: {e}")
                logger.error("请手动在币安交易所启用双向持仓模式")
                raise e

    async def _send_telegram_message(self, message, urgent=False, silent=False):
        """发送Telegram消息"""
        if not ENABLE_NOTIFICATIONS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return

        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"🤖 **{self.symbol}网格机器人** | {timestamp}\n\n{message}"
            
            if urgent:
                formatted_message = f"🚨 **紧急通知** 🚨\n\n{formatted_message}"
            elif silent:
                formatted_message = f"🔇 **定时汇总** 🔇\n\n{formatted_message}"
            
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "disable_notification": silent
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    if response.status == 200:
                        notification_type = "静音" if silent else ("紧急" if urgent else "正常")
                    else:
                        logger.warning(f"Telegram消息发送失败: {response.status}")
                        
        except Exception as e:
            logger.error(f"发送Telegram消息失败: {e}")

    async def _send_startup_notification(self):
        """发送启动通知"""
        if self.startup_notified:
            return
            
        message = f"""
🚀 **机器人启动成功**

📊 **交易配置**
• 币种: {self.symbol}
• 网格间距: {self.grid_spacing:.2%}
• 初始数量: {self.initial_quantity} 张
• 杠杆倍数: {self.leverage}x

🛡️ **风险控制**
• 锁仓阈值: {self.position_threshold:.2f}
• 持仓监控阈值: {self.position_limit:.2f}

✅ 机器人已开始运行，将自动进行网格交易...
"""
        await self._send_telegram_message(message)
        self.startup_notified = True

    async def _check_and_notify_position_threshold(self, side, position):
        """检查并通知持仓阈值状态"""
        is_over_threshold = position > self.position_threshold
        
        if side == 'long':
            if is_over_threshold and not self.long_threshold_alerted:
                await self._send_threshold_alert(side, position)
                self.long_threshold_alerted = True
            elif not is_over_threshold and self.long_threshold_alerted:
                await self._send_threshold_recovery(side, position)
                self.long_threshold_alerted = False
                
        elif side == 'short':
            if is_over_threshold and not self.short_threshold_alerted:
                await self._send_threshold_alert(side, position)
                self.short_threshold_alerted = True
            elif not is_over_threshold and self.short_threshold_alerted:
                await self._send_threshold_recovery(side, position)
                self.short_threshold_alerted = False
    
    async def _send_threshold_alert(self, side, position):
        """发送持仓超过阈值警告"""
        message = f"""
⚠️ **持仓风险警告**

📍 **{side.upper()}持仓超过极限阈值**
• 当前{side}持仓: {position} 张
• 极限阈值: {self.position_threshold:.2f}
• 最新价格: {self.latest_price:.8f}

🛑 **已暂停新开仓，等待持仓回落**
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_threshold_recovery(self, side, position):
        """发送持仓恢复正常通知"""
        message = f"""
✅ **持仓风险解除**

📍 **{side.upper()}持仓已回落至安全区间**
• 当前{side}持仓: {position} 张
• 极限阈值: {self.position_threshold:.2f}
• 最新价格: {self.latest_price:.8f}

🟢 **已恢复正常开仓策略**
"""
        await self._send_telegram_message(message, urgent=False)

    async def _check_and_notify_risk_reduction(self):
        """检查并通知风险减仓状态"""
        local_position_threshold = int(self.position_threshold * 0.8)
        both_over_threshold = (self.long_position >= local_position_threshold and 
                              self.short_position >= local_position_threshold)
        
        if both_over_threshold and not self.risk_reduction_alerted:
            await self._send_risk_reduction_alert()
            self.risk_reduction_alerted = True
        elif not both_over_threshold and self.risk_reduction_alerted:
            await self._send_risk_reduction_recovery()
            self.risk_reduction_alerted = False
    
    async def _send_risk_reduction_alert(self):
        """发送风险减仓通知"""
        message = f"""
📉 **库存风险控制**

⚖️ **双向持仓均超过阈值，执行风险减仓**
• 多头持仓: {self.long_position}
• 空头持仓: {self.short_position}
• 阈值: {int(self.position_threshold * 0.8)}

✅ 已执行部分平仓减少库存风险
"""
        await self._send_telegram_message(message)
    
    async def _send_risk_reduction_recovery(self):
        """发送风险减仓恢复通知"""
        message = f"""
✅ **库存风险已缓解**

⚖️ **持仓状况已改善**
• 多头持仓: {self.long_position}
• 空头持仓: {self.short_position}
• 监控阈值: {int(self.position_threshold * 0.8)}

🟢 **库存风险控制已解除**
"""
        await self._send_telegram_message(message)

    async def _check_and_notify_double_profit(self, side, position):
        """检查并通知双倍止盈止损状态"""
        is_over_limit = position > self.position_limit
        
        if side == 'long':
            if is_over_limit and not self.long_double_profit_alerted:
                await self._send_double_profit_alert(side, position)
                self.long_double_profit_alerted = True
            elif not is_over_limit and self.long_double_profit_alerted:
                await self._send_double_profit_recovery(side, position)
                self.long_double_profit_alerted = False
                
        elif side == 'short':
            if is_over_limit and not self.short_double_profit_alerted:
                await self._send_double_profit_alert(side, position)
                self.short_double_profit_alerted = True
            elif not is_over_limit and self.short_double_profit_alerted:
                await self._send_double_profit_recovery(side, position)
                self.short_double_profit_alerted = False
    
    async def _send_double_profit_alert(self, side, position):
        """发送双倍止盈止损启用通知"""
        message = f"""
📈 **双倍止盈止损启用**

📍 **{side.upper()}持仓超过监控阈值**
• 当前{side}持仓: {position} 张
• 监控阈值: {self.position_limit:.2f}
• 最新价格: {self.latest_price:.8f}

⚡ **已启用双倍止盈止损策略**
• 止盈数量: {self.initial_quantity * 2} 张
• 止损数量: {self.initial_quantity * 2} 张

🔄 **策略说明**
• 当持仓超过监控阈值时，系统自动启用双倍止盈止损
• 加快持仓减少速度，降低风险敞口
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_double_profit_recovery(self, side, position):
        """发送双倍止盈止损恢复正常通知"""
        message = f"""
✅ **双倍止盈止损已解除**

📍 **{side.upper()}持仓已回落至安全区间**
• 当前{side}持仓: {position} 张
• 监控阈值: {self.position_limit:.2f}
• 最新价格: {self.latest_price:.8f}

🟢 **已恢复正常止盈止损策略**
• 止盈数量: {self.initial_quantity} 张
• 止损数量: {self.initial_quantity} 张

📊 **策略说明**
• 持仓已回落至监控阈值以下
• 系统已切换回标准止盈止损策略
"""
        await self._send_telegram_message(message, urgent=False)

    async def _get_balance_info(self):
        """获取余额信息"""
        try:
            balance = self.exchange.fetch_balance(params={"type": "future"})
            balance_info = []
            
            if 'info' in balance and 'assets' in balance['info']:
                for asset in balance['info']['assets']:
                    asset_name = asset['asset']
                    margin_balance = float(asset.get('marginBalance', 0))
                    wallet_balance = float(asset.get('walletBalance', 0))
                    unrealized_pnl = float(asset.get('unrealizedProfit', 0))
                    
                    if margin_balance > 0 or wallet_balance > 0:
                        if margin_balance > 0:
                            balance_info.append(f"• {asset_name}保证金: {margin_balance:.2f}")
                        
                        if wallet_balance > 0:
                            balance_info.append(f"• {asset_name}钱包: {wallet_balance:.2f}")
                        
                        if unrealized_pnl != 0:
                            pnl_sign = "+" if unrealized_pnl > 0 else ""
                            balance_info.append(f"• {asset_name}未实现盈亏: {pnl_sign}{unrealized_pnl:.2f}")
            
            if not balance_info:
                if 'USDT' in balance:
                    usdt_balance = balance['USDT']
                    total = usdt_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDT余额: {total:.2f}")
                
                if 'USDC' in balance:
                    usdc_balance = balance['USDC']
                    total = usdc_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDC余额: {total:.2f}")
                
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

    async def _send_summary_notification(self):
        """发送定时汇总通知（静音）"""
        current_time = time.time()
        if current_time - self.last_summary_time < NOTIFICATION_INTERVAL:
            return
            
        balance_info = await self._get_balance_info()
        
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
        await self._send_telegram_message(message, urgent=False, silent=True)
        self.last_summary_time = current_time

    async def _send_error_notification(self, error_msg, error_type="运行错误"):
        """发送错误通知"""
        message = f"""
❌ **{error_type}**

🔍 **错误详情**
{error_msg}

⏰ **发生时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}

请检查机器人状态...
"""
        await self._send_telegram_message(message, urgent=True)

    def _check_orders_status(self):
        """检查当前所有挂单的状态，并更新多头和空头的挂单数量"""
        orders = self.exchange.fetch_open_orders(symbol=self.ccxt_symbol)

        buy_long_orders = 0.0
        sell_long_orders = 0.0
        buy_short_orders = 0.0
        sell_short_orders = 0.0

        for order in orders:
            orig_quantity = abs(float(order.get('info', {}).get('origQty', 0)))
            side = order.get('side')
            position_side = order.get('info', {}).get('positionSide')

            if side == 'buy' and position_side == 'LONG':
                buy_long_orders += orig_quantity
            elif side == 'sell' and position_side == 'LONG':
                sell_long_orders += orig_quantity
            elif side == 'buy' and position_side == 'SHORT':
                buy_short_orders += orig_quantity
            elif side == 'sell' and position_side == 'SHORT':
                sell_short_orders += orig_quantity

        self.buy_long_orders = buy_long_orders
        self.sell_long_orders = sell_long_orders
        self.buy_short_orders = buy_short_orders
        self.sell_short_orders = sell_short_orders

    async def _keep_listen_key_alive(self):
        """定期更新 listenKey"""
        while self.running:
            try:
                await asyncio.sleep(1800)  # 每 30 分钟更新一次
                self.exchange.fapiPrivatePutListenKey()
                self.listenKey = self._get_listen_key()
                logger.info(f"listenKey 已更新: {self.listenKey}")
            except Exception as e:
                logger.error(f"更新 listenKey 失败: {e}")
                await asyncio.sleep(60)

    async def _connect_websocket(self):
        """连接 WebSocket 并订阅 ticker 和持仓数据"""
        try:
            async with websockets.connect(WEBSOCKET_URL) as websocket:
                await self._subscribe_ticker(websocket)
                await self._subscribe_orders(websocket)
                logger.info("WebSocket 连接成功，开始接收消息")
                while self.running:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        if data.get("e") == "bookTicker":
                            await self._handle_ticker_update(message)
                        elif data.get("e") == "ORDER_TRADE_UPDATE":
                            await self._handle_order_update(message)
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("WebSocket 连接已关闭，尝试重新连接...")
                        break
                    except Exception as e:
                        logger.error(f"WebSocket 消息处理失败: {e}")
                        break
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            raise e

    async def _subscribe_ticker(self, websocket):
        """订阅 ticker 数据"""
        coin_name = self.symbol.replace('USDT', '').replace('USDC', '')
        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{coin_name.lower()}{self.contract_type.lower()}@bookTicker"],
            "id": 1
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送 ticker 订阅请求: {payload}")

    async def _subscribe_orders(self, websocket):
        """订阅挂单数据"""
        if not self.listenKey:
            logger.error("listenKey 为空，无法订阅订单更新")
            return

        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{self.listenKey}"],
            "id": 3
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"已发送挂单订阅请求: {payload}")

    async def _handle_ticker_update(self, message):
        """处理 ticker 更新"""
        current_time = time.time()
        if current_time - self.last_ticker_update_time < 0.5:
            return

        self.last_ticker_update_time = current_time
        data = json.loads(message)
        if data.get("e") == "bookTicker":
            best_bid_price = data.get("b")
            best_ask_price = data.get("a")

            if best_bid_price is None or best_ask_price is None:
                logger.warning("bookTicker 消息中缺少最佳买价或最佳卖价")
                return

            try:
                self.best_bid_price = float(best_bid_price)
                self.best_ask_price = float(best_ask_price)
                self.latest_price = (self.best_bid_price + self.best_ask_price) / 2
            except ValueError as e:
                logger.error(f"解析价格失败: {e}")

            if time.time() - self.last_position_update_time > SYNC_TIME:
                self.long_position, self.short_position = self._get_position()
                self.last_position_update_time = time.time()

            if time.time() - self.last_orders_update_time > SYNC_TIME:
                self._check_orders_status()
                self.last_orders_update_time = time.time()

            await self._grid_loop()
            await self._send_summary_notification()

    async def _handle_order_update(self, message):
        """处理订单更新和持仓更新"""
        # 延迟初始化锁
        if self.lock is None:
            self.lock = asyncio.Lock()
        
        async with self.lock:
            data = json.loads(message)

            if data.get("e") == "ORDER_TRADE_UPDATE":
                order = data.get("o", {})
                symbol = order.get("s")
                if symbol == self.symbol:
                    side = order.get("S")
                    position_side = order.get("ps")
                    reduce_only = order.get("R")
                    status = order.get("X")
                    quantity = float(order.get("q", 0))
                    filled = float(order.get("z", 0))
                    remaining = quantity - filled

                    if status == "NEW":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.buy_long_orders += remaining
                            elif position_side == "SHORT":
                                self.buy_short_orders += remaining
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.sell_long_orders += remaining
                            elif position_side == "SHORT":
                                self.sell_short_orders += remaining
                    elif status == "FILLED":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.long_position += filled
                                self.buy_long_orders = max(0.0, self.buy_long_orders - filled)
                            elif position_side == "SHORT":
                                self.short_position = max(0.0, self.short_position - filled)
                                self.buy_short_orders = max(0.0, self.buy_short_orders - filled)
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.long_position = max(0.0, self.long_position - filled)
                                self.sell_long_orders = max(0.0, self.sell_long_orders - filled)
                            elif position_side == "SHORT":
                                self.short_position += filled
                                self.sell_short_orders = max(0.0, self.sell_short_orders - filled)
                    elif status == "CANCELED":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.buy_long_orders = max(0.0, self.buy_long_orders - quantity)
                            elif position_side == "SHORT":
                                self.buy_short_orders = max(0.0, self.buy_short_orders - quantity)
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.sell_long_orders = max(0.0, self.sell_long_orders - quantity)
                            elif position_side == "SHORT":
                                self.sell_short_orders = max(0.0, self.sell_short_orders - quantity)

    def _get_take_profit_quantity(self, position, side):
        """调整止盈单的交易数量"""
        if side == 'long':
            if position > self.position_limit:
                self.long_initial_quantity = self.initial_quantity * 2
            elif self.short_position >= self.position_threshold:
                self.long_initial_quantity = self.initial_quantity * 2
            else:
                self.long_initial_quantity = self.initial_quantity

        elif side == 'short':
            if position > self.position_limit:
                self.short_initial_quantity = self.initial_quantity * 2
            elif self.long_position >= self.position_threshold:
                self.short_initial_quantity = self.initial_quantity * 2
            else:
                self.short_initial_quantity = self.initial_quantity

    async def _initialize_long_orders(self):
        """初始化多头挂单"""
        current_time = time.time()
        if current_time - self.last_long_order_time < ORDER_FIRST_TIME:
            logger.info(f"距离上次多头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        self._cancel_orders_for_side('long')
        self._place_order('buy', self.best_bid_price, self.initial_quantity, False, 'long')
        logger.info(f"挂出多头开仓单: 买入 @ {self.latest_price}")

        self.last_long_order_time = time.time()
        logger.info("初始化多头挂单完成")

    async def _initialize_short_orders(self):
        """初始化空头挂单"""
        current_time = time.time()
        if current_time - self.last_short_order_time < ORDER_FIRST_TIME:
            logger.info(f"距离上次空头挂单时间不足 {ORDER_FIRST_TIME} 秒，跳过本次挂单")
            return

        self._cancel_orders_for_side('short')
        self._place_order('sell', self.best_ask_price, self.initial_quantity, False, 'short')
        logger.info(f"挂出空头开仓单: 卖出 @ {self.latest_price}")

        self.last_short_order_time = time.time()
        logger.info("初始化空头挂单完成")

    def _cancel_orders_for_side(self, position_side):
        """撤销某个方向的所有挂单"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)

        if len(orders) == 0:
            logger.info("没有找到挂单")
        else:
            try:
                for order in orders:
                    side = order.get('side')
                    reduce_only = order.get('reduceOnly', False)
                    position_side_order = order.get('info', {}).get('positionSide', 'BOTH')

                    if position_side == 'long':
                        if not reduce_only and side == 'buy' and position_side_order == 'LONG':
                            self._cancel_order(order['id'])
                        elif reduce_only and side == 'sell' and position_side_order == 'LONG':
                            self._cancel_order(order['id'])

                    elif position_side == 'short':
                        if not reduce_only and side == 'sell' and position_side_order == 'SHORT':
                            self._cancel_order(order['id'])
                        elif reduce_only and side == 'buy' and position_side_order == 'SHORT':
                            self._cancel_order(order['id'])
            except ccxt.OrderNotFound as e:
                logger.warning(f"订单 {order['id']} 不存在，无需撤销: {e}")
                self._check_orders_status()
            except Exception as e:
                logger.error(f"撤单失败: {e}")

    def _cancel_order(self, order_id):
        """撤单"""
        try:
            self.exchange.cancel_order(order_id, self.ccxt_symbol)
        except ccxt.BaseError as e:
            logger.error(f"撤单失败: {e}")

    def _place_order(self, side, price, quantity, is_reduce_only=False, position_side=None, order_type='limit'):
        """挂单函数"""
        try:
            quantity = round(quantity, self.amount_precision)
            quantity = max(quantity, self.min_order_amount)

            import uuid
            client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"

            if order_type == 'market':
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()
                order = self.exchange.create_order(self.ccxt_symbol, 'market', side, quantity, params=params)
                return order
            else:
                if price is None:
                    logger.error("限价单必须提供 price 参数")
                    return None

                price = round(price, self.price_precision)

                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()
                order = self.exchange.create_order(self.ccxt_symbol, 'limit', side, quantity, price, params)
                return order

        except ccxt.BaseError as e:
            logger.error(f"下单报错: {e}")
            return None

    def _place_take_profit_order(self, ccxt_symbol, side, price, quantity):
        """挂止盈单"""
        orders = self.exchange.fetch_open_orders(ccxt_symbol)
        for order in orders:
            if (
                    order['info'].get('positionSide') == side.upper()
                    and float(order['price']) == price
                    and order['side'] == ('sell' if side == 'long' else 'buy')
            ):
                logger.info(f"已存在相同价格的 {side} 止盈单，跳过挂单")
                return

        try:
            if side == 'long' and self.long_position <= 0:
                logger.warning("没有多头持仓，跳过挂出多头止盈单")
                return
            elif side == 'short' and self.short_position <= 0:
                logger.warning("没有空头持仓，跳过挂出空头止盈单")
                return

            price = round(price, self.price_precision)
            quantity = round(quantity, self.amount_precision)
            quantity = max(quantity, self.min_order_amount)

            if side == 'long':
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

    async def _place_long_orders(self, latest_price):
        """挂多头订单"""
        try:
            self._get_take_profit_quantity(self.long_position, 'long')
            if self.long_position > 0:
                if self.long_position > self.position_threshold:
                    logger.info(f"持仓{self.long_position}超过极限阈值 {self.position_threshold}，long装死")
                    if self.sell_long_orders <= 0:
                        if self.short_position > 0:
                            r = float((self.long_position / self.short_position) / 100 + 1)
                        else:
                            r = 1.01
                        self._place_take_profit_order(self.ccxt_symbol, 'long', self.latest_price * r,
                                                     self.long_initial_quantity)
                else:
                    self._update_mid_price('long', latest_price)
                    self._cancel_orders_for_side('long')
                    self._place_take_profit_order(self.ccxt_symbol, 'long', self.upper_price_long,
                                                 self.long_initial_quantity)
                    self._place_order('buy', self.lower_price_long, self.long_initial_quantity, False, 'long')
                    logger.info("挂多头止盈，挂多头补仓")

        except Exception as e:
            logger.error(f"挂多头订单失败: {e}")

    async def _place_short_orders(self, latest_price):
        """挂空头订单"""
        try:
            self._get_take_profit_quantity(self.short_position, 'short')
            if self.short_position > 0:
                if self.short_position > self.position_threshold:
                    logger.info(f"持仓{self.short_position}超过极限阈值 {self.position_threshold}，short 装死")
                    if self.buy_short_orders <= 0:
                        if self.long_position > 0:
                            r = float((self.short_position / self.long_position) / 100 + 1)
                        else:
                            r = 1.01
                        logger.info("发现多头止盈单缺失。。需要补止盈单")
                        self._place_take_profit_order(self.ccxt_symbol, 'short', self.latest_price * r,
                                                     self.short_initial_quantity)

                else:
                    self._update_mid_price('short', latest_price)
                    self._cancel_orders_for_side('short')
                    self._place_take_profit_order(self.ccxt_symbol, 'short', self.lower_price_short,
                                                 self.short_initial_quantity)
                    self._place_order('sell', self.upper_price_short, self.short_initial_quantity, False, 'short')
                    logger.info("挂空头止盈，挂空头补仓")

        except Exception as e:
            logger.error(f"挂空头订单失败: {e}")

    def _update_mid_price(self, side, price):
        """更新中间价"""
        if side == 'long':
            self.mid_price_long = price
            self.upper_price_long = self.mid_price_long * (1 + self.grid_spacing)
            self.lower_price_long = self.mid_price_long * (1 - self.grid_spacing)
            logger.info("更新 long 中间价")

        elif side == 'short':
            self.mid_price_short = price
            self.upper_price_short = self.mid_price_short * (1 + self.grid_spacing)
            self.lower_price_short = self.mid_price_short * (1 - self.grid_spacing)
            logger.info("更新 short 中间价")

    async def _check_risk(self):
        """检查持仓并减少库存风险"""
        await self._check_and_notify_risk_reduction()

        local_position_threshold = self.position_threshold * 0.8
        quantity = self.position_threshold * 0.1

        if self.long_position >= local_position_threshold and self.short_position >= local_position_threshold:
            logger.info(f"多头和空头持仓均超过阈值 {local_position_threshold}，开始双向平仓，减少库存风险")
            if self.long_position > 0:
                self._place_order('sell', price=None, quantity=quantity, is_reduce_only=True, position_side='long',
                                 order_type='market')
                logger.info(f"市价平仓多头 {quantity} 个")

            if self.short_position > 0:
                self._place_order('buy', price=None, quantity=quantity, is_reduce_only=True, position_side='short',
                                 order_type='market')
                logger.info(f"市价平仓空头 {quantity} 个")

    async def _grid_loop(self):
        """核心网格交易循环"""
        await self._check_and_notify_position_threshold('long', self.long_position)
        await self._check_and_notify_position_threshold('short', self.short_position)
        await self._check_and_notify_double_profit('long', self.long_position)
        await self._check_and_notify_double_profit('short', self.short_position)
        await self._check_risk()

        current_time = time.time()
        
        # 检测多头持仓
        if self.long_position == 0:
            logger.info(f"检测到没有多头持仓{self.long_position}，初始化多头挂单@ ticker")
            await self._initialize_long_orders()
        else:
            if not (0 < self.buy_long_orders <= self.long_initial_quantity) or not (0 < self.sell_long_orders <= self.long_initial_quantity):
                if self.long_position > self.position_threshold and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"距离上次 long 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 long 挂单@ ticker")
                else:
                    await self._place_long_orders(self.latest_price)

        # 检测空头持仓
        if self.short_position == 0:
            await self._initialize_short_orders()
        else:
            if not (0 < self.sell_short_orders <= self.short_initial_quantity) or not (0 < self.buy_short_orders <= self.short_initial_quantity):
                if self.short_position > self.position_threshold and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"距离上次 short 挂止盈时间不足 {ORDER_COOLDOWN_TIME} 秒，跳过本次 short 挂单@ ticker")
                else:
                    await self._place_short_orders(self.latest_price)

    async def start(self):
        """启动机器人"""
        try:
            logger.info("网格交易机器人启动中...")
            
            # 初始化时获取一次持仓数据
            self.long_position, self.short_position = self._get_position()
            logger.info(f"初始化持仓: 多头 {self.long_position} 张, 空头 {self.short_position} 张")

            # 等待状态同步完成
            await asyncio.sleep(5)

            # 初始化时获取一次挂单状态
            self._check_orders_status()
            logger.info(
                f"初始化挂单状态: 多头开仓={self.buy_long_orders}, 多头止盈={self.sell_long_orders}, 空头开仓={self.sell_short_orders}, 空头止盈={self.buy_short_orders}")

            # 发送启动通知
            await self._send_startup_notification()

            # 设置运行状态
            self.running = True

            # 启动 listenKey 更新任务
            asyncio.create_task(self._keep_listen_key_alive())

            # 启动 WebSocket 连接
            while self.running:
                try:
                    await self._connect_websocket()
                except Exception as e:
                    logger.error(f"WebSocket 连接失败: {e}")
                    await self._send_error_notification(str(e), "WebSocket连接失败")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"启动失败: {e}")
            await self._send_error_notification(str(e), "启动失败")
            raise e

    def stop(self):
        """停止机器人"""
        logger.info("正在停止机器人...")
        self.running = False
        # 发送停止通知
        asyncio.create_task(self._send_telegram_message("🛑 **机器人已手动停止**\n\n用户主动停止了网格交易机器人", urgent=False, silent=True)) 