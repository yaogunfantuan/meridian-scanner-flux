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

- `PCP_NET`：LOCAL 曲面存在优势，且同执行价 Call/Put 的可成交 Put-Call Parity
  在扣除两条期权开平 Taker 费和 Delta 对冲开平费后仍为正。这是优先级最高的组合信号。
- `LOCAL_NET`：目标合约自身相对 leave-one-strike-out 局部曲面存在优势，手续费按当前
  这笔成交的 Taker 费乘 2（进场一次、退出一次）后仍为正。另一侧
  Call/Put 报价不会否决它；这是等待报价恢复的
  单腿回归信号，不等同于无风险套利。
- LOCAL 拟合默认只使用距离目标 `log(K/F)` 不超过 `0.20` 的参考点，并要求最近左右
  参考点跨度不超过 `0.30`。不满足时视为稀疏曲面，不生成 LOCAL 候选，避免跨越大段
  无流动性执行价做直线插值。
- `quality=PAIR_CONFLICT` 表示同执行价另一种期权的 IV 不支持当前方向；
  `quality=EXIT_THIN` 表示当前反向盘口深度不足。两者只降低单腿回归置信度，不会用
  对侧期权否决 LOCAL，也不会改变 PCP 的价格计算。
- `1-tick`：最佳 ask 与 bid 相差恰好一个最小价格单位；仅当具有 LOCAL 方向或至少偏离
  mark 5 ticks 时进入提醒。
- 可执行候选不要求价差为 1 tick。同时满足两者的合约只在候选表中显示，并标记
  `1tick=True`。
- mark 只作为辅助诊断，不单独充当 P1 公允价值。

期权手续费优先使用交易所公开合约字段；Bybit 使用公开的非 VIP 期权费率与费率上限。
`PCP_NET` 的 Delta 对冲默认按往返 Taker 0.05% 估算，可按账户实际费率修改：

```bash
python3 option_alert_daemon.py --hedge-taker-rate 0.0003 --hedge-leverage 10
```

LOCAL 稀疏度与质量门槛也可以按市场调整：

```bash
python3 option_alert_daemon.py \
  --max-neighbor-log-distance 0.20 \
  --max-bracket-log-span 0.30 \
  --pair-iv-tolerance 0.005 \
  --min-exit-depth 0.1
```

单腿输出将 `local_gross_$`、`fee_2x_$` 和 `local_net_$` 分列，便于同时观察
曲面回归毛空间与成本。`pcp_net_$` 是完整组合按当前可成交数量计算的手续费后金额；`capital_$`
是普通保证金口径的保守估算，`ret_bp` 是单次净收益/估算资金占用。组合保证金取决于账户
现有仓位，扫描器不会把公共行情推导出的估值冒充真实账户保证金。资金费未来路径未知，
因此不进入 `PCP_NET` 硬过滤，也不输出误导性的固定年化；短线回归应使用实际持仓时长，
到期策略则应另行加入匹配期限的对冲和融资成本。

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

## CI 与 GitHub Release

推送到 `main` 或创建 Pull Request 时，GitHub Actions 会在 Python 3.9 和 3.12 上运行
静态检查及无网络单元测试。普通提交不会创建部署版本。

准备发布时创建不可变的语义化版本标签：

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

`.github/workflows/release.yml` 会自动重新检查代码，调用 `build_release.sh`，创建 GitHub
Release 并上传：

```text
meridian-scanner-flux-v0.1.0.tar.gz
SHA256SUMS
```

目标机器可以使用 GitHub CLI 下载并校验：

```bash
gh release download v0.1.0 --repo yaogunfantuan/meridian-scanner-flux
shasum -a 256 -c SHA256SUMS   # Linux 也可使用 sha256sum -c
tar -xzf meridian-scanner-flux-v0.1.0.tar.gz
cd meridian-scanner-flux-v0.1.0
./install.sh --start
```

私有仓库需要先在目标机器完成 `gh auth login`，或为 GitHub CLI 配置只读细粒度 Token。
如果 Release 工作流暂时失败，可以在 Actions 页面重试；不要强制移动已经发布的版本标签。
