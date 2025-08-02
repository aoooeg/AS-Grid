import asyncio
import os
import logging
from dotenv import load_dotenv
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'multi_bot'))
from binance_multi_bot import BinanceGridBot

# 加载环境变量
load_dotenv()

# 配置验证
def validate_config():
    """验证配置参数"""
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    
    if not api_key or not api_secret:
        raise ValueError("API_KEY 和 API_SECRET 必须设置")
    
    grid_spacing = float(os.getenv("GRID_SPACING", "0.001"))
    if grid_spacing <= 0 or grid_spacing >= 1:
        raise ValueError("GRID_SPACING 必须在 0 到 1 之间")
    
    initial_quantity = int(os.getenv("INITIAL_QUANTITY", "3"))
    if initial_quantity <= 0:
        raise ValueError("INITIAL_QUANTITY 必须大于 0")
    
    leverage = int(os.getenv("LEVERAGE", "20"))
    if leverage <= 0 or leverage > 100:
        raise ValueError("LEVERAGE 必须在 1 到 100 之间")
    
    # 验证Telegram配置
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enable_notifications = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
    
    if enable_notifications:
        if not telegram_bot_token or not telegram_chat_id:
            print("警告: Telegram通知已启用但缺少BOT_TOKEN或CHAT_ID，将禁用通知功能")
        else:
            print("Telegram通知功能已启用")
    
    coin_name = os.getenv("COIN_NAME", "XRP")
    contract_type = os.getenv("CONTRACT_TYPE", "USDT")
    
    print(f"配置验证通过 - 币种: {coin_name}, 网格间距: {grid_spacing}, 初始数量: {initial_quantity}")

async def main():
    try:
        # 验证配置
        validate_config()
        
        # 从环境变量获取配置
        api_key = os.getenv("API_KEY", "")
        api_secret = os.getenv("API_SECRET", "")
        coin_name = os.getenv("COIN_NAME", "XRP")
        contract_type = os.getenv("CONTRACT_TYPE", "USDT")
        grid_spacing = float(os.getenv("GRID_SPACING", "0.001"))
        initial_quantity = int(os.getenv("INITIAL_QUANTITY", "3"))
        leverage = int(os.getenv("LEVERAGE", "20"))
        
        # 构建配置字典
        config = {
            'grid_spacing': grid_spacing,
            'initial_quantity': initial_quantity,
            'leverage': leverage,
            'contract_type': contract_type
        }
        
        # 构建交易对符号
        symbol = f"{coin_name}{contract_type}"
        
        # 创建并启动交易机器人
        bot = BinanceGridBot(symbol=symbol, api_key=api_key, api_secret=api_secret, config=config)
        print("网格交易机器人启动中...")
        await bot.start()
        
    except ValueError as e:
        print(f"配置错误: {e}")
        exit(1)
    except KeyboardInterrupt:
        print("收到停止信号，正在关闭机器人...")
        if 'bot' in locals():
            await bot.stop()
    except Exception as e:
        print(f"运行时错误: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
