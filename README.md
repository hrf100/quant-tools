# Quant Master Diagnostics 🚀

芬兰/欧洲量化交易服务器全链路体检与网关优化诊断工具。

专为 **Polymarket（预测市场）、Binance（币安）与 OKX（欧易）** 的高频套利、做市策略服务器设计。

---

## ⚡ 一行命令执行（免安装，开箱即用）

在任何 Linux 服务器终端中，直接复制运行以下命令即可：

```bash
curl -fsSL "https://raw.githubusercontent.com/hrf100/quant-tools/main/quant_master_diagnostics.py?$(date +%s)" | sudo python3
```

---

## 🔍 诊断功能涵盖

1. **Linux 系统内核与网络栈审计**
   - TCP 拥塞控制算法检测（BBR 状态）
   - 公平队列管理检测（FQ / FQ_Codel）
   - 文件描述符上限（`ulimit -n`）检测
   - 毫秒级对齐币安原子钟，计算本地系统时钟绝对漂移值

2. **Polymarket 合规与风控拦截探测**
   - 探测服务器公网出口 IP 与国家代码
   - 官方 `/api/geoblock` 下单与交易白名单准入状态检测

3. **核心交易所底层 WebSocket RTT 测量**
   - 采用纯 Python 底层实现 RFC 6455 协议栈（零外部依赖）
   - 直接发送底层 `0x89` 二进制 Ping 控制帧，精准测量物理链路 RTT
   - 覆盖节点：
     - Polymarket CLOB 撮合引擎 (`ws-subscriptions-clob.polymarket.com`)
     - Polymarket 预言机实时数据总线 (`ws-live-data.polymarket.com`)
     - Binance 现货极速行情专线 (`data-stream.binance.vision`)
     - OKX 公共行情数据流 (`ws.okx.com:8443`)

4. **骨干网路由特征审计（TCP Traceroute）**
   - 基于 TCP 443 端口探测出海关键网关的最后一跳路由特征

5. **高频量化实战优化建议**
   - 价格发现源定位
   - 长连接保活与 Keep-Alive 连接池复用策略
