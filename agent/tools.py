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
    fetch_all_manual_notes_grouped,
    insert_manual_note_group,
    get_client,
)
from utils.config import get_secret
from utils.logging_config import get_logger

logger = get_logger(__name__)


# ============ ツール定義 (Claude API tool_use 用) ============

TOOL_DEFINITIONS = [
    {
        "name": "fetch_user_tweets",
        "description": (
            "指定したXユーザーのツイートを取得してSupabaseに保存する（X API課金: 約$0.005/件）。"
            "ユーザーが「〜のツイートを取って」「〜の今日の発言を取得」と明示的に指示した時のみ使う。"
            "ページング対応で start_time まで自動で遡るが、max_total に達したら打ち切る。"
            "デフォルトは増分取得（incremental=true）でDBの最新ツイート以降のみ取得し課金節約。"
            "ユーザーが「過去◯日分を全部取り直して」「5月以降を遡って」など"
            "バックフィル/再取得を要求した場合は incremental=false を指定。"
            "戻り値の hit_cap=true なら上限到達、skipped_existing=true なら既存分はスキップした。"
            "oldest_tweet_at を確認して要求期間まで遡れたか必ず検証すること。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Xユーザー名（@抜き）"},
                "days": {"type": "integer", "description": "過去何日分まで遡るか", "default": 1},
                "max_total": {
                    "type": "integer",
                    "description": "最大取得件数の上限（コスト制御）。長期間取得時は明示的に増やす（30日=300, 90日=1000）",
                    "default": 500,
                },
                "incremental": {
                    "type": "boolean",
                    "description": "true: DBの最新以降のみ取得（既定・節約）。false: 指定 days 全期間を取り直す",
                    "default": True,
                },
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
        "description": (
            "保存済みの共通ナレッジ（ユーザーの投資方針・業界知識・経済指標解釈・記事要約など）を取得する。"
            "銘柄横断の共通知識として保存されているため、どの質問でも参照すべき。"
            "ユーザーの考え方を踏まえた回答をするため、最初に必ず呼ぶこと。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "save_manual_note",
        "description": (
            "ユーザーが共有したネット記事の要約・投資方針・業界知識などを"
            "共通ナレッジとしてSupabaseに保存する。"
            "「このメモを保存しておいて」「これ覚えておいて」と頼まれた時に使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "保存する内容"},
                "title": {"type": "string", "description": "見出し（任意）"},
                "url": {"type": "string", "description": "URL（任意）"},
                "source": {"type": "string", "description": "出典種別", "default": "memo"},
            },
            "required": ["content"],
        },
    },
]


# ============ ツール実装 ============

def _tool_fetch_user_tweets(username: str, days: int = 1, max_total: int = 500, incremental: bool = True) -> dict:
    return fetch_user_tweets_oneshot(username, days=days, max_total=max_total, incremental=incremental)


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


def _all_tickers() -> list[str]:
    return [t.strip() for t in (get_secret("TARGET_TICKERS", "") or "").split(",") if t.strip()]


def _tool_get_manual_notes(limit: int = 50) -> dict:
    groups = fetch_all_manual_notes_grouped(limit=limit)
    notes = [{
        "title": g.get("title"),
        "content": g.get("content"),
        "url": g.get("url"),
        "source": g.get("source"),
        "added_at": g.get("added_at"),
    } for g in groups]
    return {"count": len(notes), "notes": notes}


def _tool_save_manual_note(content: str, title: str = "", url: str = "", source: str = "memo") -> dict:
    tickers = _all_tickers()
    if not tickers:
        return {"ok": False, "error": "TARGET_TICKERS未設定"}
    gid = insert_manual_note_group(tickers, content, title=title, url=url, source=source)
    return {"ok": True, "group_id": gid, "title": title, "content_preview": content[:80]}


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
