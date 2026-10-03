"""
Backtest: replay the exact live strategy (strategy.py + risk.py) on real
historical 15-minute prices from Binance, including fees, and report the
competition metrics.

    python backtest.py                          # last 30 days
    python backtest.py --days 14                # last 14 days
    python backtest.py --set TARGET_ANNUAL_VOL=0.6 --set MAX_POSITIONS=5

Use it before every change to config.py: if a change doesn't improve the
composite score over several periods (e.g. --days 14, 30, 60), don't ship it.

Simplifications vs live: decisions and stops are checked on 15-min closes
(live checks stops every minute), and every trade pays the market-order fee.
"""
import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

import requests

import config as C
import strategy
from risk import RiskManager

CACHE_DIR = "data"
BAR_MS = C.BAR_MINUTES * 60 * 1000


# ------------------------------ data ------------------------------

def download(pair, start_ms, end_ms):
    """All 15-min candles between start and end, as [(open_time_ms, close)]."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{pair.replace('/', '_')}_{start_ms}_{end_ms}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return [tuple(x) for x in json.load(f)]
    symbol = pair.split("/")[0] + "USDT"
    out, cursor = [], start_ms
    while cursor < end_ms:
        rows = None
        for host in ("https://data-api.binance.vision", "https://api.binance.com"):
            try:
                r = requests.get(f"{host}/api/v3/klines", timeout=15, params={
                    "symbol": symbol, "interval": C.BINANCE_INTERVAL,
                    "startTime": cursor, "endTime": end_ms, "limit": 1000})
                if r.status_code == 400:
                    return []
                r.raise_for_status()
                rows = r.json()
                break
            except requests.RequestException as e:
                print(f"  {host} failed for {symbol}: {e}")
        if not rows:
            break
        out += [(int(k[0]), float(k[4])) for k in rows]
        cursor = int(rows[-1][0]) + BAR_MS
        time.sleep(0.2)
    with open(cache, "w") as f:
        json.dump(out, f)
    return out


# ----------------------------- metrics -----------------------------

def metrics(equity_by_day):
    vals = [v for _, v in equity_by_day]
    rets = [b / a - 1 for a, b in zip(vals[:-1], vals[1:])]
    total = vals[-1] / vals[0] - 1
    peak, max_dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        max_dd = max(max_dd, 1 - v / peak)
    if len(rets) < 2:
        return {"return": total, "max_dd": max_dd, "sharpe": 0, "sortino": 0, "calmar": 0, "score": 0}
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))
    sharpe = mean / std * math.sqrt(365) if std > 0 else 0.0
    sortino = mean / downside * math.sqrt(365) if downside > 0 else (10.0 if mean > 0 else 0.0)
    annual = (1 + total) ** (365 / len(rets)) - 1
    calmar = annual / max_dd if max_dd > 0 else (10.0 if annual > 0 else 0.0)
    score = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar
    return {"return": total, "max_dd": max_dd, "sharpe": sharpe, "sortino": sortino, "calmar": calmar, "score": score}


def daily_points(times, values):
    by_day = {}
    for t, v in zip(times, values):
        by_day[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = v
    return sorted(by_day.items())


# ---------------------------- simulation ----------------------------

def run(series, days, start_cash=100_000.0, verbose=True):
    """series: dict pair -> list of (open_ms, close), all aligned to the same timeline."""
    timeline = [t for t, _ in series[C.REGIME_PAIR]]
    closes = {p: [c for _, c in s] for p, s in series.items()}
    start = len(timeline) - days * C.BARS_PER_DAY
    if start < C.MIN_WARMUP_BARS:
        raise SystemExit("Not enough history for warm-up; reduce --days.")

    risk = RiskManager()
    cash, qty = start_cash, {p: 0.0 for p in closes}
    fees, n_trades, eq_t, eq_v = 0.0, 0, [], []
    trade_days = set()

    def trade(pair, usd_signed, price, t):
        nonlocal cash, fees, n_trades
        q = usd_signed / price
        if usd_signed < 0:
            q = max(q, -qty[pair])
        fee = abs(q * price) * C.BACKTEST_FEE
        cash -= q * price + fee
        qty[pair] += q
        fees += fee
        n_trades += 1
        trade_days.add(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"))

    for i in range(start, len(timeline)):
        now = timeline[i] / 1000 + C.BAR_MINUTES * 60          # bar close time
        prices = {p: closes[p][i] for p in closes if closes[p][i]}
        equity = cash + sum(qty[p] * prices.get(p, 0) for p in qty)
        values = {p: qty[p] * prices.get(p, 0) for p in qty}
        held = {p for p, v in values.items() if v >= C.MIN_TRADE_USD}

        bars = {p: [c for c in closes[p][max(0, i - C.MAX_BARS + 1):i + 1] if c] for p in closes}
        targets_raw, diag, _ = strategy.compute_targets(bars, held)
        vols = {p: d["vol"] for p, d in diag.items() if not p.startswith("_") and d.get("vol")}

        # risk events on this bar
        for p in set(held) | set(risk.s["trail_peaks"]):
            if p in prices:
                risk.update_trailing(p, prices[p], p in held)
        btc_recent = [(timeline[j] / 1000 + C.BAR_MINUTES * 60, closes[C.REGIME_PAIR][j])
                      for j in range(max(0, i - 4), i + 1)]
        if risk.check_crash(btc_recent, now):
            for p in held:
                trade(p, -values[p], prices[p], now)
            held = set()
        else:
            for p in risk.check_stops(prices, vols, held, now):
                trade(p, -values[p], prices[p], now)
                held.discard(p)

        equity = cash + sum(qty[p] * prices.get(p, 0) for p in qty)
        mult, _, _ = risk.exposure_multiplier(equity, now)
        targets = risk.apply(targets_raw, mult, now)

        for p in closes:
            if p not in prices:
                continue
            cur = qty[p] * prices[p]
            tgt = targets.get(p, 0.0) * equity
            diff = tgt - cur
            if tgt == 0 and cur > 10:
                trade(p, -cur, prices[p], now)
            elif abs(diff) >= C.MIN_TRADE_USD and abs(diff) / equity >= C.REBALANCE_BAND:
                trade(p, diff, prices[p], now)

        eq_t.append(now)
        eq_v.append(cash + sum(qty[p] * prices.get(p, 0) for p in qty))

    m = metrics(daily_points(eq_t, eq_v))
    btc = closes[C.REGIME_PAIR][start:]
    bh = metrics(daily_points(eq_t, [start_cash * c / btc[0] for c in btc]))
    if verbose:
        print(f"\n=== Backtest: last {days} days, {len(closes)} coins ===")
        print(f"{'':16}{'Bot':>12}{'BTC hold':>12}")
        for k, label in [("return", "Return"), ("max_dd", "Max drawdown")]:
            print(f"{label:16}{m[k]*100:>11.2f}%{bh[k]*100:>11.2f}%")
        for k, label in [("sortino", "Sortino"), ("sharpe", "Sharpe"), ("calmar", "Calmar"), ("score", "Composite")]:
            print(f"{label:16}{m[k]:>12.2f}{bh[k]:>12.2f}")
        print(f"Trades: {n_trades} | fees ${fees:,.0f} | active trading days {len(trade_days)}/{days}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pairs", nargs="*", default=C.BACKTEST_PAIRS)
    ap.add_argument("--set", action="append", default=[], help="override a config value, e.g. MAX_POSITIONS=5")
    args = ap.parse_args()

    for item in args.set:
        key, val = item.split("=", 1)
        if not hasattr(C, key):
            raise SystemExit(f"Unknown setting {key}")
        setattr(C, key, type(getattr(C, key))(eval(val)))
        print(f"Override: {key} = {getattr(C, key)}")

    end_ms = (int(time.time() * 1000) // BAR_MS) * BAR_MS
    start_ms = end_ms - (args.days * C.BARS_PER_DAY + C.MAX_BARS) * BAR_MS
    print(f"Downloading {len(args.pairs)} coins from Binance (cached in {CACHE_DIR}/)...")
    raw = {}
    for p in args.pairs:
        rows = download(p, start_ms, end_ms)
        if rows:
            raw[p] = dict(rows)
        else:
            print(f"  skipping {p}: no data")
    if C.REGIME_PAIR not in raw:
        raise SystemExit("BTC data is required.")
    timeline = sorted(raw[C.REGIME_PAIR])
    series = {}
    for p, d in raw.items():
        last, s = None, []
        for t in timeline:
            last = d.get(t, last)                    # forward-fill gaps
            s.append((t, last))
        series[p] = s
    run(series, args.days)


if __name__ == "__main__":
    main()
