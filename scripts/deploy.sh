#!/bin/bash

# 网格交易机器人 Docker 部署脚本
# 使用方法: ./deploy.sh [start|stop|restart|logs|build|status]

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目配置
PROJECT_NAME="grid-trading-bot"
CONTAINER_NAME="grid-trader"
IMAGE_NAME="grid-trading-bot:latest"

# 函数定义
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_env_file() {
    if [ ! -f "config/.env" ]; then
        print_warning ".env 文件不存在，正在从示例文件创建..."
        if [ -f "config/env.example" ]; then
            cp config/env.example .env
            print_info "请编辑 .env 文件并设置你的 API 密钥"
            return 1
        else
            print_error "config/env.example 文件不存在，无法创建 .env 文件"
            return 1
        fi
    fi
    return 0
}

create_directories() {
    print_info "创建必要的目录..."
    mkdir -p log
}

build_image() {
    print_info "构建 Docker 镜像..."
    docker build -t $IMAGE_NAME .
    print_success "Docker 镜像构建完成"
}

start_container() {
    if ! check_env_file; then
        print_error "请先配置 .env 文件"
        exit 1
    fi
    
    create_directories
    
    if [ "$(docker ps -q -f name=$CONTAINER_NAME)" ]; then
        print_warning "容器已在运行，正在重启..."
        docker-compose -f docker/docker-compose.yml restart
    else
        print_info "启动网格交易机器人..."
        docker-compose -f docker/docker-compose.yml up -d
    fi
    
    print_success "网格交易机器人已启动"
    print_info "使用 './deploy.sh logs' 查看日志"
}

stop_container() {
    print_info "停止网格交易机器人..."
    docker-compose -f docker/docker-compose.yml down
    print_success "网格交易机器人已停止"
}

restart_container() {
    print_info "重启网格交易机器人..."
    docker-compose -f docker/docker-compose.yml restart
    print_success "网格交易机器人已重启"
}

show_logs() {
    print_info "显示容器日志..."
    docker-compose -f docker/docker-compose.yml logs -f --tail=100
}

show_status() {
    print_info "容器状态:"
    docker-compose -f docker/docker-compose.yml ps
    echo
    
    if [ "$(docker ps -q -f name=$CONTAINER_NAME)" ]; then
        print_info "容器健康状态:"
        docker inspect --format='{{.State.Health.Status}}' $CONTAINER_NAME 2>/dev/null || echo "无健康检查信息"
        
        print_info "资源使用情况:"
        docker stats $CONTAINER_NAME --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"
    fi
}

cleanup() {
    print_info "清理未使用的 Docker 资源..."
    docker system prune -f
    print_success "清理完成"
}

# 多币种相关函数
start_multi_container() {
    if ! check_env_file; then
        print_error "请先配置 .env 文件"
        exit 1
    fi
    
    # 检查配置文件
    if [ ! -f "config/symbols.yaml" ] && [ ! -f "config/symbols.json" ]; then
        print_error "配置文件不存在，请创建 config/symbols.yaml 或 config/symbols.json"
        exit 1
    fi
    
    create_directories
    
    if [ "$(docker ps -q -f name=$CONTAINER_NAME)" ]; then
        print_warning "容器已在运行，正在重启..."
        docker-compose -f docker/docker-compose.yml restart
    else
        print_info "启动多币种网格交易机器人..."
        # 设置环境变量指示使用多币种模式
        export GRID_MODE="multi"
        docker-compose -f docker/docker-compose.yml up -d
    fi
    
    print_success "多币种网格交易机器人已启动"
    print_info "使用 './deploy.sh multi-logs' 查看汇总日志"
}

show_multi_logs() {
    print_info "显示多币种汇总日志..."
    if [ -f "log/status_summary.log" ]; then
        echo "=== 状态汇总日志 ==="
        tail -f log/status_summary.log
    else
        print_warning "状态汇总日志文件不存在"
        show_logs
    fi
}

show_bot_logs() {
    print_info "显示币种详细日志..."
    if [ -d "log" ]; then
        echo "可用的币种日志文件:"
        ls -la log/grid_BN_*.log 2>/dev/null || echo "暂无币种日志文件"
        echo ""
        echo "查看特定币种日志: tail -f log/grid_BN_[币种].log"
        echo "例如: tail -f log/grid_BN_BTCUSDT.log"
    else
        print_warning "日志目录不存在"
    fi
}

# 主逻辑
case "${1:-start}" in
    "build")
        build_image
        ;;
    "start")
        start_container
        ;;
    "multi-start")
        start_multi_container
        ;;
    "stop")
        stop_container
        ;;
    "restart")
        restart_container
        ;;
    "logs")
        show_logs
        ;;
    "multi-logs")
        show_multi_logs
        ;;
    "bot-logs")
        show_bot_logs
        ;;
    "status")
        show_status
        ;;
    "cleanup")
        cleanup
        ;;
    "help" | "--help" | "-h")
        echo "使用方法: $0 [命令]"
        echo ""
        echo "可用命令:"
        echo "  build       - 构建 Docker 镜像"
        echo "  start       - 启动单币种交易机器人 (默认)"
        echo "  multi-start - 启动多币种交易机器人"
        echo "  stop        - 停止交易机器人"
        echo "  restart     - 重启交易机器人"
        echo "  logs        - 查看容器日志"
        echo "  multi-logs  - 查看多币种汇总日志"
        echo "  bot-logs    - 查看币种详细日志"
        echo "  status      - 查看状态"
        echo "  cleanup     - 清理 Docker 资源"
        echo "  help        - 显示此帮助信息"
        ;;
    *)
        print_error "未知命令: $1"
        print_info "使用 '$0 help' 查看可用命令"
        exit 1
        ;;
esac 