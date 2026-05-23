# 客户端配置说明

## 配置协调服务器

有两种方式配置客户端：

### 方式1：直接修改数据库（推荐）

使用 SQLite 工具打开 `data/collector.db`，执行：

```sql
UPDATE config SET value='http://<RELAY_IP_REDACTED>:3031' WHERE key='coordinatorUrl';
UPDATE config SET value='张三-OfficePC' WHERE key='clientId';
UPDATE config SET value='1' WHERE key='coordinatorEnabled';
UPDATE config SET value='strict' WHERE key='coordinatorMode';
```

**重要**：clientId 格式必须为 `姓名-电脑名`，例如：
- 阿强-OfficePC
- 小陈-Laptop
- 张三-Desktop

### 方式2：通过代码配置

在客户端启动后，可以通过 API 配置：

```javascript
// 启用协调器
db.setConfig('coordinatorEnabled', '1');
db.setConfig('coordinatorUrl', 'http://<RELAY_IP_REDACTED>:3031');
db.setConfig('clientId', '你的名字-电脑名');
db.setConfig('coordinatorMode', 'strict');
```

## 配置项说明

- **coordinatorEnabled**: 是否启用协调功能
  - `1` = 启用
  - `0` = 禁用（单机模式）

- **coordinatorUrl**: 协调服务器地址
  - 格式: `http://IP:端口`
  - 示例: `http://<RELAY_IP_REDACTED>:3031`

- **clientId**: 客户端标识
  - 格式: `姓名-电脑名`
  - 必须稳定，不要使用随机ID

- **coordinatorMode**: 工作模式
  - `strict` (推荐): 协调器不可用时禁止新采集
  - `fallback`: 协调器不可用时降级单机模式

## 测试验证

配置完成后，重启客户端，然后：

1. 客户端A采集一个店铺
2. 客户端B尝试采集同一店铺
3. 应该看到提示："店铺正在被采集" 或 "店铺已完成采集"
