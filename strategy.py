"""
strategy.py
Multi-Timeframe BB Mean Reversion + RSI Confluence

Entry logic (fires on BASE_TF bars)
-------------------------------------
LONG  (+1): close <= bb_lower  AND  rsi < RSI_LONG_ENTRY
            AND  trend1_bias == 1   (1h EMA-20 bullish)
            AND  trend2_bias == 1   (4h EMA-50 bullish)

SHORT (-2): close >= bb_upper  AND  rsi > RSI_SHORT_ENTRY
            AND  trend1_bias == -1  (1h EMA-20 bearish)
            AND  trend2_bias == -1  (4h EMA-50 bearish)

Exit logic
----------
LONG  exit (-1): close >= bb_mid  OR  rsi > RSI_LONG_EXIT   OR  trend1_bias turns -1
SHORT exit (+2): close <= bb_mid  OR  rsi < RSI_SHORT_EXIT  OR  trend1_bias turns +1

Only one position open at a time.
"""

import pandas as pd

import config


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["signal"] = 0

    bullish = (df["trend1_bias"] == 1) & (df["trend2_bias"] == 1)
    bearish = (df["trend1_bias"] == -1) & (df["trend2_bias"] == -1)

    long_entry  = (df["close"] <= df["bb_lower"]) & (df["rsi"] < config.RSI_LONG_ENTRY)  & bullish
    long_exit   = (df["close"] >= df["bb_mid"])   | (df["rsi"] > config.RSI_LONG_EXIT)   | (df["trend1_bias"] == -1)

    short_entry = (df["close"] >= df["bb_upper"]) & (df["rsi"] > config.RSI_SHORT_ENTRY) & bearish
    short_exit  = (df["close"] <= df["bb_mid"])   | (df["rsi"] < config.RSI_SHORT_EXIT)  | (df["trend1_bias"] == 1)

    df.loc[long_entry,  "signal"] = 1
    df.loc[long_exit,   "signal"] = -1
    df.loc[short_entry, "signal"] = -2
    df.loc[short_exit,  "signal"] = 2

    return df
