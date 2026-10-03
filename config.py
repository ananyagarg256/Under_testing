"""
All bot settings in one place.

Change values here, run the backtest, then commit with a message explaining
WHY you changed them. Times are in 15-minute bars unless stated otherwise
(4 bars = 1 hour, 96 bars = 1 day).
"""

# ----------------------------- Universe -----------------------------
CORE_PAIRS = ["BTC/USD", "ETH/USD"]          # always traded
UNIVERSE_SIZE = 10                           # total coins to trade (most liquid on Roostoo)
EXCLUDE_COINS = {                            # stablecoins / wrapped / gold: no trend to trade
    "USDT", "USDC", "FDUSD", "DAI", "TUSD", "BUSD", "USD1", "PYUSD", "USDE",
    "USDP", "EUR", "EURI", "PAXG", "XAUT", "WBTC", "WETH", "STETH", "WBETH",
}
MIN_WARMUP_BARS = 400                        # a coin needs this much history to be traded

# ------------------------------ Timing ------------------------------
POLL_SECONDS = 60                            # read prices once a minute (no HFT)
BAR_MINUTES = 15
BINANCE_INTERVAL = "15m"                     # must match BAR_MINUTES
MAX_BARS = 1000                              # ~10 days of 15-min history kept
BARS_PER_DAY = 24 * 60 // BAR_MINUTES
BARS_PER_YEAR = 365 * BARS_PER_DAY

# ----------------------------- Signals ------------------------------
# Trend measured at three speeds: (fast EMA, slow EMA) in bars
EMA_PAIRS = [(16, 64), (32, 128), (64, 256)]   # 4h/16h, 8h/32h, 16h/64h
MOMENTUM_BARS = 192                          # 48h return
TREND_Z_SCALE = 0.5                          # how strong a trend must be for a full vote
VOL_SHORT_BARS = 32                          # 8h volatility
VOL_LONG_BARS = 192                          # 2-day volatility (bot uses the larger)

ENTRY_SCORE = 0.25                           # score needed to open a position (score is -1..1)
EXIT_SCORE = 0.0                             # held positions are kept until score drops below this
HOLD_RANK_BONUS = 0.10                       # held coins get a small ranking bonus (less churn)
MAX_POSITIONS = 4                            # hold at most the 4 strongest trends

# Market regime from BTC (the whole crypto market follows BTC)
REGIME_PAIR = "BTC/USD"
REGIME_LONG_EMA = (96, 384)                  # 1-day vs 4-day trend
REGIME_MIN_MULT = 0.35                       # exposure multiplier in a full bear regime (1.0 in bull)

# ------------------------------ Sizing ------------------------------
TARGET_ANNUAL_VOL = 0.50                     # portfolio risk budget (BTC alone is ~45-60%)
MAX_WEIGHT_PER_ASSET = 0.30                  # never more than 30% in one coin
MAX_GROSS_EXPOSURE = 0.90                    # never more than 90% invested

# ------------------------------- Risk -------------------------------
TRAIL_STOP_DAILY_SIGMAS = 2.0                # trailing stop = 2 x the coin's daily volatility
TRAIL_STOP_MIN = 0.025                       # ...but at least 2.5% below the peak
TRAIL_STOP_MAX = 0.12                        # ...and at most 12%
STOP_COOLDOWN_BARS = 8                       # after a stop, wait 2h before re-entering that coin

CRASH_WINDOW_MINUTES = 60                    # if BTC falls...
CRASH_DROP = 0.03                            # ...3% from its high within an hour -> sell everything
CRASH_COOLDOWN_BARS = 8                      # and stay out for 2h

DD_SOFT = 0.03                               # drawdown from peak where we start cutting risk
DD_HARD = 0.10                               # drawdown where risk is at its minimum
DD_MIN_MULT = 0.30                           # minimum exposure multiplier

DAILY_LOSS_LIMIT = 0.03                      # if down 3% since the start of the UTC day...
DAILY_LOSS_MULT = 0.5                        # ...halve exposure for the rest of the day

# ---------------------------- Execution -----------------------------
USE_LIMIT_ORDERS = True                      # try maker orders first (0.05% fee instead of 0.1%)
LIMIT_WAIT_SECONDS = 45                      # wait this long for limit fills, then use market orders
REBALANCE_BAND = 0.05                        # ignore weight changes below 5%
MIN_TRADE_USD = 500                          # ignore trades smaller than this
MAX_ORDERS_PER_HOUR = 40                     # safety cap on request volume
DAILY_FORCE_HOUR_UTC = 20                    # if no trade yet today by 20:00 UTC, rebalance exactly

# ----------------------------- Backtest -----------------------------
BACKTEST_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD",
                  "DOGE/USD", "ADA/USD", "AVAX/USD", "LINK/USD", "SUI/USD"]
BACKTEST_FEE = 0.001                         # assume all market orders (conservative)
