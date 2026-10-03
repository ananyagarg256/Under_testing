"""
Signal and position-sizing logic. Pure functions: no API calls, no clock.
The live bot and the backtest both use this file, so what you backtest is
exactly what trades.

How a coin's target weight is decided:
  1. Trend score (-1..1): average of four "votes" -
       three EMA crossovers at different speeds (2h/8h, 4h/16h, 8h/32h)
       and the 24h return. Each vote is measured relative to the coin's own
       volatility, so a 1% move counts more for BTC than for a meme coin.
  2. Only coins with a score above ENTRY_SCORE qualify (held coins stay
     until the score falls below EXIT_SCORE). The best MAX_POSITIONS win.
  3. Size = risk budget x score / volatility. Stronger trend -> bigger;
     wilder coin -> smaller. Capped per coin and in total.
  4. Everything is scaled by the BTC market regime: full size in a bull
     market, about a third in a bear market.
"""
import math

import config as C


def ema(values, span):
    alpha = 2.0 / (span + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def bar_volatility(closes, lookback):
    """Standard deviation of 15-min log returns over `lookback` bars."""
    window = closes[-(lookback + 1):]
    rets = [math.log(b / a) for a, b in zip(window[:-1], window[1:]) if a > 0 and b > 0]
    if len(rets) < max(10, lookback // 2):
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def asset_volatility(closes):
    """Per-bar volatility; uses the larger of short and long estimates (reacts fast to spikes)."""
    vols = [v for v in (bar_volatility(closes, C.VOL_SHORT_BARS),
                        bar_volatility(closes, C.VOL_LONG_BARS)) if v]
    return max(vols) if vols else None


def _ema_vote(closes, fast, slow, vol):
    window = closes[-slow * 3:]
    gap = ema(window, fast) / ema(window, slow) - 1
    return clip(gap / (vol * math.sqrt(slow) * C.TREND_Z_SCALE))


def trend_score(closes, vol):
    """-1 (strong downtrend) .. +1 (strong uptrend). None if not enough data."""
    longest = max(s for _, s in C.EMA_PAIRS)
    if not vol or len(closes) < longest + 1:
        return None
    votes = [_ema_vote(closes, f, s, vol) for f, s in C.EMA_PAIRS]
    if len(closes) > C.MOMENTUM_BARS:
        ret = math.log(closes[-1] / closes[-C.MOMENTUM_BARS - 1])
        votes.append(clip(ret / (vol * math.sqrt(C.MOMENTUM_BARS))))
    return sum(votes) / len(votes)


def regime_score(btc_closes):
    """Market regime from BTC: -1 bear .. +1 bull. None if no data."""
    if not btc_closes:
        return None
    vol = asset_volatility(btc_closes)
    short = trend_score(btc_closes, vol)
    if short is None:
        return None
    fast, slow = C.REGIME_LONG_EMA
    if len(btc_closes) >= slow + 1:
        return (short + _ema_vote(btc_closes, fast, slow, vol)) / 2
    return short


def regime_multiplier(score):
    if score is None:
        return 1.0
    return C.REGIME_MIN_MULT + (1 - C.REGIME_MIN_MULT) * (score + 1) / 2


def compute_targets(bars, held_pairs):
    """
    bars:        dict pair -> list of closes (oldest first)
    held_pairs:  set of pairs currently held

    Returns (targets, diagnostics, regime_mult)
      targets: dict pair -> portfolio weight. Pairs missing from the dict
               should be sold, except those in diagnostics with score None
               (not enough data), which should be left as they are.
    """
    diag, candidates = {}, []
    for pair, closes in bars.items():
        vol = asset_volatility(closes)
        score = trend_score(closes, vol)
        diag[pair] = {"score": score, "vol": vol}
        if score is None:
            continue
        held = pair in held_pairs
        if score > (C.EXIT_SCORE if held else C.ENTRY_SCORE):
            rank_key = score + (C.HOLD_RANK_BONUS if held else 0.0)
            candidates.append((rank_key, pair, score, vol))

    candidates.sort(reverse=True)
    annualise = math.sqrt(C.BARS_PER_YEAR)
    targets = {}
    for _, pair, score, vol in candidates[:C.MAX_POSITIONS]:
        w = (C.TARGET_ANNUAL_VOL / C.MAX_POSITIONS) * score / (vol * annualise)
        targets[pair] = min(w, C.MAX_WEIGHT_PER_ASSET)

    gross = sum(targets.values())
    if gross > C.MAX_GROSS_EXPOSURE:
        targets = {p: w * C.MAX_GROSS_EXPOSURE / gross for p, w in targets.items()}

    r_score = regime_score(bars.get(C.REGIME_PAIR))
    r_mult = regime_multiplier(r_score)
    targets = {p: w * r_mult for p, w in targets.items()}
    for p in targets:
        diag[p]["target"] = targets[p]
    diag["_regime"] = {"score": r_score, "mult": r_mult}
    return targets, diag, r_mult
