from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional

import pandas as pd
from supabase import create_client, Client

from utils.config import get_secret
from utils.logging_config import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が .env に未設定です")
    return create_client(url, key)


def upsert_prices(ticker: str, hist: pd.DataFrame) -> int:
    if hist is None or hist.empty:
        return 0
    rows = []
    for idx, row in hist.iterrows():
        date_val = idx.date() if hasattr(idx, "date") else idx
        rows.append({
            "ticker": ticker,
            "date": date_val.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        })
    if not rows:
        return 0
    get_client().table("prices").upsert(rows, on_conflict="ticker,date").execute()
    logger.info("upserted %d price rows for %s", len(rows), ticker)
    return len(rows)


def insert_manual_note(ticker: str, content: str, title: str = "", source: str = "manual", url: str = "") -> dict:
    row = {
        "ticker": ticker,
        "source": source,
        "title": title,
        "content": content,
        "url": url,
    }
    res = get_client().table("manual_notes").insert(row).execute()
    logger.info("inserted manual note for %s (len=%d)", ticker, len(content))
    return res.data[0] if res.data else row


def fetch_manual_notes(ticker: str, since: Optional[datetime] = None, limit: int = 20) -> list[dict]:
    q = get_client().table("manual_notes").select("*").eq("ticker", ticker)
    if since:
        q = q.gte("added_at", since.isoformat())
    res = q.order("added_at", desc=True).limit(limit).execute()
    return res.data or []


def mark_notes_used(note_ids: list[int]) -> None:
    if not note_ids:
        return
    get_client().table("manual_notes").update({"used_in_analysis": True})\
        .in_("id", note_ids).execute()


def insert_tweets(ticker: str, username: str, tweets: list[dict]) -> int:
    if not tweets:
        return 0
    rows = [{
        "ticker": ticker,
        "tweet_id": str(t["id"]),
        "posted_at": t.get("created_at"),
        "text": t.get("text", ""),
        "author": username,
        "metrics": t.get("metrics", {}),
    } for t in tweets]
    get_client().table("tweets").upsert(rows, on_conflict="tweet_id").execute()
    logger.info("upserted %d tweets for %s/%s", len(rows), ticker, username)
    return len(rows)


def insert_signal(ticker: str, signal: dict, model: str = "claude-sonnet-4-6") -> dict:
    row = {
        "ticker": ticker,
        "generated_at": datetime.utcnow().isoformat(),
        "signal": signal.get("signal", "HOLD").upper(),
        "confidence": float(signal.get("confidence", 0.0)),
        "sentiment_score": float(signal.get("sentiment_score", 0.0)),
        "rationale": signal.get("detailed_rationale") or signal.get("rationale", ""),
        "model": model,
        "raw": signal,
        "price_prediction": signal.get("price_prediction_1w"),
        "background": signal.get("background", ""),
        "key_drivers": signal.get("key_drivers"),
        "supporting_tweets": signal.get("supporting_tweets"),
        "risks": signal.get("risks"),
        "detailed_rationale": signal.get("detailed_rationale", ""),
    }
    res = get_client().table("signals").insert(row).execute()
    logger.info("inserted signal: %s %s (conf=%.2f)", ticker, row["signal"], row["confidence"])
    return res.data[0] if res.data else row


def fetch_signals(ticker: str, since: Optional[datetime] = None) -> list[dict]:
    q = get_client().table("signals").select("*").eq("ticker", ticker)
    if since:
        q = q.gte("generated_at", since.isoformat())
    res = q.order("generated_at", desc=False).execute()
    return res.data or []


def fetch_prices(ticker: str, since: Optional[datetime] = None) -> pd.DataFrame:
    q = get_client().table("prices").select("*").eq("ticker", ticker)
    if since:
        q = q.gte("date", since.date().isoformat())
    res = q.order("date", desc=False).execute()
    if not res.data:
        return pd.DataFrame()
    df = pd.DataFrame(res.data)
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_latest_signals(tickers: list[str]) -> dict:
    out = {}
    client = get_client()
    for t in tickers:
        res = client.table("signals").select("*").eq("ticker", t)\
            .order("generated_at", desc=True).limit(1).execute()
        if res.data:
            out[t] = res.data[0]
    return out


def fetch_latest_tweets_for_ticker(ticker: str, limit: int = 20) -> list[dict]:
    res = get_client().table("tweets").select("*").eq("ticker", ticker)\
        .order("posted_at", desc=True).limit(limit).execute()
    return res.data or []


def fetch_latest_tweets(limit: int = 20) -> list[dict]:
    res = get_client().table("tweets").select("*")\
        .order("posted_at", desc=True).limit(limit).execute()
    return res.data or []


def insert_backtest(result: dict) -> dict:
    res = get_client().table("backtest_results").insert(result).execute()
    return res.data[0] if res.data else result


def fetch_backtest_results(ticker: Optional[str] = None, limit: int = 50) -> list[dict]:
    q = get_client().table("backtest_results").select("*")
    if ticker:
        q = q.eq("ticker", ticker)
    res = q.order("run_at", desc=True).limit(limit).execute()
    return res.data or []


def create_chat_session(title: str, chat_history: list, api_history: list) -> dict:
    row = {
        "title": title[:80],
        "chat_history": chat_history,
        "api_history": api_history,
    }
    res = get_client().table("chat_sessions").insert(row).execute()
    return res.data[0] if res.data else row


def update_chat_session(session_id: int, chat_history: list, api_history: list) -> None:
    get_client().table("chat_sessions").update({
        "chat_history": chat_history,
        "api_history": api_history,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", session_id).execute()


def list_chat_sessions(limit: int = 30) -> list[dict]:
    res = get_client().table("chat_sessions").select("id,title,updated_at")\
        .order("updated_at", desc=True).limit(limit).execute()
    return res.data or []


def get_chat_session(session_id: int) -> Optional[dict]:
    res = get_client().table("chat_sessions").select("*").eq("id", session_id).execute()
    return res.data[0] if res.data else None


def delete_chat_session(session_id: int) -> None:
    get_client().table("chat_sessions").delete().eq("id", session_id).execute()
