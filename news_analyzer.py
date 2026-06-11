"""
news_analyzer.py
────────────────
Daily crypto news fetcher + sentiment analyzer.

Pulls headlines from free RSS feeds (no API key required), scores sentiment
with VADER, and produces a per-symbol market mood report.

Usage
-----
    python news_analyzer.py              # print today's report
    python news_analyzer.py --symbol ETH # filter to ETH news
    python news_analyzer.py --save       # also write news_report.json

Integration with live_trader
-----------------------------
    from news_analyzer import get_market_sentiment
    sentiment = get_market_sentiment(["BTC/USDT", "ETH/USDT"])
    # Returns e.g. {"BTC/USDT": 0.12, "ETH/USDT": -0.08}
    # Score range: -1.0 (very negative) to +1.0 (very positive)
    # live_trader blocks long entries when score < config.NEWS_FILTER_THRESHOLD

Sources (all free, no API key)
--------------------------------
    CoinDesk, CoinTelegraph, Decrypt, The Block, Bitcoin Magazine
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

NEWS_REPORT_FILE = Path("news_report.json")

# ── RSS feed sources ──────────────────────────────────────────────────────────
RSS_FEEDS = [
    {"name": "CoinDesk",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "CoinTelegraph",    "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt",          "url": "https://decrypt.co/feed"},
    {"name": "The Block",        "url": "https://www.theblock.co/rss.xml"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/"},
]

# ── Keyword map: symbol → terms to search in headlines ───────────────────────
SYMBOL_KEYWORDS = {
    "BTC/USDT": ["bitcoin", "btc", "bitcoin price", "bitcoin market", "crypto market", "cryptocurrency"],
    "ETH/USDT": ["ethereum", "eth", "ether", "defi", "smart contract"],
    "BNB/USDT": ["binance", "bnb", "binance coin"],
    "SOL/USDT": ["solana", "sol"],
    "XRP/USDT": ["xrp", "ripple"],
}

# General crypto terms — any article with these counts for all symbols
GENERAL_KEYWORDS = ["crypto", "cryptocurrency", "digital asset", "blockchain", "altcoin", "market crash", "bull", "bear"]


# ── Fetching ──────────────────────────────────────────────────────────────────

def _fetch_feed(feed: dict, max_age_hours: int = 24) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns articles from last max_age_hours.
    Uses requests with a timeout to avoid blocking the trading loop."""
    try:
        response = requests.get(feed["url"], timeout=10)
        response.raise_for_status()
        parsed   = feedparser.parse(response.content)
        cutoff   = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        articles = []

        for entry in parsed.entries:
            # Parse publish time
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                ts        = calendar.timegm(entry.published_parsed)
                published = datetime.fromtimestamp(ts, tz=timezone.utc)

            if published and published < cutoff:
                continue  # too old

            title   = getattr(entry, "title",   "").strip()
            summary = getattr(entry, "summary", "").strip()

            articles.append({
                "source":    feed["name"],
                "title":     title,
                "summary":   summary[:300],   # cap length
                "published": published.isoformat() if published else None,
                "url":       getattr(entry, "link", ""),
            })

        return articles

    except Exception as exc:
        print(f"  [WARN] Failed to fetch {feed['name']}: {exc}")
        return []


def fetch_all_news(max_age_hours: int = 24) -> list[dict]:
    """Fetch from all RSS sources. Returns combined list of recent articles."""
    all_articles = []
    for feed in RSS_FEEDS:
        articles = _fetch_feed(feed, max_age_hours)
        all_articles.extend(articles)
    return all_articles


# ── Relevance filtering ───────────────────────────────────────────────────────

def _is_relevant(article: dict, keywords: list[str]) -> bool:
    text = (article["title"] + " " + article["summary"]).lower()
    return any(kw in text for kw in keywords)


def filter_by_symbol(articles: list[dict], symbol: str) -> list[dict]:
    """Return articles relevant to the given symbol."""
    keywords = SYMBOL_KEYWORDS.get(symbol, []) + GENERAL_KEYWORDS
    return [a for a in articles if _is_relevant(a, keywords)]


# ── Sentiment scoring ─────────────────────────────────────────────────────────

def score_article(analyzer: SentimentIntensityAnalyzer, article: dict) -> float:
    """Return compound sentiment score (-1 to +1) for a single article."""
    text   = article["title"] + ". " + article["summary"]
    scores = analyzer.polarity_scores(text)
    return scores["compound"]


def score_articles(articles: list[dict]) -> list[dict]:
    """Add a 'sentiment' key to each article dict."""
    analyzer = SentimentIntensityAnalyzer()
    return [{**a, "sentiment": score_article(analyzer, a)} for a in articles]


# ── Aggregation ───────────────────────────────────────────────────────────────

def _mood_label(score: float) -> str:
    if score >=  0.25: return "BULLISH"
    if score >=  0.05: return "SLIGHTLY BULLISH"
    if score >= -0.05: return "NEUTRAL"
    if score >= -0.25: return "SLIGHTLY BEARISH"
    return "BEARISH"


def analyze_symbol(articles: list[dict], symbol: str) -> dict:
    """Compute aggregate sentiment metrics for one symbol."""
    relevant = filter_by_symbol(articles, symbol)
    if not relevant:
        return {
            "symbol":          symbol,
            "num_articles":    0,
            "avg_sentiment":   0.0,
            "mood":            "NEUTRAL",
            "top_headlines":   [],
        }

    scored    = score_articles(relevant)
    avg_score = sum(a["sentiment"] for a in scored) / len(scored)

    # Top 5 headlines sorted by absolute sentiment (most impactful first)
    top = sorted(scored, key=lambda a: abs(a["sentiment"]), reverse=True)[:5]

    return {
        "symbol":        symbol,
        "num_articles":  len(scored),
        "avg_sentiment": round(avg_score, 4),
        "mood":          _mood_label(avg_score),
        "top_headlines": [
            {
                "source":    a["source"],
                "title":     a["title"],
                "sentiment": round(a["sentiment"], 3),
                "published": a["published"],
            }
            for a in top
        ],
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_market_sentiment(symbols: list[str], max_age_hours: int = 24) -> dict[str, float]:
    """
    Fetch news and return compound sentiment score per symbol.

    Returns
    -------
    dict mapping symbol → avg sentiment score (-1.0 to +1.0)
    e.g. {"BTC/USDT": 0.12, "ETH/USDT": -0.08}

    Used by live_trader when config.NEWS_FILTER = True.
    """
    articles = fetch_all_news(max_age_hours)
    return {
        symbol: analyze_symbol(articles, symbol)["avg_sentiment"]
        for symbol in symbols
    }


def run_daily_report(symbols: list[str], save: bool = False) -> dict:
    """
    Full daily report: fetch, analyze, print, optionally save.
    Returns the full report dict.
    """
    print(f"\n{'='*64}")
    print(f"  Crypto News Report  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*64}")
    print("Fetching headlines...")

    articles = fetch_all_news(max_age_hours=24)
    print(f"  {len(articles)} articles fetched from {len(RSS_FEEDS)} sources\n")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_articles": len(articles),
        "symbols": {},
    }

    for symbol in symbols:
        result = analyze_symbol(articles, symbol)
        report["symbols"][symbol] = result

        mood_color = {
            "BULLISH":           "++",
            "SLIGHTLY BULLISH":  "+ ",
            "NEUTRAL":           "  ",
            "SLIGHTLY BEARISH":  "- ",
            "BEARISH":           "--",
        }.get(result["mood"], "  ")

        print(f"[{mood_color}] {symbol:<12}  {result['mood']:<20}  "
              f"score={result['avg_sentiment']:+.3f}  "
              f"articles={result['num_articles']}")

        if result["top_headlines"]:
            print(f"     Top headlines:")
            for h in result["top_headlines"][:3]:
                bar = "+" if h["sentiment"] >= 0 else "-"
                print(f"       [{bar}{abs(h['sentiment']):.2f}] {h['source']}: {h['title'][:70]}")
        print()

    if save:
        with open(NEWS_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"Report saved to {NEWS_REPORT_FILE}")

    return report


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Daily crypto news sentiment report")
    parser.add_argument("--symbol", type=str, default=None,  help="Single symbol e.g. BTC/USDT")
    parser.add_argument("--save",   action="store_true",     help="Save report to news_report.json")
    parser.add_argument("--hours",  type=int, default=24,    help="Look back N hours (default 24)")
    args = parser.parse_args()

    import config
    symbols = [args.symbol] if args.symbol else config.CRYPTO_SYMBOLS

    run_daily_report(symbols=symbols, save=args.save)


if __name__ == "__main__":
    main()
