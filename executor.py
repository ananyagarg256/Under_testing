"""
Order execution: turns target weights into actual orders.

Normal rebalances use a two-step "maker first" approach to cut fees:
  1. Place LIMIT orders at the best bid (buys) / best ask (sells).
     If they fill, the fee is 0.05% instead of 0.1%.
  2. After LIMIT_WAIT_SECONDS, cancel whatever didn't fill, re-read the
     balance, and finish the remaining gap with MARKET orders.
Because step 2 recomputes the gap from the real balance, partial fills
are handled automatically.

Urgent exits (stop-loss, crash guard) go straight to MARKET orders.
Sells always run before buys so the USD is available.
"""
import csv
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timezone

import config as C

log = logging.getLogger("exec")
TRADES_CSV = os.path.join("logs", "trades.csv")


def floor_to(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def ceil_to(x, decimals):
    f = 10 ** decimals
    return math.ceil(x * f - 1e-9) / f


class Executor:
    def __init__(self, client, rules, market, sleep=time.sleep, clock=time.time):
        self.client = client
        self.rules = rules
        self.md = market
        self.sleep = sleep
        self.clock = clock
        self.order_times = deque()

    # ---------- portfolio ----------

    def snapshot(self):
        b = self.client.balance()
        if not b or not b.get("Success"):
            log.warning("Balance failed: %s", (b or {}).get("ErrMsg", "no response"))
            return None
        wallet = b.get("Wallet") or b.get("SpotWallet") or {}
        usd = wallet.get("USD", {})
        usd_free = float(usd.get("Free", 0))
        usd_total = usd_free + float(usd.get("Lock", 0))
        positions = {}
        for coin, v in wallet.items():
            pair = f"{coin}/USD"
            price = self.md.price(pair)
            if coin == "USD" or pair not in self.rules or not price:
                continue
            free = float(v.get("Free", 0))
            qty = free + float(v.get("Lock", 0))
            if qty > 0:
                positions[pair] = {"qty": qty, "free": free, "value": qty * price}
        equity = usd_total + sum(p["value"] for p in positions.values())
        return {"usd_free": usd_free, "usd_total": usd_total, "positions": positions, "equity": equity}

    # ---------- planning ----------

    def plan(self, snap, targets, keep=(), only=None, band=C.REBALANCE_BAND):
        equity = snap["equity"]
        pairs = set(targets) | set(snap["positions"])
        if only is not None:
            pairs &= set(only)
        orders = []
        for pair in sorted(pairs):
            if pair in keep or not self.md.price(pair):
                continue
            cur = snap["positions"].get(pair, {}).get("value", 0.0)
            tgt = targets.get(pair, 0.0) * equity
            diff = tgt - cur
            full_exit = tgt == 0 and cur > 0
            if full_exit:
                if cur < 10:                       # ignore dust
                    continue
            elif abs(diff) < C.MIN_TRADE_USD or abs(diff) / equity < band:
                continue
            orders.append({"pair": pair, "side": "BUY" if diff > 0 else "SELL",
                           "usd": abs(diff), "full_exit": full_exit})
        return orders

    # ---------- execution ----------

    def rebalance(self, targets, keep=(), urgent=False, only=None, band=C.REBALANCE_BAND, reason=""):
        """Returns the number of successful orders."""
        snap = self.snapshot()
        if snap is None or snap["equity"] <= 0:
            return 0
        orders = self.plan(snap, targets, keep, only, band)
        if not orders:
            return 0
        log.info("Rebalance (%s): %s", reason or ("urgent" if urgent else "normal"),
                 ", ".join(f"{o['side']} {o['pair']} ${o['usd']:.0f}" for o in orders))

        done = 0
        if C.USE_LIMIT_ORDERS and not urgent:
            before = {p: v["qty"] for p, v in snap["positions"].items()}
            if self._execute(orders, snap, "LIMIT", urgent):
                self.sleep(C.LIMIT_WAIT_SECONDS)
                self.client.cancel_all_orders()      # release anything that didn't fill
                snap = self.snapshot()
                if snap is None:
                    return done
                after = {p: v["qty"] for p, v in snap["positions"].items()}
                done += sum(1 for p in set(before) | set(after)
                            if abs(after.get(p, 0) - before.get(p, 0)) > 1e-12)   # limit fills
                orders = self.plan(snap, targets, keep, only, band)
        if orders:
            done += self._execute(orders, snap, "MARKET", urgent)
        return done

    def _execute(self, orders, snap, order_type, urgent):
        ok = 0
        for o in [o for o in orders if o["side"] == "SELL"]:
            pos = snap["positions"].get(o["pair"], {})
            price = self._order_price(o["pair"], "SELL", order_type)
            free = pos.get("free", 0.0)
            qty = free if o["full_exit"] else min(o["usd"] / price, free)
            ok += self._send(o["pair"], "SELL", qty, order_type, price, urgent, max_qty=free)

        buys = sorted([o for o in orders if o["side"] == "BUY"], key=lambda o: -o["usd"])
        if buys:
            if order_type == "MARKET" and ok:       # sells just freed USD: refresh
                snap = self.snapshot() or snap
            usd_free = snap["usd_free"]
            for o in buys:
                price = self._order_price(o["pair"], "BUY", order_type)
                usd = min(o["usd"], usd_free * 0.995)
                if usd < C.MIN_TRADE_USD:
                    continue
                if self._send(o["pair"], "BUY", usd / price, order_type, price, urgent):
                    ok += 1
                    usd_free -= usd
        return ok

    def _order_price(self, pair, side, order_type):
        t = self.md.tick[pair]
        if order_type == "MARKET":
            return t["ask"] if side == "BUY" else t["bid"]
        decimals = int(self.rules[pair].get("PricePrecision", 2))
        return floor_to(t["bid"], decimals) if side == "BUY" else ceil_to(t["ask"], decimals)

    def _rate_ok(self, urgent):
        now = self.clock()
        while self.order_times and self.order_times[0] < now - 3600:
            self.order_times.popleft()
        if len(self.order_times) >= C.MAX_ORDERS_PER_HOUR and not urgent:
            log.warning("Order rate cap reached, skipping non-urgent order")
            return False
        self.order_times.append(now)
        return True

    def _send(self, pair, side, qty, order_type, price, urgent, max_qty=None):
        rules = self.rules[pair]
        qty_dec = int(rules.get("AmountPrecision", 6))
        price_dec = int(rules.get("PricePrecision", 2))
        qty = floor_to(qty, qty_dec)
        if max_qty is not None:                    # never sell more than we have (float rounding)
            step = 10 ** -qty_dec
            while qty > max_qty and qty > 0:
                qty = round(qty - step, qty_dec)
        if qty <= 0 or qty * price <= float(rules.get("MiniOrder", 1.0)) or not self._rate_ok(urgent):
            return 0
        qty_str = f"{qty:.{qty_dec}f}"
        price_str = f"{price:.{price_dec}f}" if order_type == "LIMIT" else None
        res = self.client.place_order(pair, side, qty_str, order_type, price_str)
        success = bool(res and res.get("Success"))
        d = (res or {}).get("OrderDetail", {})
        err = "" if success else (res or {}).get("ErrMsg", "no response")
        log.info("ORDER %s %s %s %s%s -> %s %s", order_type, side, qty_str, pair,
                 f" @ {price_str}" if price_str else "", d.get("Status", "FAILED"), err)
        self._log_trade([datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), pair, side, order_type, qty_str,
                         d.get("FilledAverPrice") or price, d.get("Status", "FAILED"), d.get("Role", ""),
                         d.get("CommissionChargeValue", ""), d.get("OrderID", ""), err])
        return int(success)

    @staticmethod
    def _log_trade(row):
        new = not os.path.exists(TRADES_CSV)
        with open(TRADES_CSV, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "pair", "side", "type", "quantity", "price", "status",
                            "role", "fee", "order_id", "error"])
            w.writerow(row)
