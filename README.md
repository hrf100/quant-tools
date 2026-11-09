# Quant Master & Polymarket BTC 5m 量化决策中心 🚀

专为 **Polymarket BTC 5分钟涨跌市场** 设计的全链路量化决策引擎与实时 Web 监控系统。  
包含：
1. **全链路网络/节点体检工具** (`quant_master_diagnostics.py`)
2. **BTC 5m 涨跌实时方向预测策略引擎 & Web 监控面板** (`run_strategy.py`)

**零外部第三方依赖，纯 Python 3.8+ 标准库实现，任何 Linux/Mac/Windows 服务器开箱即用！**

---

## ⚡ 一键在服务器上部署并启动策略

只需在服务器终端执行以下命令（自动拉取最新代码并运行）：

```bash
git clone https://github.com/hrf100/quant-tools.git && cd quant-tools && python3 run_strategy.py
```

若希望在后台持久运行（即使关闭 SSH 终端也不中断）：

```bash
nohup python3 run_strategy.py > strategy.log 2>&1 &
```

启动后，打开浏览器访问：
```text
http://<你的服务器IP>:8888
```
即可看到暗黑主题的高清实时量化大屏！

---

## 📊 Web 实时监控面板特性

### 1. 当前场次方向判定 (Current 5-Min Round)
- **大号决策徽标**：清晰展示本局决策（`UP 看涨` / `DOWN 看跌` / `SKIP 避险观望`）
- **置信度与综合打分**：0 ~ 100% 预测信心指数
- **倒计时进度条**：实时显示当前 5 分钟场次的剩余时间
- **多因子打分分解明细**：
  - `Layer 1: 宏观趋势` (1h EMA-21 均线过滤器)
  - `Layer 2: 结构动能` (5m RSI 超买超卖指标)
  - `Layer 2: 布林通道` (5m Bollinger Bands 轨道位置)
  - `Layer 2: K线形态` (5m 实体方向与成交量放大验证)
  - `Layer 3: 微观斜率` (开盘后 30s 逐秒价格走势斜率)

### 2. 历史场次战绩与准确率 (Historical Settlement)
- **实时统计看板**：总场次、已结算场次、命中准确率百分比、连续命中记录
- **流水明细表**：每局的开盘时间、开盘基准 TWAP、收盘结算 TWAP、预测方向 vs 实际结果对比
- **SQLite 自动持久化**：数据自动存入本地 SQLite 数据库，重启服务不丢战绩

### 3. 服务器设备与数据流健康度 (Hardware & Feeds)
- **CPU 负载与内存占用** 实时仪表盘
- **Binance 现货行情 WebSocket 速率**：接收速率 (msg/s) 与网络流量 (KB/s)
- **Chainlink Polygon RPC 预言机健康度**：链上更新距今秒数、RPC 请求往返延迟 (ms)、当前选中的最优 RPC 节点

---

## 🛠️ 单行网络诊断工具（旧版工具保留）

如果你只需要快速体检服务器网络与时钟：

```bash
curl -fsSL "https://raw.githubusercontent.com/hrf100/quant-tools/main/quant_master_diagnostics.py?$(date +%s)" | sudo python3
```
