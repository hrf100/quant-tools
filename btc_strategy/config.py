# btc_strategy/config.py
# ─── 所有可调参数集中在此 ───────────────────────────────────────────────────────

# Binance WebSocket
BINANCE_WS_HOST = "stream.binance.com"
BINANCE_WS_PORT = 9443
BINANCE_WS_PATH = "/ws/btcusdt@aggTrade"

# Chainlink / Polygon RPC (全部精选免 API Key、高可用节点)
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6F"
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
POLYGON_RPC_NODES = [
    ("1rpc.io",                        443, "/matic"),
    ("polygon-bor-rpc.publicnode.com", 443, "/"),
    ("polygon.drpc.org",               443, "/"),
]
CHAINLINK_POLL_INTERVAL = 3   # seconds

# 蜡烛参数
CANDLE_MAX_COUNT  = 300       # 每个周期最多保留多少根蜡烛

# 信号参数
SIGNAL_THRESHOLD      = 30    # |score| 低于此值 → SKIP（满分100）
MICRO_SLOPE_MIN_SEC   = 20    # 开盘后等20秒即开始融合微斜率信号
TWAP_WINDOW_SEC       = 60    # 开/收盘 TWAP 采样窗口（秒）
ROUND_DURATION_SEC    = 300   # 每局时长（5分钟）

# Web 仪表盘
WEB_HOST = "0.0.0.0"
WEB_PORT = 8888

# 数据库
DB_PATH = "quant_rounds.db"
