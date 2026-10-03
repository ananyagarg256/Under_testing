"""
Roostoo trading bot - main loop.

    python bot.py          (stop with Ctrl+C)

Every minute:
  * read prices (Roostoo ticker) and the balance
  * update trailing stops; sell any coin whose stop is hit
  * crash guard: if BTC drops sharply, sell everything and pause
Every 15 minutes (new bar):
  * recompute trend scores, regime and risk multipliers
  * rebalance towards the new target weights
Once a day: if nothing has traded by DAILY_FORCE_HOUR_UTC, rebalance exactly
(keeps the "trades every day" requirement without fake trades).

Everything is logged to logs/: bot.log, trades.csv, equity.csv, signals.csv,
state.json (risk state, survives restarts) and bars.json (price history).
"""
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

os.makedirs("logs", exist_ok=True)

from dotenv import load_dotenv  # noqa: E402

import config as C  # noqa: E402
from executor import Executor  # noqa: E402
from market_data import MarketData  # noqa: E402
from risk import RiskManager  # noqa: E402
from roostoo_client import RoostooClient  # noqa: E402
from strategy import asset_volatility, compute_targets  # noqa: E402

log = logging.getLogger("bot")
STATE_FILE = os.path.join("logs", "state.json")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def append_csv(name, header, rows):
    path = os.path.join("logs", name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


class Bot:
    def __init__(self, client, sleep=time.sleep, clock=time.time):
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.state = self._load_state()
        self.state.setdefault("risk", {})
        self.state.setdefault("universe", [])
        self.state.setdefault("trades_by_day", {})
        self.state.setdefault("force_day", "")
        self.risk = RiskManager(self.state["risk"])
        self.targets = {}
        self.vols = {}
        self.universe = []

    # ---------- state ----------

    @staticmethod
    def _load_state():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, STATE_FILE)

    def record_trades(self, n, now):
        if n:
            day = utc_day(now)
            self.state["trades_by_day"][day] = self.state["trades_by_day"].get(day, 0) + n

    # ---------- setup ----------

    def setup(self):
        info = self.client.exchange_info()
        if not info or "TradePairs" not in info:
            raise SystemExit("Could not load exchange info from Roostoo.")
        self.rules = {p: r for p, r in info["TradePairs"].items()
                      if r.get("CanTrade") and r.get("Unit", "USD") == "USD"}
        log.info("Roostoo lists %d tradable USD pairs", len(self.rules))

        self.md = MarketData(self.client, self.rules)
        self.ex = Executor(self.client, self.rules, self.md, sleep=self.sleep, clock=self.clock)
        self.client.cancel_all_orders()            # clean start: no leftover pending orders

        now = self.clock()
        if not self.md.refresh_ticker(now):
            raise SystemExit("Could not read prices from Roostoo.")
        self.universe = self.md.build_universe(now, preferred=self.state["universe"])
        if not self.universe:
            raise SystemExit("No coins with enough price history.")
        self.state["universe"] = self.universe
        log.info("Trading universe: %s", ", ".join(self.universe))
        self.on_new_bar(now, reason="startup")
        self.save_state()

    # ---------- every minute ----------

    def step(self):
        now = self.clock()
        if not self.md.refresh_ticker(now):
            return
        snap = self.ex.snapshot()
        if snap is None:
            return
        held = {p for p, v in snap["positions"].items() if v["value"] >= C.MIN_TRADE_USD}
        prices = {p: self.md.price(p) for p in self.md.tick}
        for p in set(held) | set(self.risk.s["trail_peaks"]):
            if prices.get(p):
                self.risk.update_trailing(p, prices[p], p in held)

        if self.risk.check_crash(list(self.md.btc_minutes), now):
            log.warning("CRASH GUARD: BTC fell %.0f%%+ within %d min -> selling everything",
                        C.CRASH_DROP * 100, C.CRASH_WINDOW_MINUTES)
            self.targets = {}
            self.record_trades(self.ex.rebalance({}, urgent=True, reason="crash guard"), now)
        else:
            hits = self.risk.check_stops(prices, self.vols, held, now)
            if hits:
                log.warning("TRAILING STOP hit: %s", ", ".join(hits))
                for p in hits:
                    self.targets.pop(p, None)
                self.record_trades(self.ex.rebalance(self.targets, urgent=True, only=hits,
                                                     reason="trailing stop"), now)

        if self.md.close_bar_if_new(now):
            self.on_new_bar(now)
        elif self._needs_daily_force(now):
            self.state["force_day"] = utc_day(now)
            self.on_new_bar(now, force=True, reason="daily rebalance")
        self.save_state()

    def _needs_daily_force(self, now):
        day = utc_day(now)
        return (datetime.fromtimestamp(now, timezone.utc).hour >= C.DAILY_FORCE_HOUR_UTC
                and self.state["trades_by_day"].get(day, 0) == 0
                and self.state["force_day"] != day)

    # ---------- every 15 minutes ----------

    def on_new_bar(self, now, force=False, reason="new bar"):
        snap = self.ex.snapshot()
        if snap is None:
            return
        held = {p for p, v in snap["positions"].items() if v["value"] >= C.MIN_TRADE_USD}
        bars = {p: self.md.bars[p] for p in self.universe if p in self.md.bars}
        raw, diag, regime_mult = compute_targets(bars, held)
        self.vols = {p: d["vol"] for p, d in diag.items() if not p.startswith("_") and d["vol"]}
        for p in held - set(self.vols):            # coins held outside the universe
            if p in self.md.bars:
                self.vols[p] = asset_volatility(self.md.bars[p])

        keep = {p for p in held if p in diag and diag[p]["score"] is None}
        risk_mult, drawdown, day_ret = self.risk.exposure_multiplier(snap["equity"], now)
        self.targets = self.risk.apply(raw, risk_mult, now)

        exposure = 1 - snap["usd_total"] / snap["equity"]
        regime = diag["_regime"]
        log.info("%s | equity $%.2f | invested %.0f%% | dd %.2f%% | day %+.2f%% | regime %s x%.2f | risk x%.2f | targets %s",
                 reason, snap["equity"], exposure * 100, drawdown * 100, day_ret * 100,
                 "n/a" if regime["score"] is None else f"{regime['score']:+.2f}", regime_mult, risk_mult,
                 {p: round(w, 3) for p, w in self.targets.items() if w > 0} or "all cash")

        ts = datetime.fromtimestamp(now, timezone.utc).isoformat()
        append_csv("equity.csv",
                   ["time", "equity", "invested_pct", "drawdown", "day_return", "regime_score",
                    "regime_mult", "risk_mult"],
                   [[ts, round(snap["equity"], 2), round(exposure, 4), round(drawdown, 4), round(day_ret, 4),
                     regime["score"] if regime["score"] is None else round(regime["score"], 3),
                     round(regime_mult, 3), round(risk_mult, 3)]])
        append_csv("signals.csv", ["time", "pair", "score", "daily_vol", "target_weight"],
                   [[ts, p, None if d["score"] is None else round(d["score"], 3),
                     None if not d["vol"] else round(d["vol"] * (C.BARS_PER_DAY ** 0.5), 4),
                     round(self.targets.get(p, 0.0), 4)]
                    for p, d in diag.items() if not p.startswith("_")])

        n = self.ex.rebalance(self.targets, keep=keep, band=0.0 if force else C.REBALANCE_BAND, reason=reason)
        self.record_trades(n, now)

    # ---------- main loop ----------

    def run(self):
        self.setup()
        log.info("Bot running. Polling every %ds.", C.POLL_SECONDS)
        while True:
            started = self.clock()
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Unexpected error in step, continuing")
            self.sleep(max(1.0, C.POLL_SECONDS - (self.clock() - started)))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-6s %(message)s",
        handlers=[logging.FileHandler(os.path.join("logs", "bot.log")), logging.StreamHandler(sys.stdout)],
    )
    load_dotenv()
    key, secret = os.getenv("ROOSTOO_API_KEY"), os.getenv("ROOSTOO_API_SECRET")
    if not key or not secret:
        raise SystemExit("Keys not found. Create a .env file with ROOSTOO_API_KEY and ROOSTOO_API_SECRET.")
    bot = Bot(RoostooClient(key, secret))
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.save_state()
        log.info("Stopped by user. State saved.")


if __name__ == "__main__":
    main()
