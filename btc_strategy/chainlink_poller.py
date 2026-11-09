# btc_strategy/chainlink_poller.py
"""
每 N 秒通过 Polygon JSON-RPC 读取 Chainlink BTC/USD latestRoundData。
- 多节点故障自动转移 (Failover)
- 零依赖，纯 http.client + 二进制 ABI 解码
- 严格验证 HTTP 状态与 JSON-RPC 结果，杜绝报错假存活
"""

import http.client
import json
import ssl
import threading
import time

from btc_strategy.config import (
    CHAINLINK_BTC_USD_POLYGON,
    LATEST_ROUND_DATA_SELECTOR,
    POLYGON_RPC_NODES,
    CHAINLINK_POLL_INTERVAL,
)


class ChainlinkPoller:

    def __init__(self):
        self._lock        = threading.Lock()
        self._thread      = None
        self._running     = False
        # 状态
        self.price        = 0.0
        self.round_id     = 0
        self.updated_at   = 0
        self.age_sec      = 9999.0
        self.rpc_host     = ""
        self.rpc_path     = ""
        self.rpc_rtt_ms   = 0.0
        self.fetch_count  = 0
        self.error_count  = 0
        self.last_ok      = 0.0
        self._consecutive_fails = 0
        
        # 节点池索引
        self._node_idx    = 0
        self._best_host   = POLYGON_RPC_NODES[0][0]
        self._best_port   = POLYGON_RPC_NODES[0][1]
        self._best_path   = POLYGON_RPC_NODES[0][2]
        self.rpc_host     = self._best_host

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_state(self) -> dict:
        with self._lock:
            now_age = round(time.time() - self.updated_at, 1) if self.updated_at > 0 else 9999.0
            return {
                "price":       round(self.price, 2),
                "round_id":    self.round_id,
                "updated_at":  self.updated_at,
                "age_sec":     now_age,
                "rpc_host":    self.rpc_host,
                "rpc_rtt_ms":  round(self.rpc_rtt_ms, 1),
                "fetch_count": self.fetch_count,
                "error_count": self.error_count,
                "last_ok":     round(self.last_ok, 1),
            }

    # ── 内部逻辑 ──────────────────────────────────────────────────────────────

    def _run(self):
        # 立即执行一次，开机秒出数据
        self._fetch_once()
        # 后台选最优可用节点
        self._select_best_rpc()
        while self._running:
            self._fetch_once()
            time.sleep(CHAINLINK_POLL_INTERVAL)

    def _select_best_rpc(self):
        best_rtt  = 9999.0
        best_node = None
        for i, (host, port, path) in enumerate(POLYGON_RPC_NODES):
            rtt = self._ping_rpc(host, port, path)
            if rtt < best_rtt:
                best_rtt  = rtt
                best_node = (host, port, path)
                self._node_idx = i

        if best_node:
            self._best_host, self._best_port, self._best_path = best_node
            with self._lock:
                self.rpc_host = self._best_host

    def _switch_to_next_rpc(self):
        """故障转移：自动切换到下一个候选节点"""
        self._node_idx = (self._node_idx + 1) % len(POLYGON_RPC_NODES)
        self._best_host, self._best_port, self._best_path = POLYGON_RPC_NODES[self._node_idx]
        with self._lock:
            self.rpc_host = self._best_host
        self._consecutive_fails = 0

    def _ping_rpc(self, host, port, path) -> float:
        """真实测试节点是否返回有效区块数据"""
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}).encode()
        try:
            ctx  = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=4)
            t0   = time.time()
            conn.request("POST", path, body=payload,
                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            if resp.status != 200:
                conn.close()
                return 9999.0
            body = json.loads(resp.read().decode())
            conn.close()
            if "result" in body:
                return (time.time() - t0) * 1000
            return 9999.0
        except Exception:
            return 9999.0

    def _fetch_once(self):
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{"to": CHAINLINK_BTC_USD_POLYGON,
                          "data": LATEST_ROUND_DATA_SELECTOR}, "latest"],
            "id":      1,
        }).encode()
        try:
            ctx  = ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                self._best_host, self._best_port, context=ctx, timeout=5)
            t0   = time.time()
            conn.request("POST", self._best_path, body=payload,
                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            if resp.status != 200:
                conn.close()
                self._handle_error()
                return
            body = json.loads(resp.read().decode())
            rtt  = (time.time() - t0) * 1000
            conn.close()

            result = body.get("result")
            parsed = self._decode(result)
            if parsed:
                rid, price, _, updated_at, _ = parsed
                with self._lock:
                    self.price       = price
                    self.round_id    = rid
                    self.updated_at  = updated_at
                    self.age_sec     = time.time() - updated_at
                    self.rpc_rtt_ms  = rtt
                    self.fetch_count += 1
                    self.last_ok     = time.time()
                    self._consecutive_fails = 0
            else:
                self._handle_error()
        except Exception:
            self._handle_error()

    def _handle_error(self):
        with self._lock:
            self.error_count += 1
            self._consecutive_fails += 1
        if self._consecutive_fails >= 2:
            self._switch_to_next_rpc()

    @staticmethod
    def _decode(hex_result: str):
        if not hex_result or hex_result == "0x":
            return None
        try:
            data = bytes.fromhex(hex_result[2:])
            if len(data) < 160:
                return None
            round_id   = int.from_bytes(data[0:32],   "big")
            answer     = int.from_bytes(data[32:64],  "big")
            started_at = int.from_bytes(data[64:96],  "big")
            updated_at = int.from_bytes(data[96:128], "big")
            answered   = int.from_bytes(data[128:160],"big")
            if answer >= (1 << 255):
                answer -= (1 << 256)
            return round_id, answer / 1e8, started_at, updated_at, answered
        except Exception:
            return None
