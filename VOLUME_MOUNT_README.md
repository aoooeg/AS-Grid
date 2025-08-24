# 卷挂载配置说明

## 问题描述

之前出现的问题是：程序在容器内写入装死状态文件到 `/app/src/multi_bot/state/` 目录，但由于没有配置卷挂载，这些文件不会同步到宿主机的 `/home/opc/grid/src/multi_bot/state/` 目录。

这导致：
- 日志显示"已写入装死状态文件"
- 但宿主机上的文件没有更新
- 重启容器后状态丢失

## 解决方案

### 1. 已配置的卷挂载

在 `docker/docker-compose.yml` 中添加了状态文件目录的卷挂载：

```yaml
volumes:
  - ../src/multi_bot/state:/app/src/multi_bot/state  # 装死状态文件目录
```

### 2. 目录结构映射

```
宿主机路径: /home/opc/grid/src/multi_bot/state/
容器内路径: /app/src/multi_bot/state/
```

### 3. 使用方法

#### 启动容器
```bash
# 使用部署脚本（推荐）
./scripts/deploy.sh start

# 或手动使用 docker-compose
docker-compose -f docker/docker-compose.yml --env-file config/.env up -d
```

#### 验证卷挂载
```bash
# 检查容器状态
./scripts/deploy.sh status

# 查看容器日志
./scripts/deploy.sh logs

# 检查状态文件
ls -la src/multi_bot/state/
```

### 4. 测试卷挂载

运行测试脚本验证卷挂载是否正常工作：

```bash
python3 test_volume_mount.py
```

### 5. 注意事项

1. **权限问题**：确保宿主机目录有正确的读写权限
2. **目录存在**：部署脚本会自动创建必要的目录
3. **重启容器**：修改卷挂载配置后需要重新启动容器
4. **数据同步**：容器内和宿主机的状态文件会实时同步

### 6. 故障排除

如果卷挂载不工作：

1. 检查容器状态：`docker ps`
2. 检查卷挂载：`docker inspect grid-trader | grep -A 10 Mounts`
3. 检查目录权限：`ls -la src/multi_bot/state/`
4. 查看容器日志：`docker logs grid-trader`

### 7. 重启容器

修改配置后需要重启容器：

```bash
./scripts/deploy.sh restart
```

## 总结

通过配置卷挂载，现在：
- ✅ 程序写入容器内的状态文件会同步到宿主机
- ✅ 宿主机可以直接查看和编辑状态文件
- ✅ 重启容器后状态不会丢失
- ✅ 日志显示和实际文件状态保持一致
