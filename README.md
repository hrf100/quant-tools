# Quant Master Diagnostics v2.0 🚀

量化交易服务器**全链路超级体检与网关优化**诊断工具。

适用于任意量化交易服务器（Linux / macOS / Windows）。  
专为 **Polymarket、Binance、OKX、Deribit** 高频套利与做市场景设计。  
**零外部依赖，纯 Python 标准库实现。**

---

## ⚡ 一行命令执行（免安装，始终最新）

```bash
curl -fsSL "https://raw.githubusercontent.com/hrf100/quant-tools/main/quant_master_diagnostics.py?$(date +%s)" | sudo python3
```

### 参数选项

| 参数 | 说明 |
|------|------|
| _(无参数)_ | 完整检测（含路由追踪，需 root） |
| `--no-trace` | 跳过路由追踪，无需 root 权限 |
| `--fix-clock` | 检测时钟偏移后自动调用系统 NTP 客户端纠正 |

```bash
# 无 root 快速检测
curl -fsSL "..." | python3 - --no-trace

# 自动纠正时钟偏移
curl -fsSL "..." | sudo python3 - --fix-clock
```

---

## 🔍 v2.0 诊断功能

### 1. 系统内核 & 网络栈审计（跨平台自适应）
- TCP 拥塞控制算法（BBR 状态）
- 队列调度 FQ/FQ_Codel
- Socket 收发缓冲区大小
- 文件描述符上限

### 2. 时钟漂移审计 & 自动纠正
- 多源对齐（Binance + OKX），取中位数，提高可信度
- 偏移 > 15ms 自动告警
- `--fix-clock` 自动调用 `chronyc makestep` / `ntpdate` / `w32tm`
- 自动写入 `crontab` 每 5 分钟守护

### 3. Polymarket 合规检测
- 出口 IP / 国家 / 下单白名单状态

### 4. 全节点并发 WebSocket RTT 测量（14 个节点）
- **并发探测**，全程仅需约 5~8 秒
- 每平台自动标注 ★ 最优接入节点

| 平台 | 节点数 | 覆盖 |
|------|--------|------|
| Polymarket | 2 | CLOB 撮合引擎 / 预言机总线 |
| Binance | 6 | 现货主线/CDN/US / 期货主线/欧洲CDN / 交割 |
| OKX | 4 | 主线 / 私有 / AWS 专线 / 爱尔兰欧洲节点 |
| Deribit | 2 | 主线 / 测试网 |

### 5. 全节点路由链路追踪
- 对每个可达节点执行路由追踪
- 自动检测并选用可用工具：`mtr` → `traceroute` → `tracert`
- 显示最后 3 跳，快速定位出海骨干路由

### 6. 动态优化建议
- 根据本次检测结果，输出各平台最优接入节点
- BBR 内核一键开启命令
- CLOB 保活策略与 HTTPS 连接池建议
