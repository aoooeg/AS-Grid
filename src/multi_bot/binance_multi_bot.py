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

# 使用优化的日志配置
try:
    from logging_config import setup_binance_multi_bot_logging, ThresholdStateLogger
    logger = setup_binance_multi_bot_logging()
    threshold_logger = ThresholdStateLogger(logger)
except ImportError:
    # 如果导入失败，使用默认配置
    os.makedirs("log", exist_ok=True)
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
    threshold_logger = None


class CustomBinance(ccxt.binance):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        return super().fetch(url, method, headers, body)


class BinanceGridBot:
    # ===== 持久化：仅用本地文件恢复装死状态 =====
    def _state_file_path(self):
        state_dir = os.path.join("src", "multi_bot", "state")
        os.makedirs(state_dir, exist_ok=True)
        safe_symbol = str(self.symbol).replace("USDT", "").replace("USDC", "")
        return os.path.join(state_dir, f"lockdown_{safe_symbol}.json")

    def _persist_lockdown_state(self):
        """将当前 lockdown_mode 持久化到本地，仅本地恢复，不依赖交易所订单。"""
        try:
            data = {
                'long': {
                    'active': bool(self.lockdown_mode.get('long', {}).get('active')),
                    'lockdown_price': self.lockdown_mode.get('long', {}).get('lockdown_price'),
                },
                'short': {
                    'active': bool(self.lockdown_mode.get('short', {}).get('active')),
                    'lockdown_price': self.lockdown_mode.get('short', {}).get('lockdown_price'),
                },
                'updated_at': time.time()
            }
            with open(self._state_file_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if logger: logger.info("已将装死状态写入本地文件")
        except Exception as e:
            if logger: logger.error(f"写入装死状态本地文件失败: {e}")

    def _restore_lockdown_from_local(self):
        """仅从本地文件恢复装死状态；若无本地记录则不做任何推断。"""
        try:
            path = self._state_file_path()
            if not os.path.exists(path):
                if logger: logger.info(f"未找到本地装死状态文件: {path}，跳过恢复")
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if logger: logger.info(f"成功读取装死状态文件: {path}, 数据: {data}")
            for side in ('long', 'short'):
                pos = self.long_position if side == 'long' else self.short_position
                if pos is None or self.position_threshold is None:
                    continue
                if pos > self.position_threshold:
                    # 仅当仓位超阈值且本地记录 active/lockdown_price 时恢复
                    side_data = data.get(side, {})
                    lock = side_data.get('lockdown_price')
                    active = bool(side_data.get('active'))
                    if logger: logger.info(f"检查{side}恢复条件: pos={pos}, threshold={self.position_threshold}, active={active}, lock={lock}")
                    if active and lock:
                        self.lockdown_mode[side]['active'] = True
                        self.lockdown_mode[side]['lockdown_price'] = float(lock)
                        r = float(self._compute_tp_multiplier(side))
                        # 由锁仓价推导固定止盈价（不读取/不反推交易所订单）
                        if side == 'long':
                            tp = self.lockdown_mode[side]['lockdown_price'] * r
                        else:
                            tp = self.lockdown_mode[side]['lockdown_price'] / r
                        self.lockdown_mode[side]['tp_price'] = tp
                        if logger:
                            logger.info(f"从本地恢复 {side} 装死：lock={self.lockdown_mode[side]['lockdown_price']}, tp={tp}")
        except Exception as e:
            if logger: logger.error(f"从本地恢复装死状态失败: {e}")

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
        self.position_threshold_factor = float(self.config.get('position_threshold_factor', 10))
        self.position_limit_factor = float(self.config.get('position_limit_factor', 5))
        self.position_threshold = self.position_threshold_factor * self.initial_quantity / self.grid_spacing * 2 / 100
        self.position_limit = self.position_limit_factor * self.initial_quantity / self.grid_spacing * 2 / 100
        
        # 初始化交易所
        self.exchange = self._init_exchange()
        self.ccxt_symbol = f"{symbol.replace('USDT', '').replace('USDC', '')}/{self.contract_type}:{self.contract_type}"
        
        # 获取价格精度
        self._get_price_precision()
        
        # 初始化状态变量
        # === 紧急减仓配置与状态（Simple Plan, Fixed Quantity） ===
        self.emg_enter_ratio = float(self.config.get('emg_enter_ratio', 0.80))
        self.emg_exit_ratio  = float(self.config.get('emg_exit_ratio', 0.75))
        self.enable_dynamic_enter_075 = bool(self.config.get('enable_dynamic_enter_075', True))
        self.emg_cooldown_s  = int(self.config.get('emg_cooldown_s', 60))
        self.grid_pause_after_emg_s = int(self.config.get('grid_pause_after_emg_s', 90))
        self.emg_batches     = int(self.config.get('emg_batches', 2))
        self.emg_batch_sleep_ms = int(self.config.get('emg_batch_sleep_ms', 300))
        self.emg_slip_cap_bp = int(self.config.get('emg_slip_cap_bp', 15))
        self.emg_daily_fuse_count = int(self.config.get('emg_daily_fuse_count', 3))

        self._emg_last_ts = 0.0
        self._emg_in_progress = False
        self._emg_trigger_count_today = 0
        self._grid_pause_until_ts = 0.0
        self._day_fuse_on = False
        self._emg_day = time.strftime('%Y-%m-%d')

        self._initial_quantity_base = self.initial_quantity
        self._grid_spacing_base = self.grid_spacing
        self._last_param_recover_ts = 0.0

        from collections import deque
        self._vol_prices = deque(maxlen=60)

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
        
        # 装死模式状态记录（新增）
        self.lockdown_mode = {
            'long': {'active': False, 'tp_price': None, 'lockdown_price': None},
            'short': {'active': False, 'tp_price': None, 'lockdown_price': None}
        }

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
        local_position_threshold = self.position_threshold * 0.8
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
• 阈值: {self.position_threshold * 0.8:.2f}

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
• 监控阈值: {self.position_threshold * 0.8:.2f}

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
        # 先按精度 round
        price = round(float(price), self.price_precision)

        # 如果已有"同价位"的止盈单则跳过（使用 round 后的严格相等判断）
        orders = self.exchange.fetch_open_orders(ccxt_symbol)
        for order in orders:
            pos = order['info'].get('positionSide')
            s = order['side']
            try:
                op = round(float(order['price']), self.price_precision)
            except Exception:
                op = None
            if (
                pos == side.upper()
                and s == ('sell' if side == 'long' else 'buy')
                and op is not None and op == price
            ):
                logger.info(f"已存在相同价格的 {side} 止盈单({price})，跳过挂单")
                return

        try:
            if side == 'long' and self.long_position <= 0:
                logger.warning("没有多头持仓，跳过挂出多头止盈单")
                return
            elif side == 'short' and self.short_position <= 0:
                logger.warning("没有空头持仓，跳过挂出空头止盈单")
                return

            qty = round(float(quantity), self.amount_precision)
            qty = max(qty, self.min_order_amount)
            if side == 'long':
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'LONG'
                }
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'sell', qty, price, params)
                logger.info(f"成功挂 long 止盈单: 卖出 {qty} {ccxt_symbol} @ {price}")
            elif side == 'short':
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'buy', qty, price, {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'SHORT'
                })
                logger.info(f"成功挂 short 止盈单: 买入 {qty} {ccxt_symbol} @ {price}")
        except ccxt.BaseError as e:
            logger.error(f"挂止盈单失败: {e}")

    # ===== 核心：多头下单逻辑（修复：只加倍止盈、不加倍补仓；装死限幅；下单后更新冷却时间）=====
    async def _place_long_orders(self, latest_price):
        """挂多头订单"""
        try:
            # 根据当前持仓情况动态调整多头下单数量（可能翻倍）
            self._get_take_profit_quantity(self.long_position, 'long')  # 只影响止盈数量
            if self.long_position <= 0:
                return
            placed_any = False
            
            # 只有在有多头持仓时才进行挂单操作
            if self.long_position > 0:
                # 检查是否超过极限阈值，决定是否进入"装死"模式
                if self.long_position > self.position_threshold:
                    # 装死模式：持仓过大，停止开新仓，只补止盈单
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'long', self.long_position, self.position_threshold, True)
                    else:
                        logger.info(f"持仓{self.long_position}超过极限阈值 {self.position_threshold}，long装死")
                    
                    # 检查是否刚进入装死模式，记录固定止盈价
                    if not self.lockdown_mode['long']['active']:
                        self.lockdown_mode['long']['active'] = True
                        # 记录装死时的价格，确保后续不再变化
                        self.lockdown_mode['long']['lockdown_price'] = self.latest_price
                        r = self._compute_tp_multiplier('long')
                        self.lockdown_mode['long']['tp_price'] = self.lockdown_mode['long']['lockdown_price'] * r
                        # 写入本地持久化，确保重启可恢复
                        self._persist_lockdown_state()
                        logger.info(f"多头进入装死模式，固定止盈价: {self.lockdown_mode['long']['tp_price']} (基于装死价格: {self.lockdown_mode['long']['lockdown_price']})")
                    
                    # 装死模式下使用固定的止盈价，基于装死时的价格计算
                    fixed_tp_price = self.lockdown_mode['long']['tp_price']
                    placed_any |= self._ensure_lockdown_take_profit(
                        side='long',
                        target_price=fixed_tp_price,
                        quantity=self.long_initial_quantity
                    )
                    
                    # 记录装死模式状态
                    self._log_lockdown_status('long')
                    
                    # 验证装死模式完整性
                    if not self._validate_lockdown_integrity('long'):
                        logger.error("多头装死模式完整性验证失败，重置装死状态")
                        self.lockdown_mode['long']['active'] = False
                        self.lockdown_mode['long']['tp_price'] = None
                        self.lockdown_mode['long']['lockdown_price'] = None
                else:
                    # 正常网格：先更新中线，再只撤开仓挂单，止盈按目标价"校准/重挂"，补仓用基础数量
                    # 检查是否从装死模式恢复正常
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'long', self.long_position, self.position_threshold, False)
                    
                    # 如果从装死模式恢复正常，重置装死状态
                    if self.lockdown_mode['long']['active']:
                        self.lockdown_mode['long']['active'] = False
                        self.lockdown_mode['long']['tp_price'] = None
                        self.lockdown_mode['long']['lockdown_price'] = None
                        # 写入本地持久化，确保状态同步
                        self._persist_lockdown_state()
                        logger.info("多头退出装死模式，恢复正常交易")
                    
                    self._update_mid_price('long', latest_price)
                    self._cancel_open_orders_for_side('long')

                    # 止盈（可能重挂）：用 long_initial_quantity（可能=2*initial_quantity）
                    placed_any |= self._ensure_take_profit_at(
                        side='long',
                        target_price=self.upper_price_long,
                        quantity=self.long_initial_quantity,
                        tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                    )

                    # 补仓：始终使用基础数量 initial_quantity，而不是"加倍后"的 long_initial_quantity
                    open_qty = max(self.min_order_amount, round(self.initial_quantity, self.amount_precision))
                    if self._place_order('buy', self.lower_price_long, open_qty, False, 'long'):
                        placed_any = True
                    logger.info("挂多头止盈，挂多头补仓")

                # 若本轮确实有挂出新单/重挂，则更新冷却时间戳
                if placed_any:
                    self.last_long_order_time = time.time()

        except Exception as e:
            logger.error(f"挂多头订单失败: {e}")

    async def _place_short_orders(self, latest_price):
        """挂空头订单"""
        try:
            # 根据当前持仓情况动态调整空头下单数量（可能翻倍）
            self._get_take_profit_quantity(self.short_position, 'short')
            if self.short_position <= 0:
                return
            placed_any = False
            
            # 只有在有空头持仓时才进行挂单操作
            if self.short_position > 0:
                # 检查是否超过极限阈值，决定是否进入"装死"模式
                if self.short_position > self.position_threshold:
                    # 装死模式：持仓过大，停止开新仓，只补止盈单
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'short', self.short_position, self.position_threshold, True)
                    else:
                        logger.info(f"持仓{self.short_position}超过极限阈值 {self.position_threshold}，short 装死")
                    
                    # 检查是否刚进入装死模式，记录固定止盈价
                    if not self.lockdown_mode['short']['active']:
                        self.lockdown_mode['short']['active'] = True
                        # 记录装死时的价格，确保后续不再变化
                        self.lockdown_mode['short']['lockdown_price'] = self.latest_price
                        r = self._compute_tp_multiplier('short')
                        self.lockdown_mode['short']['tp_price'] = self.lockdown_mode['short']['lockdown_price'] / r
                        # 写入本地持久化，确保重启可恢复
                        self._persist_lockdown_state()
                        logger.info(f"空头进入装死模式，固定止盈价: {self.lockdown_mode['short']['tp_price']} (基于装死价格: {self.lockdown_mode['short']['lockdown_price']})")
                    
                    # 装死模式下使用固定的止盈价，基于装死时的价格计算
                    fixed_tp_price = self.lockdown_mode['short']['tp_price']
                    placed_any |= self._ensure_lockdown_take_profit(
                        side='short',
                        target_price=fixed_tp_price,
                        quantity=self.short_initial_quantity
                    )
                    
                    # 记录装死模式状态
                    self._log_lockdown_status('short')
                    
                    # 验证装死模式完整性
                    if not self._validate_lockdown_integrity('short'):
                        logger.error("空头装死模式完整性验证失败，重置装死状态")
                        self.lockdown_mode['short']['active'] = False
                        self.lockdown_mode['short']['tp_price'] = None
                        self.lockdown_mode['short']['lockdown_price'] = None
                else:
                    # 检查是否从装死模式恢复正常
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'short', self.short_position, self.position_threshold, False)
                    
                    # 如果从装死模式恢复正常，重置装死状态
                    if self.lockdown_mode['short']['active']:
                        self.lockdown_mode['short']['active'] = False
                        self.lockdown_mode['short']['tp_price'] = None
                        self.lockdown_mode['short']['lockdown_price'] = None
                        # 写入本地持久化，确保状态同步
                        self._persist_lockdown_state()
                        logger.info("空头退出装死模式，恢复正常交易")
                    
                    self._update_mid_price('short', latest_price)
                    self._cancel_open_orders_for_side('short')

                    placed_any |= self._ensure_take_profit_at(
                        side='short',
                        target_price=self.lower_price_short,
                        quantity=self.short_initial_quantity,
                        tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                    )

                    open_qty = max(self.min_order_amount, round(self.initial_quantity, self.amount_precision))
                    if self._place_order('sell', self.upper_price_short, open_qty, False, 'short'):
                        placed_any = True
                    logger.info("挂空头止盈，挂空头补仓")

                # 若本轮确实有挂出新单/重挂，则更新冷却时间戳
                if placed_any:
                    self.last_short_order_time = time.time()

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
        """检查持仓并减少库存风险（紧急减仓：固定数量 + 冷却 + 暂停网格 + 退出滞后）"""
        self._reset_emg_daily_counter_if_new_day()
        if self._day_fuse_on:
            return

        enter_ratio = self.emg_enter_ratio
        if self.enable_dynamic_enter_075 and self._is_extreme_vol():
            enter_ratio = min(enter_ratio, 0.75)

        T = self.position_threshold
        now = time.time()

        if self._emg_in_progress:
            if (self.long_position < self.emg_exit_ratio * T and
                self.short_position < self.emg_exit_ratio * T and
                now >= self._grid_pause_until_ts):
                self._emg_in_progress = False
                logger.info(f"[EMG][{self.symbol}] 退出紧急态：多空均低于 {self.emg_exit_ratio:.2f}T")
                # 发送退出紧急状态通知
                await self._send_emergency_exit_notification()
            return

        if (self.long_position >= enter_ratio * T and
            self.short_position >= enter_ratio * T and
            (now - self._emg_last_ts >= self.emg_cooldown_s)):
            self._emg_in_progress = True
            self._emg_last_ts = now
            self._grid_pause_until_ts = now + self.grid_pause_after_emg_s
            self._emg_trigger_count_today += 1
            logger.info(f"[EMG][{self.symbol}] 进入紧急减仓：阈值 {enter_ratio:.2f}T，冷却 {self.emg_cooldown_s}s，暂停网格 {self.grid_pause_after_emg_s}s")
            
            # 发送进入紧急状态通知
            await self._send_emergency_enter_notification(enter_ratio)

            if self._emg_trigger_count_today >= self.emg_daily_fuse_count:
                self._enter_day_fuse_mode()
                # 发送日内封盘通知
                await self._send_daily_fuse_notification()
                return

            try:
                self._cancel_open_orders_for_side('long')
                self._cancel_open_orders_for_side('short')
            except Exception as e:
                logger.warning(f"[EMG] 撤开仓挂单异常：{e}")

            fixed_qty = max(self.min_order_amount, round(self.position_threshold * 0.1, self.amount_precision))
            long_cut  = min(fixed_qty, max(0.0, self.long_position))
            short_cut = min(fixed_qty, max(0.0, self.short_position))

            if long_cut > 0:
                await self._emg_reduce_side_batched('long', long_cut)
            if short_cut > 0:
                await self._emg_reduce_side_batched('short', short_cut)

            self._apply_temp_param_cooling()

    async def _grid_loop(self):
        """核心网格交易循环"""
        await self._check_and_notify_position_threshold('long', self.long_position)
        await self._check_and_notify_position_threshold('short', self.short_position)
        await self._check_and_notify_double_profit('long', self.long_position)
        await self._check_and_notify_double_profit('short', self.short_position)
        await self._check_risk()

        # 记录价格与风控辅助
        self._record_price(self.latest_price)
        self._recover_params_if_needed()
        self._reset_emg_daily_counter_if_new_day()

        # 暂停窗口或封盘：不再开新网格/初始化
        if time.time() < self._grid_pause_until_ts or self._day_fuse_on:
            # 避免重复记录暂停日志
            if not hasattr(self, '_last_pause_log_ts') or time.time() - getattr(self, '_last_pause_log_ts', 0) > 60:
                self._last_pause_log_ts = time.time()
                if self._day_fuse_on:
                    logger.info('[EMG] 日内封盘模式开启，跳过本轮开仓/挂单')
                else:
                    remaining_time = self._grid_pause_until_ts - time.time()
                    logger.info(f'[EMG] 暂停窗口开启，剩余暂停时间: {remaining_time:.0f}秒，跳过本轮开仓/挂单')
            return

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
    
    # ===== 新增：只撤"开仓"挂单，保留 reduceOnly 的止盈挂单 =====
    def _cancel_open_orders_for_side(self, position_side: str):
        """仅撤销某个方向的开仓挂单（reduceOnly=False），保留止盈单"""
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)
        try:
            for order in orders:
                side = order.get('side')  # 'buy' / 'sell'
                pos = order.get('info', {}).get('positionSide', 'BOTH')  # 'LONG' / 'SHORT'
                # 兼容读取 reduceOnly
                ro = order.get('reduceOnly')
                if ro is None:
                    ro = order.get('info', {}).get('reduceOnly') or order.get('info', {}).get('reduce_only') or False

                if position_side == 'long':
                    # 多头开仓: buy + LONG + 非 reduceOnly
                    if (pos == 'LONG') and (side == 'buy') and (not ro):
                        self._cancel_order(order['id'])
                elif position_side == 'short':
                    # 空头开仓: sell + SHORT + 非 reduceOnly
                    if (pos == 'SHORT') and (side == 'sell') and (not ro):
                        self._cancel_order(order['id'])
        except ccxt.OrderNotFound as e:
            logger.warning(f"撤单时发现不存在的订单: {e}")
            self._check_orders_status()
        except Exception as e:
            logger.error(f"撤销开仓挂单失败: {e}")

    # ===== 新增：获取当前方向已有的止盈单（reduceOnly=True）=====
    def _get_existing_tp_order(self, side: str):
        """
        返回该方向当前已存在的一张 reduceOnly 止盈单（若有）。
        side: 'long' or 'short'
        """
        orders = self.exchange.fetch_open_orders(self.ccxt_symbol)
        for order in orders:
            pos = order.get('info', {}).get('positionSide', 'BOTH')
            s = order.get('side')
            ro = order.get('reduceOnly')
            if ro is None:
                ro = order.get('info', {}).get('reduceOnly') or order.get('info', {}).get('reduce_only') or False

            if side == 'long' and pos == 'LONG' and ro and s == 'sell':
                return order
            if side == 'short' and pos == 'SHORT' and ro and s == 'buy':
                return order
        return None

    # ===== 新增：确保止盈单在目标价位（偏离超阈值则重挂），返回是否有下单动作 =====
    def _ensure_take_profit_at(self, side: str, target_price: float, quantity: float, tol_ratio: float = None) -> bool:
        """
        side: 'long'/'short'
        target_price: 目标止盈价（会按精度 round）
        quantity: 止盈数量（已考虑 double 逻辑）
        tol_ratio: 相对容忍度（如 0.002 = 0.2%）。默认取 grid_spacing 的 0.2 与 0.1% 的较大值。
        """
        if tol_ratio is None:
            tol_ratio = max(self.grid_spacing * 0.2, 0.001)  # 根据网格间距自适应

        target_price = round(float(target_price), self.price_precision)
        existing = self._get_existing_tp_order(side)
        if existing:
            try:
                existing_price = round(float(existing['price']), self.price_precision)
            except Exception:
                existing_price = None

            if existing_price is not None:
                rel_diff = abs(existing_price / target_price - 1.0)
                if rel_diff <= tol_ratio:
                    # 已有止盈价足够接近，不重挂
                    return False
                else:
                    # 价格偏离明显，先撤再重挂
                    self._cancel_order(existing['id'])

        # 挂新的止盈
        self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
        return True

    def _ensure_lockdown_take_profit(self, side: str, target_price: float, quantity: float):
        """装死模式下的止盈单管理：只在首次进入时挂单，后续不重挂，确保价格完全固定"""
        existing = self._get_existing_tp_order(side)
        if existing:
            # 已有止盈单，验证价格是否与装死时的固定价格一致
            try:
                existing_price = round(float(existing['price']), self.price_precision)
                target_price_rounded = round(float(target_price), self.price_precision)
                
                if existing_price != target_price_rounded:
                    logger.warning(f"装死模式止盈单价格不一致！现有: {existing_price}, 固定目标: {target_price_rounded}")
                    # 在装死模式下，如果价格不一致，强制撤单并重新挂单
                    self._cancel_order(existing['id'])
                    logger.info(f"装死模式：撤单并重新挂出固定止盈价: {target_price_rounded}")
                    self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
                    return True
                else:
                    # 价格一致，不重挂
                    return False
            except Exception as e:
                logger.error(f"验证装死模式止盈单价格时出错: {e}")
                return False
        
        # 没有止盈单，挂新的止盈单
        logger.info(f"装死模式：首次挂出固定止盈单 {side} @ {target_price}")
        self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
        return True

    # ===== 新增：装死分支的 r 限幅计算 =====
    def _compute_tp_multiplier(self, side: str) -> float:
        """
        计算在"装死"状态下用于调整止盈价的倍数 r，并做上下限约束：
        下限= max(1 + grid_spacing, 1.01)，上限= min(1 + 3*grid_spacing, 1.05)
        """
        if side == 'long':
            pos, opp = self.long_position, self.short_position
        else:
            pos, opp = self.short_position, self.long_position

        if opp > 0:
            r = 1.0 + (pos / opp) / 100.0
        else:
            r = 1.01

        min_r = max(1.0 + self.grid_spacing, 1.01)
        max_r = min(1.0 + 3.0 * self.grid_spacing, 1.05)
        return max(min_r, min(r, max_r))

    def _log_lockdown_status(self, side: str):
        """记录装死模式状态，用于调试和监控（只在状态变化时记录）"""
        current_time = time.time()
        
        # 检查是否需要记录日志（只在状态变化时记录）
        if not hasattr(self, '_last_lockdown_log_time'):
            self._last_lockdown_log_time = {}
        
        if side not in self._last_lockdown_log_time:
            self._last_lockdown_log_time[side] = 0
            
        # 如果距离上次记录时间不足1秒，则跳过（避免重复记录）
        if current_time - self._last_lockdown_log_time[side] < 1:
            return
            
        if self.lockdown_mode[side]['active']:
            logger.info(f"装死模式状态 - {side}: 激活中, 固定止盈价: {self.lockdown_mode[side]['tp_price']}, 装死基准价: {self.lockdown_mode[side]['lockdown_price']}")
            # 更新最后记录时间
            self._last_lockdown_log_time[side] = current_time
        else:
            # 未激活状态不记录，避免日志过多
            pass

    def _validate_lockdown_integrity(self, side: str) -> bool:
        """验证装死模式的完整性，确保价格固定逻辑正确"""
        if not self.lockdown_mode[side]['active']:
            return True
            
        # 检查装死模式的关键数据是否完整
        if (self.lockdown_mode[side]['tp_price'] is None or 
            self.lockdown_mode[side]['lockdown_price'] is None):
            logger.error(f"装死模式数据不完整: {side} - tp_price: {self.lockdown_mode[side]['tp_price']}, lockdown_price: {self.lockdown_mode[side]['lockdown_price']}")
            return False
            
        # 验证止盈价是否基于装死基准价计算
        if side == 'long':
            expected_tp = self.lockdown_mode[side]['lockdown_price'] * self._compute_tp_multiplier(side)
        else:
            expected_tp = self.lockdown_mode[side]['lockdown_price'] / self._compute_tp_multiplier(side)
            
        if abs(self.lockdown_mode[side]['tp_price'] - expected_tp) > 0.000001:
            logger.error(f"装死模式止盈价计算错误: {side} - 实际: {self.lockdown_mode[side]['tp_price']}, 期望: {expected_tp}")
            return False
            
        logger.debug(f"装死模式完整性验证通过: {side}")
        return True

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
            # 仅用本地持久化恢复装死状态（不读取订单、不反推）
            self._restore_lockdown_from_local()

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

    # === 紧急减仓：辅助方法（固定数量版） ===
    def _reset_emg_daily_counter_if_new_day(self):
        day = time.strftime('%Y-%m-%d')
        if day != self._emg_day:
            self._emg_day = day
            self._emg_trigger_count_today = 0
            self._day_fuse_on = False

    def _enter_day_fuse_mode(self):
        self._day_fuse_on = True
        try:
            self._cancel_open_orders_for_side('long')
            self._cancel_open_orders_for_side('short')
        except Exception as e:
            logger.warning(f"[EMG] 进入封盘时撤单异常: {e}")
        logger.warning(f"[EMG][{self.symbol}] 日内触发≥{self.emg_daily_fuse_count}次，封盘：仅保留reduceOnly止盈/止损")

    def _apply_temp_param_cooling(self):
        try:
            base_q = getattr(self, '_initial_quantity_base', self.initial_quantity)
            base_g = getattr(self, '_grid_spacing_base', self.grid_spacing)
            self.initial_quantity = max(self.min_order_amount, round(base_q * 0.7, self.amount_precision))
            self.grid_spacing     = base_g * 1.3
            self._last_param_recover_ts = time.time()
            logger.info(f"[EMG] 临时降参：initial_quantity→{self.initial_quantity}, grid_spacing→{self.grid_spacing:.6f}")
        except Exception as e:
            logger.warning(f"[EMG] 降参失败: {e}")

    def _recover_params_if_needed(self):
        if self._last_param_recover_ts == 0:
            return
        if time.time() - self._last_param_recover_ts < 300:
            return
        base_q = getattr(self, '_initial_quantity_base', self.initial_quantity)
        base_g = getattr(self, '_grid_spacing_base', self.grid_spacing)
        try:
            new_q = min(base_q, round(self.initial_quantity * 1.1, self.amount_precision))
            new_g = max(base_g, self.grid_spacing / 1.1)
            
            # 计算恢复进度
            q_progress = (new_q - base_q * 0.7) / (base_q - base_q * 0.7) * 100 if base_q > base_q * 0.7 else 100
            g_progress = (base_g * 1.3 - new_g) / (base_g * 1.3 - base_g) * 100 if base_g * 1.3 > base_g else 100
            
            self.initial_quantity = new_q
            self.grid_spacing     = new_g
            self._last_param_recover_ts = time.time()
            
            # 检查是否完全恢复
            if abs(new_q - base_q) < 0.01 and abs(new_g - base_g) < 0.000001:
                logger.info(f"[EMG] 参数已完全恢复：initial_quantity→{self.initial_quantity}, grid_spacing→{self.grid_spacing:.6f}")
                self._last_param_recover_ts = 0  # 重置，避免重复检查
                # 发送参数完全恢复通知
                asyncio.create_task(self._send_param_recovery_complete_notification())
            else:
                # 只在重要进度节点发送通知，避免过于频繁
                current_progress = min(q_progress, g_progress)
                if not hasattr(self, '_last_progress_notification') or current_progress - getattr(self, '_last_progress_notification', 0) >= 25:
                    # 每25%进度发送一次通知
                    self._last_progress_notification = current_progress
                    asyncio.create_task(self._send_param_recovery_progress_notification(q_progress, g_progress))
                    # 只在发送通知时记录日志，避免重复
                    logger.info(f"[EMG] 参数恢复进度 - 下单量: {q_progress:.1f}%, 网格间距: {g_progress:.1f}%")
                
        except Exception as e:
            logger.warning(f"[EMG] 参数恢复失败: {e}")

    def _record_price(self, price: float):
        try:
            if price and price > 0:
                self._vol_prices.append(float(price))
        except Exception:
            pass

    def _is_extreme_vol(self) -> bool:
        if len(self._vol_prices) < 10:
            return False
        hi = max(self._vol_prices)
        lo = min(self._vol_prices)
        mid = (hi + lo) / 2.0 if (hi + lo) else 0.0
        if mid == 0:
            return False
        
        volatility = (hi - lo) / mid
        is_extreme = volatility >= 0.006
        
        if is_extreme:
            # 避免重复通知，只在波动率变化显著时通知，并增加时间间隔控制
            current_time = time.time()
            if (not hasattr(self, '_last_volatility_notification') or 
                abs(volatility - getattr(self, '_last_volatility_notification', 0)) >= 0.002 or
                current_time - getattr(self, '_last_volatility_time', 0) >= 300):  # 至少5分钟间隔
                self._last_volatility_notification = volatility
                self._last_volatility_time = current_time
                logger.info(f"[EMG] 检测到极端波动：最高价={hi:.8f}, 最低价={lo:.8f}, 波动率={volatility:.4f} ({volatility*100:.2f}%)")
        
        return is_extreme

    async def _emg_reduce_side_batched(self, side: str, qty_total: float):
        batches = max(1, int(self.emg_batches))
        if batches == 1:
            parts = [qty_total]
        else:
            base = qty_total / batches
            parts = [round(base, self.amount_precision)] * (batches - 1)
            last = max(self.min_order_amount, qty_total - sum(parts))
            parts.append(last)

        logger.info(f"[EMG] 开始执行{side}方向减仓，总数量: {qty_total}，分{len(parts)}批")
        
        # 发送减仓开始通知
        await self._send_reduction_start_notification(side, qty_total, len(parts))

        for i, part in enumerate(parts, 1):
            try:
                lp, sp = self._get_position()
                if lp is not None:
                    self.long_position = lp
                if sp is not None:
                    self.short_position = sp
            except Exception:
                pass

            if side == 'long' and self.long_position < self.emg_exit_ratio * self.position_threshold:
                logger.info(f"[EMG] {side}方向仓位已降至安全区，停止减仓")
                # 发送提前完成通知
                await self._send_reduction_early_complete_notification(side, i-1, len(parts))
                break
            if side == 'short' and self.short_position < self.emg_exit_ratio * self.position_threshold:
                logger.info(f"[EMG] {side}方向仓位已降至安全区，停止减仓")
                # 发送提前完成通知
                await self._send_reduction_early_complete_notification(side, i-1, len(parts))
                break

            ok = False
            try:
                bid, ask = self._get_best_quotes()
                slip = self.emg_slip_cap_bp / 10000.0
                if side == 'long' and bid:
                    limit_price = bid * (1 - slip)
                    self._place_order('sell', price=limit_price, quantity=part, is_reduce_only=True, position_side='long', order_type='limit')
                    ok = True
                    # 减少日志频率，只在关键批次记录
                    if i == 1 or i == len(parts):
                        logger.info(f"[EMG] {side}方向第{i}批限价减仓成功: 卖出{part}张 @ {limit_price:.8f}")
                elif side == 'short' and ask:
                    limit_price = ask * (1 + slip)
                    self._place_order('buy', price=limit_price, quantity=part, is_reduce_only=True, position_side='short', order_type='limit')
                    ok = True
                    # 减少日志频率，只在关键批次记录
                    if i == 1 or i == len(parts):
                        logger.info(f"[EMG] {side}方向第{i}批限价减仓成功: 买入{part}张 @ {limit_price:.8f}")
            except Exception as e:
                logger.warning(f"[EMG] 限价减仓异常（{side} 第{i}批）：{e}")

            if not ok:
                try:
                    if side == 'long':
                        self._place_order('sell', price=None, quantity=part, is_reduce_only=True, position_side='long', order_type='market')
                        logger.info(f"[EMG] {side}方向第{i}批市价减仓成功: 卖出{part}张")
                    else:
                        self._place_order('buy', price=None, quantity=part, is_reduce_only=True, position_side='short', order_type='market')
                        logger.info(f"[EMG] {side}方向第{i}批市价减仓成功: 买入{part}张")
                except Exception as e:
                    logger.error(f"[EMG] 市价减仓失败（{side} 第{i}批）：{e}")

            # 修复异步问题：使用asyncio.sleep替代time.sleep
            if i < len(parts):  # 最后一批不需要等待
                await asyncio.sleep(self.emg_batch_sleep_ms / 1000.0)
        
        # 发送减仓完成通知
        await self._send_reduction_complete_notification(side, qty_total, len(parts))

    def _get_best_quotes(self):
        try:
            t = self.exchange.fetch_ticker(self.ccxt_symbol)
            bid = t.get('bid') or t.get('info', {}).get('bidPrice')
            ask = t.get('ask') or t.get('info', {}).get('askPrice')
            return float(bid) if bid else None, float(ask) if ask else None
        except Exception as e:
            logger.warning(f"[EMG] 获取报价失败: {e}")
            return None, None

    def stop(self):
        """停止机器人"""
        logger.info("正在停止机器人...")
        self.running = False
        # 发送停止通知
        asyncio.create_task(self._send_telegram_message("🛑 **机器人已手动停止**\n\n用户主动停止了网格交易机器人", urgent=False, silent=True))

    async def _send_daily_circuit_breaker_notification(self):
        """发送日内封盘通知"""
        message = f"""
🚫 **日内封盘模式启动**

⚠️ **触发条件**
• 当日紧急减仓次数: {self.emergency_mode['daily_trigger_count']} 次
• 已达到最大允许次数: 3次

🛑 **限制措施**
• 当日不再开新仓
• 只保留现有止盈单
• 次日零点自动重置

📊 **风险提示**
• 市场波动较大，建议谨慎操作
• 可考虑手动调整策略参数
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_emergency_enter_notification(self, enter_ratio):
        """发送进入紧急减仓状态通知"""
        message = f"""
🚨 **紧急减仓触发**

📊 **持仓状况**
• 币种: {self.symbol}
• 多头持仓: {self.long_position} 张
• 空头持仓: {self.short_position} 张
• 触发阈值: {enter_ratio:.2f} × {self.position_threshold:.2f} = {enter_ratio * self.position_threshold:.2f}

⚡ **执行措施**
• 撤销所有开仓挂单
• 分批执行减仓操作
• 暂停网格开仓 {self.grid_pause_after_emg_s} 秒
• 临时调整参数：下单量70%，网格间距1.3倍

📈 **当日统计**
• 第 {self._emg_trigger_count_today} 次触发
• 冷却期: {self.emg_cooldown_s} 秒
• 剩余触发次数: {self.emg_daily_fuse_count - self._emg_trigger_count_today} 次

⏰ **触发时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_emergency_exit_notification(self):
        """发送退出紧急减仓状态通知"""
        message = f"""
✅ **紧急减仓状态解除**

📊 **当前持仓**
• 币种: {self.symbol}
• 多头持仓: {self.long_position} 张
• 空头持仓: {self.short_position} 张
• 安全阈值: {self.emg_exit_ratio:.2f} × {self.position_threshold:.2f} = {self.emg_exit_ratio * self.position_threshold:.2f}

🔄 **参数恢复**
• 开始逐步恢复原始参数
• 每5分钟恢复10%
• 预计恢复时间: 15-20分钟

📈 **当日统计**
• 已触发 {self._emg_trigger_count_today} 次
• 剩余触发次数: {self.emg_daily_fuse_count - self._emg_trigger_count_today} 次

⏰ **解除时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_daily_fuse_notification(self):
        """发送日内封盘通知"""
        message = f"""
🚫 **日内封盘模式启动**

⚠️ **触发条件**
• 币种: {self.symbol}
• 当日紧急减仓次数: {self._emg_trigger_count_today} 次
• 已达到最大允许次数: {self.emg_daily_fuse_count} 次

🛑 **限制措施**
• 当日不再开新仓
• 只保留现有止盈单
• 次日零点自动重置

📊 **风险提示**
• 市场波动较大，建议谨慎操作
• 可考虑手动调整策略参数
• 建议检查市场状况和策略设置

⏰ **封盘时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_reduction_start_notification(self, side: str, qty_total: float, batch_count: int):
        """发送减仓开始通知"""
        side_name = "多头" if side == 'long' else "空头"
        action = "卖出" if side == 'long' else "买入"
        
        message = f"""
🔄 **紧急减仓开始**

📊 **减仓信息**
• 币种: {self.symbol}
• 方向: {side_name}
• 总数量: {qty_total} 张
• 批次: {batch_count} 批
• 动作: {action}

⚡ **执行策略**
• 优先限价单（滑点容忍: {self.emg_slip_cap_bp} 基点）
• 限价单失败时使用市价单
• 每批间隔: {self.emg_batch_sleep_ms} 毫秒

⏰ **开始时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_reduction_early_complete_notification(self, side: str, completed_batches: int, total_batches: int):
        """发送减仓提前完成通知"""
        side_name = "多头" if side == 'long' else "空头"
        
        message = f"""
✅ **紧急减仓提前完成**

📊 **完成情况**
• 币种: {self.symbol}
• 方向: {side_name}
• 已完成批次: {completed_batches}/{total_batches}
• 完成原因: 仓位已降至安全区

🎯 **安全状态**
• 当前仓位已低于退出阈值
• 无需继续减仓操作
• 系统将开始参数恢复流程

⏰ **完成时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_reduction_complete_notification(self, side: str, qty_total: float, batch_count: int):
        """发送减仓完成通知"""
        side_name = "多头" if side == 'long' else "空头"
        action = "卖出" if side == 'long' else "买入"
        
        message = f"""
✅ **紧急减仓执行完成**

📊 **执行结果**
• 币种: {self.symbol}
• 方向: {side_name}
• 总数量: {qty_total} 张
• 批次: {batch_count} 批
• 动作: {action}

🔄 **后续流程**
• 减仓操作已完成
• 系统将开始参数恢复
• 网格开仓将继续暂停

⏰ **完成时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_param_recovery_progress_notification(self, q_progress: float, g_progress: float):
        """发送参数恢复进度通知"""
        message = f"""
🔄 **参数恢复进度**

📊 **恢复状态**
• 币种: {self.symbol}
• 下单量: {q_progress:.1f}%
• 网格间距: {g_progress:.1f}%

⏰ **下次更新**: 5分钟后
"""
        await self._send_telegram_message(message, urgent=False, silent=True)
    
    async def _send_param_recovery_complete_notification(self):
        """发送参数完全恢复通知"""
        base_q = getattr(self, '_initial_quantity_base', self.initial_quantity)
        base_g = getattr(self, '_grid_spacing_base', self.grid_spacing)
        
        message = f"""
✅ **参数恢复完成**

📊 **恢复结果**
• 币种: {self.symbol}
• 下单量: {self.initial_quantity} → {base_q} 张
• 网格间距: {self.grid_spacing:.6f} → {base_g:.6f}

🎯 **系统状态**
• 所有参数已恢复到原始值
• 紧急减仓机制已完全退出
• 网格交易恢复正常运行

⏰ **完成时间**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)