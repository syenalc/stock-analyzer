-- Supabase SQL Editor で実行する

create table if not exists prices (
  ticker text not null,
  date date not null,
  open numeric,
  high numeric,
  low numeric,
  close numeric,
  volume bigint,
  primary key (ticker, date)
);

create table if not exists tweets (
  id bigserial primary key,
  ticker text not null,
  tweet_id text unique,
  posted_at timestamptz,
  text text,
  author text,
  metrics jsonb,
  created_at timestamptz default now()
);
create index if not exists idx_tweets_posted on tweets(posted_at desc);
create index if not exists idx_tweets_ticker on tweets(ticker, posted_at desc);

-- 手動で入力する補助情報（URLメモ、ネット記事の要約、自分のメモ等）
create table if not exists manual_notes (
  id bigserial primary key,
  ticker text not null,
  source text,
  title text,
  content text not null,
  url text,
  added_at timestamptz default now(),
  used_in_analysis boolean default false
);
create index if not exists idx_manual_notes_ticker on manual_notes(ticker, added_at desc);

create table if not exists signals (
  id bigserial primary key,
  ticker text not null,
  generated_at timestamptz not null default now(),
  signal text not null check (signal in ('BUY','SELL','HOLD','WATCH')),
  confidence numeric,
  sentiment_score numeric,
  rationale text,
  model text,
  raw jsonb
);
create index if not exists idx_signals_ticker on signals(ticker, generated_at desc);

create table if not exists backtest_results (
  id bigserial primary key,
  run_at timestamptz default now(),
  ticker text not null,
  period_days int,
  horizon_days int,
  total int,
  correct int,
  accuracy numeric,
  avg_return numeric,
  details jsonb
);
create index if not exists idx_backtest_ticker on backtest_results(ticker, run_at desc);
