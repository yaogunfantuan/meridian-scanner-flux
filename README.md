# 多交易所期权曲面扫描器

使用 Derive、Bybit 和 Gate.io 的公开 REST API 扫描可成交期权报价。交易所行情只在各自
内部构建曲面，不会跨交易所混合。

## 代码结构

```text
option_alert_daemon.py   常驻循环、目录缓存、日志和飞书提醒
exchange_adapters.py     Derive、Bybit、Gate REST 适配与字段标准化
option_scanner_core.py   LOCAL 曲面、Put-Call Parity、P1 和 1-tick 算法
scanner_http.py          gzip 响应解压与流量统计
```

运行只需要 Python 3.9+，不依赖第三方包，也不需要交易所 API Key。

## 信号分类

- `P1`：LOCAL 曲面存在可成交优势，并得到 Put-Call Parity、配对数量和 Vega 门槛确认。
- `1-tick`：最佳 ask 与 bid 相差恰好一个最小价格单位；仅当具有 LOCAL 方向或至少偏离
  mark 5 ticks 时进入提醒。
- P1 不要求价差为 1 tick。同时满足两者的合约只在 P1 中显示，并标记 `1tick=True`。
- mark 只作为辅助诊断，不单独充当 P1 公允价值。

## 单轮扫描

扫描所有交易所和全部期权标的：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -u option_alert_daemon.py \
  --once \
  --dry-run
```

只扫描 Derive：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -u option_alert_daemon.py \
  --exchange derive \
  --once \
  --dry-run
```

只扫描某个标的：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -u option_alert_daemon.py \
  --underlying BTC \
  --once \
  --dry-run
```

## 每分钟扫描

程序使用定时 REST 快照，不订阅 WebSocket。合约目录默认缓存 6 小时；每轮串行调度，
上一轮未结束时不会启动下一轮。

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -u option_alert_daemon.py \
  --interval 60 \
  --catalog-ttl 21600 \
  --dry-run
```

默认文件：

- 合约缓存：`work/option_catalog.json`
- 扫描日志：`outputs/option_alerts.jsonl`

扫描日志默认达到 100 MiB 时轮转为 `option_alerts.jsonl.1`，只保留当前文件和上一份。
可通过 `--max-log-mb` 修改；设置为 `0` 表示不轮转。

HTTP 客户端请求 gzip 压缩，并显示每轮压缩响应体大小及每日估算流量。估算不包含 TLS、
HTTP 头和请求上传流量，因此是近似下限。

## 飞书提醒

在飞书群添加自定义机器人并开启签名校验，然后设置环境变量：

```bash
export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
export FEISHU_WEBHOOK_SECRET='签名密钥'

SSL_CERT_FILE=/etc/ssl/cert.pem python3 -u option_alert_daemon.py \
  --interval 60 \
  --catalog-ttl 21600 \
  --notify
```

每轮最多发送一条合并消息。当前不做连续轮次确认，也没有提醒冷却；同一机会如果持续
存在，会每分钟再次发送。Webhook 和签名密钥只从环境变量读取，不会写入缓存或日志。

## 部署到其他机器

项目没有第三方 Python 依赖，推荐直接使用随附的用户级安装脚本。它会将代码、配置、
状态分别安装到：

```text
~/.local/share/option-scanner    程序
~/.config/option-scanner         配置与飞书密钥（权限 0600）
~/.local/state/option-scanner    合约缓存、JSONL 和服务日志
```

在目标机器解压代码后执行：

```bash
./install.sh
```

默认只安装 `systemd --user` 或 macOS LaunchAgent，不会立即启动。编辑配置：

```bash
vi ~/.config/option-scanner/option-scanner.env
```

确认 `SCANNER_NOTIFY`、Webhook 和 Secret 后启动或升级：

```bash
./install.sh --start
```

Linux 无桌面的长期运行账户还应启用 linger，否则用户退出后服务可能停止：

```bash
sudo loginctl enable-linger "$USER"
```

macOS 使用 LaunchAgent，适合保持登录的用户会话；真正无用户登录的机器应改用需要管理员
权限的 LaunchDaemon。只有在已经使用 Docker/Kubernetes 管理其他服务时才建议容器化；
本项目仅四个标准库 Python 文件，直接交给系统服务管理更轻量。

查看状态：

```bash
# Linux
systemctl --user status option-scanner
journalctl --user -u option-scanner -f

# macOS
launchctl print "gui/$(id -u)/com.local.option-scanner"
tail -f ~/.local/state/option-scanner/service.stderr.log
```

卸载程序但保留配置和历史：

```bash
./install.sh --uninstall
```

同时清除配置、缓存和日志：

```bash
./install.sh --uninstall --purge
```
