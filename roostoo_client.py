"""
Roostoo REST API client (https://github.com/roostoo/Roostoo-API-Documents).

Signed requests: parameters + timestamp are sorted, joined as k=v&k=v and
signed with HMAC-SHA256 using the secret key. The API key and signature go
in the RST-API-KEY and MSG-SIGNATURE headers. The secret never leaves your
machine.

Every method returns the parsed JSON, or None if the request failed.
Roostoo answers failed trades with HTTP 200 + "Success": false, so callers
must check the Success flag.
"""
import hashlib
import hmac
import logging
import time

import requests

log = logging.getLogger("roostoo")


class RoostooClient:
    def __init__(self, api_key, secret_key, base_url="https://mock-api.roostoo.com", timeout=10):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    @staticmethod
    def _timestamp():
        return str(int(time.time() * 1000))

    def _sign(self, params):
        params = dict(params)
        params["timestamp"] = self._timestamp()
        total_params = "&".join(f"{k}={params[k]}" for k in sorted(params))
        signature = hmac.new(self.secret_key.encode(), total_params.encode(), hashlib.sha256).hexdigest()
        return params, total_params, {"RST-API-KEY": self.api_key, "MSG-SIGNATURE": signature}

    def _request(self, method, path, params=None, signed=False, retries=3):
        url = self.base_url + path
        params = params or {}
        for attempt in range(1, retries + 1):
            try:
                if signed:
                    p, total_params, headers = self._sign(params)
                    if method == "GET":
                        r = self.session.get(url, params=p, headers=headers, timeout=self.timeout)
                    else:
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                        r = self.session.post(url, data=total_params, headers=headers, timeout=self.timeout)
                else:
                    r = self.session.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("%s %s failed (attempt %d/%d): %s", method, path, attempt, retries, e)
                if attempt < retries:
                    time.sleep(2 * attempt)
        return None

    # ----- public -----
    def server_time(self):
        return self._request("GET", "/v3/serverTime")

    def exchange_info(self):
        return self._request("GET", "/v3/exchangeInfo")

    def ticker(self, pair=None):
        params = {"timestamp": self._timestamp()}
        if pair:
            params["pair"] = pair
        return self._request("GET", "/v3/ticker", params=params)

    # ----- signed -----
    def balance(self):
        return self._request("GET", "/v3/balance", signed=True)

    def place_order(self, pair, side, quantity, order_type="MARKET", price=None):
        """Orders are sent once (retries=1) so a timeout can never cause a double order."""
        params = {"pair": pair, "side": side, "type": order_type, "quantity": quantity}
        if order_type == "LIMIT":
            params["price"] = price
        return self._request("POST", "/v3/place_order", params=params, signed=True, retries=1)

    def cancel_all_orders(self):
        return self._request("POST", "/v3/cancel_order", signed=True)

    def query_orders(self, pair=None, pending_only=None, limit=None):
        params = {}
        if pair:
            params["pair"] = pair
            if pending_only is not None:
                params["pending_only"] = "TRUE" if pending_only else "FALSE"
        if limit:
            params["limit"] = str(limit)
        return self._request("POST", "/v3/query_order", params=params, signed=True)
