from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from db.supabase_client import (
    fetch_signals,
    fetch_prices,
    insert_backtest,
)
from utils.logging_config import get_logger

logger = get_logger(__name__)


def _judge(signal: str, return_pct: float, threshold: float) -> bool:
    s = signal.upper()
    if s == "BUY":
        return return_pct >= threshold
    if s == "SELL":
        return return_pct <= -threshold
    if s in ("HOLD", "WATCH"):
        return abs(return_pct) <= threshold
    return False


def run_backtest(
    ticker: str,
    period_days: int = 90,
    horizon_days: int = 3,
    threshold: float = 0.01,
    persist: bool = True,
) -> dict:
    since = datetime.utcnow() - timedelta(days=period_days)
    signals = fetch_signals(ticker, since=since)
    if not signals:
        logger.warning("no signals for %s in last %d days", ticker, period_days)
        return {"ticker": ticker, "total": 0, "accuracy": None, "avg_return": None, "details": []}

    prices_df = fetch_prices(ticker, since=since - timedelta(days=horizon_days + 7))
    if prices_df.empty:
        logger.warning("no prices for %s", ticker)
        return {"ticker": ticker, "total": 0, "accuracy": None, "avg_return": None, "details": []}

    prices_df = prices_df.sort_values("date").reset_index(drop=True)
    prices_df["date_only"] = prices_df["date"].dt.date

    details = []
    correct = 0
    total = 0
    returns = []

    for sig in signals:
        sig_date = pd.to_datetime(sig["generated_at"]).date()
        future_cutoff = sig_date + timedelta(days=horizon_days + 5)

        baseline = prices_df[prices_df["date_only"] >= sig_date].head(1)
        future = prices_df[
            (prices_df["date_only"] > sig_date) & (prices_df["date_only"] <= future_cutoff)
        ].head(horizon_days)

        if baseline.empty or len(future) < horizon_days:
            continue

        base_close = float(baseline.iloc[0]["close"])
        future_close = float(future.iloc[-1]["close"])
        return_pct = (future_close - base_close) / base_close

        is_correct = _judge(sig["signal"], return_pct, threshold)
        total += 1
        if is_correct:
            correct += 1
        returns.append(return_pct)
        details.append({
            "signal_id": sig.get("id"),
            "generated_at": str(sig["generated_at"]),
            "signal": sig["signal"],
            "confidence": sig.get("confidence"),
            "base_close": base_close,
            "future_close": future_close,
            "return_pct": round(return_pct, 4),
            "correct": is_correct,
        })

    accuracy = (correct / total) if total else None
    avg_return = (sum(returns) / len(returns)) if returns else None

    result = {
        "ticker": ticker,
        "period_days": period_days,
        "horizon_days": horizon_days,
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "avg_return": round(avg_return, 4) if avg_return is not None else None,
        "details": details,
    }

    logger.info(
        "backtest %s: total=%d correct=%d accuracy=%s avg_return=%s",
        ticker, total, correct, result["accuracy"], result["avg_return"],
    )

    if persist and total > 0:
        try:
            insert_backtest(result)
        except Exception as e:
            logger.error("failed to persist backtest: %s", e)

    return result
