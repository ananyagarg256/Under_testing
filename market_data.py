"""
Market data: where the bot's prices come from.

Live prices: Roostoo's GET /v3/ticker returns, for every coin, the last
traded price, best bid, best ask and 24h volume. Roostoo is a mock exchange:
the money is fake but prices follow the real crypto market. The bot calls
this once per minute.

History: Roostoo has no price-history endpoint, so the bot builds its own
15-minute bars from the ticker. To start trading immediately (instead of
waiting days for history), it downloads recent 15-minute candles from
Binance's free public market-data API on startup. Bars are saved to
logs/bars.json so a restart doesn't lose them.
"""
import json
import logging
import math
import os
import time
from collections import deque

import requests

import config as C

log = logging.getLogger("data")
BAR_SECONDS = C.BAR_MINUTES * 60
BARS_FILE = os.path.join("logs", "bars.json")


def fetch_binance_closes(pair, limit=C.MAX_BARS, end_time_ms=None):
    """Completed 15-min closing prices for a pair from Binance public data ([] on failure)."""
    symbol = pair.split("/")[0] + "USDT"
    params = {"symbol": symbol, "interval": C.BINANCE_INTERVAL, "limit": min(limit, 1000)}
    if end_time_ms:
        params["endTime"] = int(end_time_ms)
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = requests.get(f"{host}/api/v3/klines", params=params, timeout=10)
            if r.status_code == 400:          # symbol doesn't exist on Binance
                return []
            r.raise_for_status()
            now_ms = time.time() * 1000
            return [float(k[4]) for k in r.json() if k[6] < now_ms]
        except (requests.RequestException, ValueError, IndexError, TypeError) as e:
            log.warning("Binance %s failed for %s: %s", host, symbol, e)
    return []


class MarketData:
    def __init__(self, client, rules):
        self.client = client
        self.rules = rules                      # pair -> exchange rules (tradable USD pairs)
        self.tick = {}                          # pair -> {"last", "bid", "ask", "volume"}
        self.bars = {}                          # pair -> list of 15-min closes
        self.btc_minutes = deque(maxlen=C.CRASH_WINDOW_MINUTES * 2)
        self.bar_id = None

    # ---------- live ticker ----------

    def refresh_ticker(self, now):
        t = self.client.ticker()
        if not t or not t.get("Success"):
            log.warning("Ticker failed: %s", (t or {}).get("ErrMsg", "no response"))
            return False
        for pair, d in (t.get("Data") or {}).items():
            if pair not in self.rules or not d.get("LastPrice"):
                continue
            last = float(d["LastPrice"])
            self.tick[pair] = {
                "last": last,
                "bid": float(d.get("MaxBid") or last),
                "ask": float(d.get("MinAsk") or last),
                "volume": float(d.get("UnitTradeValue") or 0),
            }
        if C.REGIME_PAIR in self.tick:
            self.btc_minutes.append((now, self.tick[C.REGIME_PAIR]["last"]))
        return True

    def price(self, pair):
        return self.tick.get(pair, {}).get("last")

    # ---------- history / universe ----------

    def _load_saved(self, now):
        try:
            with open(BARS_FILE) as f:
                saved = json.load(f)
            if saved.get("bar_id", 0) >= int(now // BAR_SECONDS) - 2:
                return saved.get("bars", {})
            log.info("Saved bars are stale, reloading history from Binance")
        except (OSError, ValueError):
            pass
        return {}

    def _warmup(self, pair):
        closes = fetch_binance_closes(pair)
        live = self.price(pair)
        if closes and live:
            ratio = live / closes[-1]
            if abs(ratio - 1) > 0.02:           # Roostoo and Binance prices differ: align them
                log.warning("%s: Roostoo/Binance price ratio %.4f, rescaling history", pair, ratio)
                closes = [c * ratio for c in closes]
        return closes

    def build_universe(self, now, preferred=()):
        """Pick the most liquid tradable coins that have enough price history."""
        saved = self._load_saved(now)
        by_volume = sorted(self.tick, key=lambda p: self.tick[p]["volume"], reverse=True)
        ordered = []
        for p in list(C.CORE_PAIRS) + list(preferred) + by_volume:
            coin = p.split("/")[0]
            if p in self.tick and p not in ordered and coin not in C.EXCLUDE_COINS:
                ordered.append(p)

        universe = []
        for pair in ordered:
            if len(universe) >= C.UNIVERSE_SIZE:
                break
            closes = saved.get(pair) or []
            if len(closes) < C.MIN_WARMUP_BARS:
                closes = self._warmup(pair)
            if len(closes) >= C.MIN_WARMUP_BARS:
                self.bars[pair] = closes[-C.MAX_BARS:]
                universe.append(pair)
                log.info("Universe + %-10s %4d bars", pair, len(closes))
            else:
                log.info("Universe - %-10s (only %d bars of history)", pair, len(closes))
        self.bar_id = int(now // BAR_SECONDS)
        self.save()
        return universe

    def close_bar_if_new(self, now):
        """At each 15-min boundary, record the latest price as the closed bar. Returns True if a bar closed."""
        current = int(now // BAR_SECONDS)
        if self.bar_id is None:
            self.bar_id = current
            return False
        if current == self.bar_id:
            return False
        for pair, closes in self.bars.items():
            price = self.price(pair)
            if price and math.isfinite(price):
                closes.append(price)
                del closes[:-C.MAX_BARS]
        self.bar_id = current
        self.save()
        return True

    def save(self):
        tmp = BARS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"bar_id": self.bar_id, "bars": self.bars}, f)
        os.replace(tmp, BARS_FILE)
