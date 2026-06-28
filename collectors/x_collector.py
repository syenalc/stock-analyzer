"""
X (Twitter) 投稿収集モジュール
.env の X_USER_MAP で指定したユーザーのツイートのみ収集する
"""
import os
import json
from datetime import datetime, timedelta, timezone

from db.supabase_client import insert_tweets, get_latest_posted_at_for_author
from utils.config import get_secret
from utils.logging_config import get_logger
from utils.retry import default_retry

logger = get_logger(__name__)

try:
    import tweepy
    TWEEPY_AVAILABLE = True
except ImportError:
    TWEEPY_AVAILABLE = False


CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "x_users.json")


def load_user_config() -> list[dict]:
    """config/x_users.json からユーザー設定を読み込む"""
    if not os.path.exists(CONFIG_PATH):
        logger.error("config/x_users.json が見つかりません: %s", CONFIG_PATH)
        return []
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_user_map() -> dict[str, list[str]]:
    """username → tickers のマッピングを返す"""
    users = load_user_config()
    return {u["username"]: u["tickers"] for u in users}


def get_display_name(username: str) -> str:
    """usernameからアカウント名を返す。見つからなければusernameをそのまま返す"""
    for u in load_user_config():
        if u["username"] == username:
            return u["display_name"]
    return username


def resolve_tickers(user_tickers: list[str], all_tickers: list[str]) -> list[str]:
    if not user_tickers:
        return []
    if "ALL" in [t.upper() for t in user_tickers]:
        return all_tickers
    return user_tickers


def _get_client():
    bearer_token = get_secret("X_BEARER_TOKEN")
    if not bearer_token:
        return None
    return tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)


@default_retry(max_attempts=2)
def _fetch_user(client, username: str):
    return client.get_user(username=username, user_fields=["id", "name"])


@default_retry(max_attempts=2)
def _fetch_user_tweets(client, user_id, start_time: datetime, max_results: int = 100):
    return client.get_users_tweets(
        id=user_id,
        start_time=start_time,
        max_results=max_results,
        tweet_fields=["created_at", "text", "public_metrics"],
        exclude=["retweets", "replies"],
    )


def collect_all_tweets(all_tickers: list[str], persist: bool = True) -> dict:
    """X_USER_MAP に従い、全ユーザーのツイートを収集し銘柄別に紐づける

    戻り値: {ticker: [tweet, ...], ...}
    """
    if not TWEEPY_AVAILABLE:
        logger.error("tweepy未インストール: pip install tweepy")
        return {}

    client = _get_client()
    if client is None:
        logger.error("X_BEARER_TOKEN 未設定です。.env を確認してください")
        return {}

    user_map = load_user_map()
    if not user_map:
        logger.error("X_USER_MAP が未設定です。.env を確認してください")
        return {}

    fetch_days = int(get_secret("X_FETCH_DAYS", "7") or "7")
    # APIの1リクエストあたり上限は100件。多投稿ユーザーの暴発防止に上限を設定
    max_results = min(int(get_secret("X_MAX_RESULTS", "100") or "100"), 100)
    start_time = datetime.now(timezone.utc) - timedelta(days=fetch_days)
    logger.info("fetching tweets since %s (%d days)", start_time.strftime("%Y-%m-%d"), fetch_days)

    by_ticker: dict[str, list[dict]] = {t: [] for t in all_tickers}
    fetched_counts = {}

    for username, raw_tickers in user_map.items():
        try:
            user_resp = _fetch_user(client, username)
            if not user_resp.data:
                logger.warning("X user not found: %s", username)
                continue

            tweets_resp = _fetch_user_tweets(client, user_resp.data.id, start_time, max_results)
            tweets = []
            if tweets_resp.data:
                for tweet in tweets_resp.data:
                    tweets.append({
                        "id": tweet.id,
                        "text": tweet.text,
                        "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                        "metrics": tweet.public_metrics,
                    })

            fetched_counts[username] = len(tweets)
            logger.info("fetched %d tweets from @%s", len(tweets), username)

            target_tickers = resolve_tickers(raw_tickers, all_tickers)
            for ticker in target_tickers:
                by_ticker.setdefault(ticker, []).extend(
                    [{**t, "_author": username} for t in tweets]
                )
                if persist and tweets:
                    insert_tweets(ticker, username, tweets)

        except Exception as e:
            logger.error("X fetch failed for @%s: %s", username, e)

    logger.info("collection summary: %s", fetched_counts)
    return by_ticker


def fetch_user_tweets_oneshot(username: str, days: int = 1, max_total: int = 500, incremental: bool = True) -> dict:
    """指定1ユーザーのツイートを取得しSupabaseに保存（エージェントから呼ばれる）

    tweepy.Paginator で start_time まで遡る。max_total で取得上限を制御。
    incremental=True (デフォルト): DBにある最新ツイートより新しい分だけ取る（API課金節約）。
    incremental=False: 指定 days 全期間を取り直す（過去遡及/バックフィル用）。
    """
    if not TWEEPY_AVAILABLE:
        return {"ok": False, "error": "tweepy未インストール"}
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "X_BEARER_TOKEN未設定"}

    user_config = load_user_config()
    user_entry = next((u for u in user_config if u["username"] == username), None)
    if not user_entry:
        return {"ok": False, "error": f"@{username} は config/x_users.json に未登録です"}

    target_tickers_raw = user_entry["tickers"]
    all_tickers = [t.strip() for t in (get_secret("TARGET_TICKERS", "") or "").split(",") if t.strip()]
    target_tickers = resolve_tickers(target_tickers_raw, all_tickers)

    requested_start = datetime.now(timezone.utc) - timedelta(days=days)
    latest_in_db = None
    if incremental:
        try:
            latest_in_db = get_latest_posted_at_for_author(username)
        except Exception as e:
            logger.warning("latest_posted_at取得失敗 (%s): %s", username, e)

    if latest_in_db and latest_in_db > requested_start:
        start_time = latest_in_db + timedelta(seconds=1)
        skipped_existing = True
    else:
        start_time = requested_start
        skipped_existing = False

    # X API は now-10秒以内の start_time を受け付けない
    if start_time >= datetime.now(timezone.utc) - timedelta(seconds=10):
        return {
            "ok": True,
            "username": username,
            "fetched": 0,
            "message": "DBに既に最新ツイートあり。取得スキップ",
            "incremental": incremental,
            "latest_in_db": latest_in_db.isoformat() if latest_in_db else None,
        }

    try:
        user_resp = _fetch_user(client, username)
        if not user_resp.data:
            return {"ok": False, "error": f"@{username} が見つかりません"}

        tweets = []
        pages = 0
        hit_cap = False
        paginator = tweepy.Paginator(
            client.get_users_tweets,
            id=user_resp.data.id,
            start_time=start_time,
            max_results=100,
            tweet_fields=["created_at", "text", "public_metrics"],
            exclude=["retweets", "replies"],
        )
        for response in paginator:
            pages += 1
            if not response.data:
                continue
            for tweet in response.data:
                tweets.append({
                    "id": tweet.id,
                    "text": tweet.text,
                    "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                    "metrics": tweet.public_metrics,
                })
                if len(tweets) >= max_total:
                    hit_cap = True
                    break
            if hit_cap:
                break

        for ticker in target_tickers:
            if tweets:
                insert_tweets(ticker, username, tweets)

        oldest = min((t["created_at"] for t in tweets if t["created_at"]), default=None)
        newest = max((t["created_at"] for t in tweets if t["created_at"]), default=None)
        return {
            "ok": True,
            "username": username,
            "display_name": user_entry["display_name"],
            "fetched": len(tweets),
            "pages": pages,
            "hit_cap": hit_cap,
            "max_total": max_total,
            "oldest_tweet_at": oldest,
            "newest_tweet_at": newest,
            "linked_tickers": target_tickers,
            "days": days,
            "incremental": incremental,
            "skipped_existing": skipped_existing,
            "start_time_used": start_time.isoformat(),
        }
    except Exception as e:
        logger.exception("oneshot fetch failed for @%s: %s", username, e)
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    from dotenv import load_dotenv
    from utils.logging_config import setup_logging
    load_dotenv()
    setup_logging()
    tickers = [t.strip() for t in os.getenv("TARGET_TICKERS", "AAPL,TSLA").split(",")]
    result = collect_all_tweets(tickers)
    for ticker, tweets in result.items():
        print(f"{ticker}: {len(tweets)} tweets")
