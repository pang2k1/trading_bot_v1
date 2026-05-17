"""
data_fetcher.py
Paginates OHLCV data from a ccxt exchange for multiple timeframes.
Returns standard DataFrames: DatetimeIndex (UTC), columns open/high/low/close/volume.
"""

import time
from datetime import datetime, timezone

import pandas as pd
import ccxt

import config

_MAX_PAGES = 500   # safety cap — ~500 000 bars per request, far more than needed


def _since_ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_date: str = config.START_DATE,
    exchange_id: str = config.EXCHANGE,
) -> pd.DataFrame:
    """
    Paginate and download all OHLCV bars from start_date to now.

    Parameters
    ----------
    symbol     : e.g. 'BTC/USDT'
    timeframe  : ccxt string: '15m', '1h', '4h', '1d'
    start_date : 'YYYY-MM-DD' UTC
    exchange_id: any ccxt exchange supporting OHLCV
    """
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})

    since = _since_ms(start_date)
    all_bars = []

    for _ in range(_MAX_PAGES):
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < 1000:
            break
        since = bars[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000)

    df = pd.DataFrame(all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="first")]
    return df


def fetch_all_timeframes(symbol: str) -> dict[str, pd.DataFrame]:
    """
    Fetch BASE_TF, TREND_TF1, and TREND_TF2 data for a symbol.
    Returns a dict keyed by timeframe string.
    """
    timeframes = [config.BASE_TF, config.TREND_TF1, config.TREND_TF2]
    result = {}
    for tf in timeframes:
        print(f"    Fetching {tf} ...")
        result[tf] = fetch_ohlcv(symbol, tf)
    return result
