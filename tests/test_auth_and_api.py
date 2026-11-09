import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from btc_strategy.auth_store import AuthStore
from btc_strategy.polymarket_service import PolymarketService, _normalize_key
from btc_strategy.traffic_stats import TrafficStats
from btc_strategy.web_server import ThreadedHTTPServer, QuantRequestHandler


class _Rounds:
    def get_current(self):
        return {"open_ts": 0}


class _Ctx:
    def __init__(self, traffic):
        self.traffic = traffic
        self.rounds = _Rounds()

    def get_full_status(self):
        return {"ok": True, "traffic": self.traffic.snapshot(0), "feed_stats": {}, "chainlink": {}}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "auth.db")
        self.auth = AuthStore(db)
        self.traffic = TrafficStats()
        self.poly = PolymarketService(db)
        self.httpd = ThreadedHTTPServer(("127.0.0.1", 0), QuantRequestHandler)
        self.httpd.app_context = _Ctx(self.traffic)
        self.httpd.auth = self.auth
        self.httpd.traffic = self.traffic
        self.httpd.polymarket = self.poly
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def _json(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_setup_then_status_requires_login(self):
        code, body = self._json("GET", "/api/auth/status")
        self.assertTrue(body["needs_setup"])
        self.assertFalse(body["authenticated"])
        code, body = self._json("GET", "/api/status")
        self.assertEqual(code, 401)
        code, body = self._json("POST", "/api/auth/setup", {"username": "admin", "password": "secret1"})
        self.assertEqual(code, 200)
        code, body = self._json("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(body["traffic"]["http_requests"], 1)
        code, body = self._json("POST", "/api/auth/setup", {"username": "other", "password": "secret1"})
        self.assertEqual(code, 400)

    def test_order_amount_is_saved_and_secrets_stay_off_the_account(self):
        code, _ = self._json("POST", "/api/auth/setup", {"username": "admin", "password": "secret1"})
        self.assertEqual(code, 200)
        code, body = self._json("GET", "/api/settings")
        self.assertEqual(body["order_amount"], 5)
        code, body = self._json("POST", "/api/settings", {"order_amount": 12.5})
        self.assertEqual(code, 200)
        self.assertEqual(body["order_amount"], 12.5)
        code, body = self._json("GET", "/api/settings")
        self.assertEqual(body["order_amount"], 12.5)
        code, body = self._json("POST", "/api/settings", {"order_amount": 0})
        self.assertEqual(code, 400)
        code, body = self._json("GET", "/api/account")
        self.assertFalse(body["connected"])
        for key in ("signer_key", "api_key", "api_secret", "api_passphrase", "private_key"):
            self.assertNotIn(key, body)

    def test_private_key_shape(self):
        key = "0x" + "11" * 32
        self.assertEqual(_normalize_key(key), key)
        with self.assertRaises(ValueError):
            _normalize_key("0x1234")


if __name__ == "__main__":
    unittest.main()
