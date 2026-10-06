"""
Compare several strategy settings on the same real data, over several
periods, and print one table. Use it to choose config.py values.

    python sweep.py

A good setting wins on the composite score across ALL periods, not just one
(winning on only one period usually means it got lucky).
"""
import time

import backtest as BT
import config as C

# Each variant = settings that differ from config.py
  VARIANTS = {
      "A current": {},
      "B more risk": {"TARGET_ANNUAL_VOL": 0.7},
      "C chop filter": {"MIN_EFFICIENCY": 0.15},
      "D more risk + chop": {"TARGET_ANNUAL_VOL": 0.7, "MIN_EFFICIENCY": 0.15},
      "E D + wider stops": {"TARGET_ANNUAL_VOL": 0.7, "MIN_EFFICIENCY": 0.15,
                            "TRAIL_STOP_DAILY_SIGMAS": 3.0, "TRAIL_STOP_MIN": 0.04},
  }
PERIODS = [14, 30, 60]


def load_series(days):
    end_ms = (int(time.time() * 1000) // BT.BAR_MS) * BT.BAR_MS
    start_ms = end_ms - (days * C.BARS_PER_DAY + C.MAX_BARS) * BT.BAR_MS
    raw = {}
    for p in C.BACKTEST_PAIRS:
        rows = BT.download(p, start_ms, end_ms)
        if rows:
            raw[p] = dict(rows)
    timeline = sorted(raw[C.REGIME_PAIR])
    series = {}
    for p, d in raw.items():
        last, s = None, []
        for t in timeline:
            last = d.get(t, last)
            s.append((t, last))
        series[p] = s
    return series


def run_variant(series, days, overrides):
    saved = {k: getattr(C, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(C, k, v)
        return BT.run(series, days, verbose=False)
    finally:
        for k, v in saved.items():
            setattr(C, k, v)


def main():
    print(f"Downloading {max(PERIODS)} days of data (cached after the first run)...")
    series = load_series(max(PERIODS))
    print(f"\n{'Variant':20}" + "".join(f"{f'{d}d return':>11}{f'{d}d maxDD':>10}{f'{d}d score':>10}" for d in PERIODS)
          + f"{'avg score':>11}")
    print("-" * (20 + 31 * len(PERIODS) + 11))
    for name, overrides in VARIANTS.items():
        row, scores = f"{name:20}", []
        for d in PERIODS:
            m = run_variant(series, d, overrides)
            scores.append(m["score"])
            row += f"{m['return']*100:>10.2f}%{m['max_dd']*100:>9.2f}%{m['score']:>10.2f}"
        print(row + f"{sum(scores)/len(scores):>11.2f}", flush=True)
    print("\nPick the variant with the best avg score that also has positive returns in every period.")


if __name__ == "__main__":
    main()
