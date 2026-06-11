"""
data_fetcher.py
Paginates OHLCV data from a ccxt exchange for the base timeframe.
Higher timeframes are derived by resampling the base TF data — fewer API
calls and guaranteed alignment.

Raw data is cached to local parquet files (data/{symbol}_{tf}.parquet).
Subsequent runs only fetch missing bars after the last cached timestamp.

Returns standard DataFrames: DatetimeIndex (UTC), columns open/high/low/close/volume.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import ccxt

import config

_MAX_PAGES = 500
_CACHE_DIR = Path("data")


def _since_ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def _cache_path(symbol: str, tf: str) -> Path:
    safe_name = symbol.replace("/", "_")
    _CACHE_DIR.mkdir(exist_ok=True)
    return _CACHE_DIR / f"{safe_name}_{tf}.parquet"


def _resample_ohlcv(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample a base-TF DataFrame to a higher timeframe using proper OHLCV agg."""
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    return df.resample(tf).agg(agg).dropna()


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_date: str = config.START_DATE,
    exchange_id: str = config.EXCHANGE,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars with local parquet caching.
    Only downloads bars after the last cached timestamp.
    """
    cache = _cache_path(symbol, timeframe)

    # Load cached data if available
    cached_df = None
    if cache.exists():
        try:
            cached_df = pd.read_parquet(cache)
        except Exception:
            cached_df = None

    # Determine what we need to fetch
    if cached_df is not None and not cached_df.empty:
        # Start fetching from just after the last cached bar
        fetch_start = cached_df.index[-1]
        fetch_start_ms = int(fetch_start.timestamp() * 1000) + 1
    else:
        fetch_start_ms = _since_ms(start_date)

    # Check if we even need to fetch
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if fetch_start_ms >= now_ms and cached_df is not None:
        return cached_df

    # Fetch missing bars
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})

    all_bars = []
    since = fetch_start_ms

    for _ in range(_MAX_PAGES):
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < 1000:
            break
        since = bars[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000)

    # Build new data
    if all_bars:
        new_df = pd.DataFrame(all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        new_df["timestamp"] = pd.to_datetime(new_df["timestamp"], unit="ms", utc=True)
        new_df.set_index("timestamp", inplace=True)
        new_df = new_df[~new_df.index.duplicated(keep="first")]

        # Merge with cached data
        if cached_df is not None:
            combined = pd.concat([cached_df, new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
        else:
            combined = new_df

        # Save to cache
        try:
            combined.to_parquet(cache)
        except Exception:
            pass

        return combined

    return cached_df if cached_df is not None else pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"]
    )


def fetch_all_timeframes(symbol: str) -> dict[str, pd.DataFrame]:
    """
    Fetch base TF data, then derive higher timeframes via resampling.
    Returns a dict keyed by timeframe string.
    """
    print(f"    Fetching {config.BASE_TF} ...")
    base_df = fetch_ohlcv(symbol, config.BASE_TF)

    result = {config.BASE_TF: base_df}

    for tf in [config.TREND_TF1, config.TREND_TF2]:
        print(f"    Resampling {config.BASE_TF} → {tf} ...")
        result[tf] = _resample_ohlcv(base_df, tf)

    return result
