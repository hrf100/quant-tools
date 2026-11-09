"""Polymarket 账户、盘口和限价下单。私钥只加密落在本地。"""

import json
import sqlite3
import threading
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from eth_account import Account

from btc_strategy.config import AUTH_DB_PATH

_BASE = Decimal("1000000")


class PolymarketService:

    def __init__(self, db_path: str = None):
        self._path = db_path or AUTH_DB_PATH
        self._lock = threading.Lock()
        self._book_lock = threading.Lock()
        self._client = None
        self._view_cache = None
        self._view_cache_at = 0.0
        self._book_cache = None
        self._orders = deque(maxlen=20)
        self._restore_tried = False
        self._init_db()

    def _connect_db(self):
        return sqlite3.connect(self._path)

    def _init_db(self):
        con = self._connect_db()
        con.execute("""
            CREATE TABLE IF NOT EXISTS polymarket_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enc_blob TEXT NOT NULL,
                signer TEXT,
                wallet TEXT,
                wallet_type TEXT,
                updated_at INTEGER NOT NULL
            )
        """)
        con.commit()
        con.close()

    def connected(self) -> bool:
        with self._lock:
            return self._client is not None

    def ensure(self, wrap_key: bytes):
        with self._lock:
            if self._client is not None or self._restore_tried:
                return
            self._restore_tried = True
        self.restore(wrap_key)

    def restore(self, wrap_key: bytes):
        row = self._load_row()
        if not row:
            return
        try:
            secret = json.loads(_decrypt(wrap_key, row[0]))
        except Exception:
            return
        self._open_client(secret["private_key"], secret.get("wallet") or None)

    def connect(self, private_key: str, wrap_key: bytes, wallet: str | None = None) -> dict:
        private_key = _normalize_key(private_key)
        Account.from_key(private_key)
        wallet = _normalize_addr(wallet)
        self._open_client(private_key, wallet)
        with self._lock:
            client = self._client
        if client is None:
            raise RuntimeError("Polymarket 客户端没有建立")
        creds = client.credentials
        blob = {
            "private_key": private_key,
            "wallet": wallet or "",
            "api_key": creds.key,
            "api_secret": creds.secret,
            "api_passphrase": creds.passphrase,
        }
        con = self._connect_db()
        con.execute(
            """
            INSERT INTO polymarket_account (id, enc_blob, signer, wallet, wallet_type, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enc_blob=excluded.enc_blob,
                signer=excluded.signer,
                wallet=excluded.wallet,
                wallet_type=excluded.wallet_type,
                updated_at=excluded.updated_at
            """,
            (
                _encrypt(wrap_key, json.dumps(blob)),
                str(client.signer),
                str(client.wallet),
                str(client.wallet_type),
                int(time.time()),
            ),
        )
        con.commit()
        con.close()
        self._view_cache = None
        return self.account_view(wrap_key)

    def disconnect(self):
        with self._lock:
            client = self._client
            self._client = None
            self._view_cache = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        con = self._connect_db()
        con.execute("DELETE FROM polymarket_account")
        con.commit()
        con.close()

    def account_view(self, wrap_key: bytes) -> dict:
        self.ensure(wrap_key)
        with self._lock:
            client = self._client
        if client is None:
            return {
                "connected": False,
                "orders": list(self._orders),
            }
        now = time.time()
        if self._view_cache and now - self._view_cache_at < 5:
            cached = dict(self._view_cache)
            cached["orders"] = list(self._orders)
            return cached

        balance = None
        portfolio = None
        balance_error = None
        try:
            bal = client.get_balance_allowance(asset_type="COLLATERAL")
            balance = round(bal.balance / 1_000_000, 4)
        except Exception as exc:
            balance_error = _safe_error(exc)
        try:
            port = client.get_portfolio_value()
            portfolio = round(float(port.value), 4)
        except Exception as exc:
            if not balance_error:
                balance_error = _safe_error(exc)

        view = {
            "connected": True,
            "signer": str(client.signer),
            "deposit_wallet": str(client.wallet),
            "wallet_type": str(client.wallet_type),
            "credentials_saved": True,
            "balance": balance,
            "portfolio_value": portfolio,
            "balance_error": balance_error,
            "orders": list(self._orders),
        }
        self._view_cache = view
        self._view_cache_at = now
        return view

    def book(self, open_ts: int | None) -> dict:
        if not open_ts:
            return {"ready": False, "error": "场次还没开始"}
        with self._book_lock:
            cached = self._book_cache
            if cached and cached[0] == open_ts and time.time() - cached[1] < 1.2:
                return cached[2]
            try:
                payload = _load_book(int(open_ts))
            except Exception as exc:
                payload = {"ready": False, "error": _safe_error(exc), "open_ts": int(open_ts)}
            self._book_cache = (int(open_ts), time.time(), payload)
            return payload

    def place_order(self, wrap_key: bytes, side: str, price, amount) -> dict:
        self.ensure(wrap_key)
        with self._lock:
            client = self._client
        if client is None:
            raise RuntimeError("请先连接钱包")
        side = (side or "").upper()
        if side not in ("UP", "DOWN"):
            raise RuntimeError("方向只能是 UP 或 DOWN")
        amount = Decimal(str(amount))
        price = Decimal(str(price))
        if amount <= 0:
            raise RuntimeError("下单金额要大于 0")
        if price <= 0 or price >= 1:
            raise RuntimeError("价格要在 0 和 1 之间")

        open_ts = int(time.time()) // 300 * 300
        market = self.book(open_ts)
        if not market.get("ready"):
            raise RuntimeError(market.get("error") or "盘口还没准备好")
        leg = market["up"] if side == "UP" else market["down"]
        tick = Decimal(str(leg.get("tick_size") or "0.01"))
        min_size = Decimal(str(leg.get("min_order_size") or "1"))
        price = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
        if price <= 0 or price >= 1:
            raise RuntimeError("价格不在可下单区间")
        shares = (amount / price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if shares < min_size:
            raise RuntimeError(f"份额 {shares} 低于最小下单量 {min_size}")

        result = client.place_limit_order(
            token_id=leg["token_id"],
            price=price,
            size=shares,
            side="BUY",
        )
        if not getattr(result, "ok", False):
            message = getattr(result, "message", None) or "下单被拒绝"
            self._push_order(False, side, price, shares, amount, message)
            raise RuntimeError(message)
        order_id = getattr(result, "order_id", "")
        message = f"已提交 {side} 买单，价格 {price}，份额 {shares}，金额约 {amount} pUSD"
        self._push_order(True, side, price, shares, amount, message, order_id)
        self._view_cache = None
        return {"ok": True, "message": message, "order_id": order_id}

    def _open_client(self, private_key: str, wallet: str | None):
        from polymarket import SecureClient

        kwargs = {"private_key": private_key}
        if wallet:
            kwargs["wallet"] = wallet
        client = SecureClient.create(**kwargs)
        with self._lock:
            old = self._client
            self._client = client
        if old is not None and old is not client:
            try:
                old.close()
            except Exception:
                pass

    def _load_row(self):
        con = self._connect_db()
        row = con.execute(
            "SELECT enc_blob, signer, wallet, wallet_type FROM polymarket_account WHERE id=1"
        ).fetchone()
        con.close()
        return row

    def _push_order(self, ok, side, price, shares, amount, message, order_id=""):
        self._orders.appendleft({
            "ok": ok,
            "side": side,
            "price": str(price),
            "shares": str(shares),
            "amount": str(amount),
            "order_id": order_id,
            "message": message,
            "ts": int(time.time()),
        })


def _load_book(open_ts: int) -> dict:
    from polymarket import PublicClient

    slug = f"btc-updown-5m-{open_ts}"
    with PublicClient() as client:
        found = None
        for market in client.list_markets(slug=slug, page_size=5).iter_items():
            if getattr(market, "slug", None) == slug:
                found = market
                break
        if found is None:
            return {"ready": False, "open_ts": open_ts, "slug": slug, "error": "这一局的 Polymarket 市场还没有出现"}
        up_id = found.outcomes.yes.token_id
        down_id = found.outcomes.no.token_id
        up_book = client.get_order_book(token_id=up_id)
        down_book = client.get_order_book(token_id=down_id)
    return {
        "ready": True,
        "open_ts": open_ts,
        "slug": slug,
        "question": found.question,
        "up": _leg(up_id, "Up", up_book),
        "down": _leg(down_id, "Down", down_book),
    }


def _leg(token_id: str, label: str, book) -> dict:
    asks = list(book.asks)
    bids = list(book.bids)
    best_asks = asks[-8:]
    best_bids = list(reversed(bids[-8:]))
    return {
        "label": label,
        "token_id": str(token_id),
        "tick_size": str(book.tick_size),
        "min_order_size": str(book.min_order_size),
        "last": None if book.last_trade_price is None else str(book.last_trade_price),
        "asks": [{"price": str(level.price), "size": str(level.size)} for level in best_asks],
        "bids": [{"price": str(level.price), "size": str(level.size)} for level in best_bids],
    }


def _normalize_key(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("0X"):
        value = "0x" + value[2:]
    if not value.startswith("0x"):
        value = "0x" + value
    if len(value) != 66:
        raise ValueError("私钥长度不正确")
    int(value, 16)
    return value


def _normalize_addr(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    value = str(value).strip()
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError("中转钱包地址格式不正确")
    int(value[2:], 16)
    return value


def _encrypt(wrap_key: bytes, text: str) -> str:
    import base64
    from cryptography.fernet import Fernet

    token = Fernet(base64.urlsafe_b64encode(wrap_key)).encrypt(text.encode("utf-8"))
    return token.decode("ascii")


def _decrypt(wrap_key: bytes, token: str) -> str:
    import base64
    from cryptography.fernet import Fernet

    raw = Fernet(base64.urlsafe_b64encode(wrap_key)).decrypt(token.encode("ascii"))
    return raw.decode("utf-8")


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 300:
        text = text[:300]
    return text
