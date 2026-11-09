"""本地单用户登录。没有账号时允许首次设置。"""

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

from btc_strategy.config import AUTH_DB_PATH

PBKDF2_ROUNDS = 200_000
SESSION_TTL_SEC = 7 * 24 * 3600


class AuthStore:

    def __init__(self, db_path: str = None):
        self._path = db_path or AUTH_DB_PATH
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self._path)

    def _init_db(self):
        con = self._connect()
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username   TEXT PRIMARY KEY,
                pass_hash  BLOB NOT NULL,
                pass_salt  BLOB NOT NULL,
                wrap_salt  BLOB NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.commit()
        con.close()

    def has_user(self) -> bool:
        con = self._connect()
        row = con.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        con.close()
        return row is not None

    def status(self, token: str | None) -> dict:
        session = self.session(token)
        return {
            "needs_setup": not self.has_user(),
            "authenticated": session is not None,
            "username": session["username"] if session else None,
        }

    def setup(self, username: str, password: str) -> str:
        username = (username or "").strip()
        if len(username) < 1 or len(username) > 32:
            raise ValueError("用户名需要 1 到 32 个字符")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        with self._lock:
            if self.has_user():
                raise ValueError("已经设置过账号，请直接登录")
            pass_salt = os.urandom(16)
            wrap_salt = os.urandom(16)
            con = self._connect()
            con.execute(
                "INSERT INTO users (username, pass_hash, pass_salt, wrap_salt, created_at) VALUES (?,?,?,?,?)",
                (username, _hash_password(password, pass_salt), pass_salt, wrap_salt, int(time.time())),
            )
            con.commit()
            con.close()
        return self._open_session(username, password, wrap_salt)

    def login(self, username: str, password: str) -> str:
        username = (username or "").strip()
        con = self._connect()
        row = con.execute(
            "SELECT pass_hash, pass_salt, wrap_salt FROM users WHERE username=?",
            (username,),
        ).fetchone()
        con.close()
        if not row:
            raise ValueError("用户名或密码不正确")
        pass_hash, pass_salt, wrap_salt = row
        if not hmac.compare_digest(pass_hash, _hash_password(password, pass_salt)):
            raise ValueError("用户名或密码不正确")
        return self._open_session(username, password, wrap_salt)

    def logout(self, token: str | None):
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._lock:
            item = self._sessions.get(token)
            if not item:
                return None
            if item["exp"] < time.time():
                self._sessions.pop(token, None)
                return None
            return item

    def get_settings(self) -> dict:
        con = self._connect()
        row = con.execute("SELECT value FROM settings WHERE key='order_amount'").fetchone()
        con.close()
        amount = 5.0
        if row:
            try:
                amount = float(row[0])
            except ValueError:
                amount = 5.0
        if amount <= 0:
            amount = 5.0
        return {"order_amount": amount}

    def save_settings(self, order_amount) -> dict:
        try:
            amount = round(float(order_amount), 2)
        except (TypeError, ValueError):
            raise ValueError("下单金额不正确")
        if amount <= 0 or amount > 1_000_000:
            raise ValueError("下单金额要大于 0")
        con = self._connect()
        con.execute(
            """
            INSERT INTO settings (key, value) VALUES ('order_amount', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(amount),),
        )
        con.commit()
        con.close()
        return {"order_amount": amount}

    def wrap_key(self, token: str | None) -> bytes | None:
        item = self.session(token)
        if not item:
            return None
        return item["wrap_key"]

    def _open_session(self, username: str, password: str, wrap_salt: bytes) -> str:
        token = secrets.token_urlsafe(32)
        wrap_key = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), wrap_salt, PBKDF2_ROUNDS, dklen=32
        )
        with self._lock:
            self._sessions[token] = {
                "username": username,
                "exp": time.time() + SESSION_TTL_SEC,
                "wrap_key": wrap_key,
            }
        return token


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS, dklen=32
    )
