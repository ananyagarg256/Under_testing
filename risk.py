"""
Risk management. Protects against losses in four ways:

  1. Trailing stop per coin: tracks the highest price since we bought.
     If the price falls a volatility-scaled distance below that peak,
     the coin is sold and blocked for STOP_COOLDOWN_BARS.
  2. Crash guard: if BTC drops CRASH_DROP from its high within an hour,
     everything is sold and trading pauses for CRASH_COOLDOWN_BARS.
  3. Drawdown control: the further equity falls below its all-time peak,
     the smaller every position becomes (down to DD_MIN_MULT).
  4. Daily loss limit: a bad day halves exposure until the next UTC day.

The clock is passed in (now = unix seconds) so the backtest can reuse it.
"""
import math
from datetime import datetime, timezone

import config as C

BAR_SECONDS = C.BAR_MINUTES * 60


class RiskManager:
    def __init__(self, state=None):
        self.s = state if state is not None else {}
        self.s.setdefault("peak_equity", 0.0)
        self.s.setdefault("day", "")
        self.s.setdefault("day_start_equity", 0.0)
        self.s.setdefault("trail_peaks", {})
        self.s.setdefault("cooldown_until", {})
        self.s.setdefault("global_cooldown_until", 0.0)

    # ---------- portfolio-level ----------

    def exposure_multiplier(self, equity, now):
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.s["day"] != day:
            self.s["day"] = day
            self.s["day_start_equity"] = equity
        self.s["peak_equity"] = max(self.s["peak_equity"], equity)

        drawdown = 1 - equity / self.s["peak_equity"]
        if drawdown <= C.DD_SOFT:
            mult = 1.0
        elif drawdown >= C.DD_HARD:
            mult = C.DD_MIN_MULT
        else:
            frac = (drawdown - C.DD_SOFT) / (C.DD_HARD - C.DD_SOFT)
            mult = 1.0 - frac * (1.0 - C.DD_MIN_MULT)

        day_return = equity / self.s["day_start_equity"] - 1 if self.s["day_start_equity"] else 0.0
        if day_return < -C.DAILY_LOSS_LIMIT:
            mult *= C.DAILY_LOSS_MULT
        return mult, drawdown, day_return

    # ---------- per-coin trailing stops ----------

    def update_trailing(self, pair, price, held):
        peaks = self.s["trail_peaks"]
        if held:
            peaks[pair] = max(peaks.get(pair, price), price)
        else:
            peaks.pop(pair, None)

    def stop_distance(self, bar_vol):
        daily_vol = bar_vol * math.sqrt(C.BARS_PER_DAY)
        return min(C.TRAIL_STOP_MAX, max(C.TRAIL_STOP_MIN, C.TRAIL_STOP_DAILY_SIGMAS * daily_vol))

    def check_stops(self, prices, vols, held_pairs, now):
        """Returns pairs whose trailing stop was hit (and puts them on cooldown)."""
        hits = []
        for pair in held_pairs:
            peak, price, vol = self.s["trail_peaks"].get(pair), prices.get(pair), vols.get(pair)
            if not peak or not price or not vol:
                continue
            if price < peak * (1 - self.stop_distance(vol)):
                hits.append(pair)
                self.s["cooldown_until"][pair] = now + C.STOP_COOLDOWN_BARS * BAR_SECONDS
                self.s["trail_peaks"].pop(pair, None)
        return hits

    # ---------- market crash guard ----------

    def check_crash(self, recent_btc, now):
        """recent_btc: list of (timestamp, price). True if BTC crashed within the window."""
        window = [p for t, p in recent_btc if t >= now - C.CRASH_WINDOW_MINUTES * 60]
        if len(window) < 2 or now < self.s["global_cooldown_until"]:
            return False
        if window[-1] / max(window) - 1 < -C.CRASH_DROP:
            self.s["global_cooldown_until"] = now + C.CRASH_COOLDOWN_BARS * BAR_SECONDS
            return True
        return False

    # ---------- applying blocks ----------

    def blocked(self, pair, now):
        return now < self.s["global_cooldown_until"] or now < self.s["cooldown_until"].get(pair, 0)

    def apply(self, targets, mult, now):
        return {p: (0.0 if self.blocked(p, now) else w * mult) for p, w in targets.items()}
