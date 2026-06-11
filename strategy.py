"""
strategy.py
Multi-Timeframe BB Mean Reversion + RSI Confluence

Entry/exit signals are separate boolean columns — no collision possible.

Entry logic (fires on BASE_TF bars)
-------------------------------------
LONG  entry: close <= bb_lower  AND  rsi < RSI_LONG_ENTRY
             AND  trend1_bias == 1   (1h EMA bullish)
             AND  trend2_bias == 1   (4h EMA bullish)

SHORT entry: close >= bb_upper  AND  rsi > RSI_SHORT_ENTRY
             AND  trend1_bias == -1  (1h EMA bearish)
             AND  trend2_bias == -1  (4h EMA bearish)

Exit logic
----------
LONG  exit: close >= bb_mid  OR  rsi > RSI_LONG_EXIT   OR  trend1_bias turns -1
SHORT exit: close <= bb_mid  OR  rsi < RSI_SHORT_EXIT  OR  trend1_bias turns +1

Only one position open at a time.
"""

import pandas as pd

import config


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    bullish = (df["trend1_bias"] == 1) & (df["trend2_bias"] == 1)
    bearish = (df["trend1_bias"] == -1) & (df["trend2_bias"] == -1)

    df["long_entry"]  = (df["close"] <= df["bb_lower"]) & (df["rsi"] < config.RSI_LONG_ENTRY)  & bullish
    df["long_exit"]   = (df["close"] >= df["bb_mid"])   | (df["rsi"] > config.RSI_LONG_EXIT)   | (df["trend1_bias"] == -1)
    df["short_entry"] = (df["close"] >= df["bb_upper"]) & (df["rsi"] > config.RSI_SHORT_ENTRY) & bearish
    df["short_exit"]  = (df["close"] <= df["bb_mid"])   | (df["rsi"] < config.RSI_SHORT_EXIT)  | (df["trend1_bias"] == 1)

    return df
