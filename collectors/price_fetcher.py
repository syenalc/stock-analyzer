import os
import json
from datetime import datetime

import yfinance as yf

from db.supabase_client import upsert_prices
from utils.logging_config import get_logger
from utils.retry import default_retry

logger = get_logger(__name__)


@default_retry(max_attempts=3)
def _fetch_one(ticker: str, period: str):
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period)
    if hist is None or hist.empty:
        raise ValueError(f"yfinance returned empty history for {ticker}")
    info = stock.info
    return hist, info


def fetch_prices(tickers: list[str], period: str = "3mo", persist: bool = True) -> dict:
    results = {}
    for ticker in tickers:
        try:
            hist, info = _fetch_one(ticker, period)
            results[ticker] = {
                "history": hist.reset_index().to_dict(orient="records"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "company_name": info.get("longName", ticker),
                "sector": info.get("sector", ""),
                "fetched_at": datetime.utcnow().isoformat(),
            }
            if persist:
                upsert_prices(ticker, hist)
        except Exception as e:
            logger.error("price fetch failed for %s: %s", ticker, e)
    return results


def save_prices_json(results: dict, output_dir: str = "data/prices"):
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    for ticker, data in results.items():
        path = os.path.join(output_dir, f"{ticker}_{date_str}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from utils.logging_config import setup_logging
    load_dotenv()
    setup_logging()
    tickers = os.getenv("TARGET_TICKERS", "AAPL,TSLA,NVDA").split(",")
    fetch_prices(tickers)
