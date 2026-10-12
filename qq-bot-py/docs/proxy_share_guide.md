# 可选 Mihomo 代理分享工具

本工具用于已有 Mihomo 服务，给已配置出口增加独立账号密码的 HTTP/SOCKS5 入口。它不安装 Mihomo、不购买上游服务，也不是 RinBot 基础部署的必需步骤。

示例地址和端口需按自己的服务器替换，不包含原部署的域名、出口 IP 或账号。公网入口的账号密码认证不会自动加密客户端到服务器的连接；需要加密时，应另行使用支持加密的传输方式。

## 准备与执行

确认服务器已有可用出口、Mihomo 控制器和服务管理配置。以下命令在 `qq-bot-py` 目录执行，`YOUR_OUTBOUND` 是已经配置的出口名称：

```sh
python3 -u tools/mihomo_share_proxy.py \
  --config /path/to/mihomo/config.yaml \
  --api-url http://127.0.0.1:9090 \
  --service mihomo \
  --outbound YOUR_OUTBOUND \
  --address proxy.example.com \
  --port 17895 \
  --set-user alice --set-user bob
```

将配置路径、出口名、服务名和域名换成实际值。`--address` 只设置凭据信息中的地址，不修改 DNS 或安装 TLS 证书；代理域名应能直接到达服务器，普通网站 HTTP 反向代理不能转发任意 SOCKS5 端口。

程序交互询问每个账号的密码及确认密码，输入不回显，密码至少 12 个字符。若提示缺少 YAML 支持，先在当前 Python 环境安装 PyYAML。不要把密码写入命令、聊天或公开配置。

脚本先检查现有配置、备份并验证修改，再通过控制器热加载。入口不同、端口占用或出口不存在时停止；加载失败会尝试恢复备份。它会检查未认证连接和错误密码被拒绝，并验证每个账号能认证。成功时显示 `Share proxy is ready`；认证成功仍需单独确认真实出口能访问目标网站。

## 账号与凭据

重复 `--set-user 账号` 可添加或更新多个账号；其他账号与上游凭据保留。再次运行时也应显式指定自己的 `--config`、`--outbound` 和 `--address`。不传 `--set-user` 时保留已有入口的账号密码；默认执行不会打印密码。

所有分享凭据保存于服务器私有文件 `/root/proxy-share-17895.txt`，端口变更时文件名随端口变化，权限为 `600`。每个使用者只应收到自己的账号密码，不要发送包含所有凭据的文件。

账号共用同一出口和带宽。修改密码影响后续认证，不保证关闭已经建立的连接。

## 客户端配置

在服务器安全组与已启用的防火墙中放行自己选择的入口端口；不要为分享入口开放 Mihomo 管理端口。以端口 `17895` 为例：

```sh
ufw status
ufw allow 17895/tcp
```

客户端需要填写协议、服务器地址、端口、账号和对应密码。支持配置导入的 Mihomo 客户端可使用以下示例；已有订阅可只添加节点及策略组，不必覆盖整份配置。

```yaml
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
proxies:
  - name: 分享出口
    type: socks5
    server: proxy.example.com
    port: 17895
    username: alice
    password: "替换为该账号的密码"
    udp: false
proxy-groups:
  - name: 出口选择
    type: select
    proxies: [分享出口, DIRECT]
rules:
  - MATCH,出口选择
```

本工具分享链路以 TCP 为主。需要 UDP 的游戏、语音或其他应用应另外核对出口与客户端支持，不能仅凭网页可访问判断。

## 排查

客户端可用 curl 检查连接，省略密码会由终端询问，避免密码进入命令历史；PowerShell 使用 `curl.exe`：

```sh
curl --noproxy "" --proxy socks5h://proxy.example.com:17895 \
  --proxy-user alice --connect-timeout 10 --max-time 30 https://api.ipify.org
```

返回地址应为实际出口，与部署地区或旧日志没有固定对应关系。超时先检查域名、端口与防火墙；认证失败检查分享账号；能连接但不能访问时检查原出口与前置节点。

服务器可查看监听和服务日志：

```sh
ss -lntp | grep ':17895'
journalctl -u mihomo --since "5 minutes ago" --no-pager
```

## 参考

- [Mihomo 入口配置](https://wiki.metacubex.one/config/inbound/listeners/mixed/)
- [Mihomo SOCKS5 节点](https://wiki.metacubex.one/config/proxies/socks/)
- [FlClash 项目](https://github.com/chen08209/FlClash)
