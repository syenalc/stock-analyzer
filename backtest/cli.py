import argparse
import json
import os

from dotenv import load_dotenv

from backtest.engine import run_backtest
from utils.logging_config import setup_logging


def main():
    load_dotenv()
    setup_logging()

    parser = argparse.ArgumentParser(description="Run backtest for signals stored in Supabase")
    parser.add_argument("--tickers", type=str, default=os.getenv("TARGET_TICKERS", "AAPL"),
                        help="カンマ区切りのティッカー (例: AAPL,TSLA)")
    parser.add_argument("--period", type=int, default=90, help="過去何日のシグナルを対象にするか")
    parser.add_argument("--horizon", type=int, default=3, help="シグナル後の評価日数")
    parser.add_argument("--threshold", type=float, default=0.01, help="正解判定の閾値 (例: 0.01 = 1%)")
    parser.add_argument("--no-persist", action="store_true", help="Supabaseに結果を保存しない")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    summary = []
    for ticker in tickers:
        result = run_backtest(
            ticker,
            period_days=args.period,
            horizon_days=args.horizon,
            threshold=args.threshold,
            persist=not args.no_persist,
        )
        summary.append({
            "ticker": ticker,
            "total": result["total"],
            "accuracy": result["accuracy"],
            "avg_return": result["avg_return"],
        })

    print("\n=== Backtest Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
