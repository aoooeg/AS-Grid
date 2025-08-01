#!/bin/bash

# 设置时区
export TZ=UTC

# 根据EXCHANGE环境变量选择运行哪个脚本
if [ "$EXCHANGE" = "binance" ]; then
    echo "启动币安网格交易机器人..."
    python grid_BN.py
elif [ "$EXCHANGE" = "gate" ]; then
    echo "启动Gate.io网格交易机器人..."
    python grid_Gate.py
else
    echo "未指定交易所或交易所不支持，默认启动Gate.io机器人..."
    python grid_Gate.py
fi 