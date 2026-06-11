"""
indicators.py
Computes indicators for each timeframe, then merges trend bias columns
into the BASE_TF DataFrame via forward-fill alignment.

BASE_TF columns added:   bb_upper, bb_mid, bb_lower, rsi
TREND_TF1 columns added: trend1_ema, trend1_bias  (1 = bullish, -1 = bearish)
TREND_TF2 columns added: trend2_ema, trend2_bias
"""

import pandas as pd

import config


# ── Primitives ────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d    = s.diff()
    gain = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    # Replace zero loss with NaN to avoid division by zero → RSI stays NaN
    # (those rows will be dropped by dropna in build())
    rs = gain / loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def _bollinger(s: pd.Series, n: int, k: float):
    mid = s.rolling(n).mean()
    std = s.rolling(n).std(ddof=0)
    return mid + k * std, mid, mid - k * std


# ── Per-timeframe computation ─────────────────────────────────────────────────

def _add_base_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = _bollinger(
        df["close"], config.BB_PERIOD, config.BB_STD
    )
    df["rsi"] = _rsi(df["close"], config.RSI_PERIOD)
    return df


def _add_trend_indicators(df: pd.DataFrame, ema_period: int, prefix: str) -> pd.DataFrame:
    df = df.copy()
    df[f"{prefix}_ema"]  = _ema(df["close"], ema_period)
    df[f"{prefix}_bias"] = (df["close"] > df[f"{prefix}_ema"]).map({True: 1, False: -1})
    return df


# ── Main entry point ──────────────────────────────────────────────────────────

def build(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Parameters
    ----------
    frames : dict returned by data_fetcher.fetch_all_timeframes()
             keys = timeframe strings (BASE_TF, TREND_TF1, TREND_TF2)

    Returns
    -------
    A single DataFrame at BASE_TF resolution with all indicator columns.
    NaN rows (warm-up period and any zero-loss RSI bars) are dropped.

    Look-ahead prevention: higher-TF trend columns are shifted by 1 bar on
    their own timeframe before joining, so 15m bars inside a candle only
    see the PREVIOUS closed candle's bias — matching live behaviour.
    """
    base  = _add_base_indicators(frames[config.BASE_TF])
    tf1   = _add_trend_indicators(frames[config.TREND_TF1], config.EMA_TREND1, "trend1")
    tf2   = _add_trend_indicators(frames[config.TREND_TF2], config.EMA_TREND2, "trend2")

    # Shift trend columns by 1 bar on their own timeframe to prevent look-ahead
    trend_cols1 = ["trend1_ema", "trend1_bias"]
    trend_cols2 = ["trend2_ema", "trend2_bias"]
    tf1[trend_cols1] = tf1[trend_cols1].shift(1)
    tf2[trend_cols2] = tf2[trend_cols2].shift(1)

    merged = base.join(tf1[trend_cols1], how="left").join(tf2[trend_cols2], how="left")
    merged[trend_cols1 + trend_cols2] = merged[trend_cols1 + trend_cols2].ffill()

    merged.dropna(inplace=True)
    return merged
