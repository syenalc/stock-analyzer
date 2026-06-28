"""
Claudeエージェントが呼び出せるツール群。
ユーザーの自然言語指示に応じて Claude がこれらを呼び出す。
"""
import os
import json
from datetime import datetime, timedelta, timezone

from collectors.price_fetcher import fetch_prices
from collectors.x_collector import (
    fetch_user_tweets_oneshot,
    load_user_config,
)
from db.supabase_client import (
    fetch_prices as db_fetch_prices,
    fetch_latest_tweets_for_ticker,
    fetch_manual_notes,
    insert_manual_note,
    get_client,
)
from utils.logging_config import get_logger

logger = get_logger(__name__)


# ============ ツール定義 (Claude API tool_use 用) ============

TOOL_DEFINITIONS = [
    {
        "name": "fetch_user_tweets",
        "description": (
            "指定したXユーザーのツイートを取得してSupabaseに保存する。"
            "ユーザーが「〜のツイートを取って」「〜の今日の発言を取得」と明示的に指示した時のみ使う。"
            "ティッカー紐付けは config/x_users.json の設定に従う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Xユーザー名（@抜き）"},
                "days": {"type": "integer", "description": "過去何日分を取得するか", "default": 1},
            },
            "required": ["username"],
        },
    },
    {
        "name": "list_known_users",
        "description": "config/x_users.json に登録されているXユーザー一覧と表示名・対象銘柄を返す。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_tweets",
        "description": (
            "Supabaseに保存済みのツイートを検索する。"
            "ticker・author・days で絞り込み可能。回答する前に必ず呼んで根拠を集めること。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "銘柄ティッカーで絞る (例: SNDU)"},
                "author": {"type": "string", "description": "Xユーザー名で絞る (例: elonmusk)"},
                "days": {"type": "integer", "description": "過去何日分", "default": 7},
                "limit": {"type": "integer", "description": "取得上限", "default": 50},
            },
        },
    },
    {
        "name": "get_stock_price",
        "description": (
            "指定銘柄の株価を取得してSupabaseに保存し、最新価格と直近の値動きを返す。"
            "ユーザーが株価を知りたい時、または値動き分析が必要な時に呼ぶ。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "銘柄ティッカー"},
                "period": {"type": "string", "description": "期間 (1mo, 3mo, 6mo, 1y)", "default": "1mo"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_price_history",
        "description": "Supabaseから既に保存済みの株価履歴を取得（API課金なし）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_manual_notes",
        "description": "ある銘柄について手動で保存した補助メモを取得する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "save_manual_note",
        "description": (
            "ユーザーが共有したネット記事の要約・自分のメモ等をSupabaseに保存する。"
            "「このメモを保存しておいて」と頼まれた時に使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "対象銘柄"},
                "content": {"type": "string", "description": "保存する内容"},
                "title": {"type": "string", "description": "見出し（任意）"},
                "url": {"type": "string", "description": "URL（任意）"},
                "source": {"type": "string", "description": "出典種別", "default": "memo"},
            },
            "required": ["ticker", "content"],
        },
    },
]


# ============ ツール実装 ============

def _tool_fetch_user_tweets(username: str, days: int = 1) -> dict:
    result = fetch_user_tweets_oneshot(username, days=days)
    return {"ok": True, **result}


def _tool_list_known_users() -> dict:
    users = load_user_config()
    return {"users": users}


def _tool_query_tweets(ticker: str = None, author: str = None, days: int = 7, limit: int = 50) -> dict:
    client = get_client()
    q = client.table("tweets").select("ticker,author,text,posted_at,metrics")
    if ticker:
        q = q.eq("ticker", ticker.upper())
    if author:
        q = q.eq("author", author.lstrip("@"))
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = q.gte("posted_at", since)
    res = q.order("posted_at", desc=True).limit(limit).execute()
    rows = res.data or []
    # 重複ツイート（複数銘柄に紐づくため）を text+author でユニーク化
    seen = set()
    unique = []
    for r in rows:
        key = (r.get("author"), r.get("text"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return {"count": len(unique), "tweets": unique}


def _tool_get_stock_price(ticker: str, period: str = "1mo") -> dict:
    ticker = ticker.upper()
    result = fetch_prices([ticker], period=period, persist=True)
    if ticker not in result:
        return {"ok": False, "error": f"{ticker} の株価取得に失敗"}
    data = result[ticker]
    return {
        "ok": True,
        "ticker": ticker,
        "current_price": data.get("current_price"),
        "company_name": data.get("company_name"),
        "sector": data.get("sector"),
        "history_count": len(data.get("history", [])),
    }


def _tool_get_price_history(ticker: str, days: int = 30) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    df = db_fetch_prices(ticker.upper(), since=since)
    if df.empty:
        return {"ok": False, "message": "Supabaseに保存済みデータなし。get_stock_price で取得してください"}
    rows = df[["date", "open", "high", "low", "close", "volume"]].to_dict(orient="records")
    # 日付をstr化
    for r in rows:
        r["date"] = str(r["date"])[:10]
    latest = df.iloc[-1]
    first = df.iloc[0]
    change_pct = float((latest["close"] - first["close"]) / first["close"] * 100)
    return {
        "ok": True,
        "ticker": ticker.upper(),
        "rows": rows,
        "summary": {
            "latest_close": float(latest["close"]),
            "period_high": float(df["high"].max()),
            "period_low": float(df["low"].min()),
            "change_pct": round(change_pct, 2),
        },
    }


def _tool_get_manual_notes(ticker: str, limit: int = 10) -> dict:
    notes = fetch_manual_notes(ticker.upper(), limit=limit)
    return {"count": len(notes), "notes": notes}


def _tool_save_manual_note(ticker: str, content: str, title: str = "", url: str = "", source: str = "memo") -> dict:
    note = insert_manual_note(ticker.upper(), content, title=title, url=url, source=source)
    return {"ok": True, "saved": note}


TOOL_HANDLERS = {
    "fetch_user_tweets": _tool_fetch_user_tweets,
    "list_known_users": _tool_list_known_users,
    "query_tweets": _tool_query_tweets,
    "get_stock_price": _tool_get_stock_price,
    "get_price_history": _tool_get_price_history,
    "get_manual_notes": _tool_get_manual_notes,
    "save_manual_note": _tool_save_manual_note,
}


def execute_tool(name: str, arguments: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    try:
        result = handler(**arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.exception("tool %s failed: %s", name, e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
