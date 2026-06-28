import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from agent.chat import run_agent, AVAILABLE_MODELS
from collectors.x_collector import get_display_name, load_user_config
from utils.config import get_secret
from db.supabase_client import (
    fetch_prices,
    fetch_latest_tweets_for_ticker,
    fetch_manual_notes,
    insert_manual_note,
    create_chat_session,
    update_chat_session,
    list_chat_sessions,
    get_chat_session,
    delete_chat_session,
)

st.set_page_config(page_title="Stock AI", page_icon="📈", layout="wide")


def check_password() -> bool:
    """簡易パスワード認証。APP_PASSWORD が未設定なら認証スキップ（ローカル開発）"""
    expected = get_secret("APP_PASSWORD")
    if not expected:
        return True
    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 Stock AI")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン", type="primary"):
        if pw == expected:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False


if not check_password():
    st.stop()

st.title("📈 Stock AI")

TICKERS = [t.strip() for t in (get_secret("TARGET_TICKERS", "SNDU,MUU") or "SNDU,MUU").split(",") if t.strip()]

if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "api_history" not in st.session_state:
    st.session_state.api_history = []

if "selected_model_label" not in st.session_state:
    default_label = next(
        (k for k, v in AVAILABLE_MODELS.items() if v == "claude-sonnet-4-6"),
        list(AVAILABLE_MODELS.keys())[0],
    )
    st.session_state.selected_model_label = default_label

with st.sidebar:
    st.subheader("🧠 AIモデル")
    model_labels = list(AVAILABLE_MODELS.keys())
    st.selectbox(
        "モデル選択",
        model_labels,
        index=model_labels.index(st.session_state.selected_model_label),
        key="selected_model_label",
        label_visibility="collapsed",
    )

    st.divider()
    st.header("ℹ️ 監視中")
    st.caption(f"銘柄: {', '.join(TICKERS)}")
    with st.expander("Xユーザー"):
        for u in load_user_config():
            st.caption(f"・{u['display_name']} (@{u['username']})")

    st.divider()
    st.subheader("💬 会話履歴")
    if st.button("🆕 新しい会話", use_container_width=True):
        st.session_state.current_session_id = None
        st.session_state.chat_history = []
        st.session_state.api_history = []
        st.rerun()

    try:
        sessions = list_chat_sessions(limit=30)
    except Exception as e:
        sessions = []
        st.caption(f"⚠️ 履歴取得失敗: {e}")
    for s in sessions:
        is_active = s["id"] == st.session_state.current_session_id
        col1, col2 = st.columns([5, 1])
        with col1:
            label = ("▶ " if is_active else "") + (s["title"] or "(無題)")[:28]
            if st.button(label, key=f"sess_{s['id']}", use_container_width=True):
                full = get_chat_session(s["id"])
                if full:
                    st.session_state.current_session_id = full["id"]
                    st.session_state.chat_history = full.get("chat_history") or []
                    st.session_state.api_history = full.get("api_history") or []
                    st.rerun()
        with col2:
            if st.button("🗑", key=f"del_{s['id']}", help="削除"):
                delete_chat_session(s["id"])
                if is_active:
                    st.session_state.current_session_id = None
                    st.session_state.chat_history = []
                    st.session_state.api_history = []
                st.rerun()

    st.divider()
    st.subheader("📊 詳細ビュー")
    view_ticker = st.selectbox("銘柄選択", TICKERS)

tab_chat, tab_data, tab_notes = st.tabs(["💬 AIに質問", "📊 データ閲覧", "📝 メモ追加"])

# ============ TAB 1: チャット ============
with tab_chat:
    st.caption("💡 質問例: 「SNDUどう思う?」「@elonmuskの今日のツイート取って」「MUUの株価は？」")

    for msg in st.session_state.chat_history:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["display"])
        elif msg["role"] == "assistant_display":
            with st.chat_message("assistant"):
                st.markdown(msg["display"])
                if msg.get("tools"):
                    st.caption(f"🔧 使用ツール: {', '.join(msg['tools'])}")

    if prompt := st.chat_input("質問を入力..."):
        st.session_state.chat_history.append({"role": "user", "display": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Anthropic用のhistoryを構築
        api_history = st.session_state.get("api_history", [])
        with st.chat_message("assistant"):
            with st.spinner("考え中..."):
                answer, new_api_history, tool_trace = run_agent(
                    prompt,
                    history=api_history,
                    model=AVAILABLE_MODELS[st.session_state.selected_model_label],
                )
            st.markdown(answer)
            if tool_trace:
                st.caption(f"🔧 使用ツール: {', '.join(tool_trace)}")

        st.session_state.api_history = new_api_history
        st.session_state.chat_history.append({
            "role": "assistant_display",
            "display": answer,
            "tools": tool_trace,
        })

        try:
            if st.session_state.current_session_id is None:
                session = create_chat_session(
                    title=prompt[:80],
                    chat_history=st.session_state.chat_history,
                    api_history=st.session_state.api_history,
                )
                st.session_state.current_session_id = session["id"]
            else:
                update_chat_session(
                    st.session_state.current_session_id,
                    st.session_state.chat_history,
                    st.session_state.api_history,
                )
        except Exception as e:
            st.warning(f"⚠️ 会話保存失敗: {e}")

# ============ TAB 2: データ閲覧 ============
with tab_data:
    st.subheader(f"💹 {view_ticker} 株価")
    days = st.slider("表示期間（日）", 7, 365, 60, key="price_days")
    since = datetime.utcnow() - timedelta(days=days)
    df = fetch_prices(view_ticker, since=since)
    if df.empty:
        st.info("価格データなし。チャットで「{view_ticker}の株価取って」と頼んでください")
    else:
        fig = go.Figure(data=[go.Candlestick(
            x=df["date"],
            open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        )])
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=20, b=0), xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

    st.subheader(f"💬 {view_ticker} 関連の保存済みツイート")
    tweets = fetch_latest_tweets_for_ticker(view_ticker, limit=30)
    if not tweets:
        st.info("ツイート未取得。チャットで「@xxxのツイート取って」と頼むか、対象銘柄が紐づくユーザーの発言を取得してください")
    else:
        for t in tweets:
            author = t["author"]
            display = get_display_name(author)
            with st.expander(f"{display} (@{author}) — {(t.get('posted_at') or '')[:16]}"):
                st.write(t["text"])
                metrics = t.get("metrics") or {}
                if metrics:
                    st.caption(f"♥{metrics.get('like_count', 0)} 🔁{metrics.get('retweet_count', 0)} 💬{metrics.get('reply_count', 0)}")

    st.subheader(f"📝 {view_ticker} 手動メモ")
    notes = fetch_manual_notes(view_ticker, limit=10)
    if notes:
        for n in notes:
            with st.expander(f"{n.get('title') or '(無題)'} — {n.get('added_at', '')[:16]}"):
                if n.get("url"):
                    st.markdown(f"🔗 [{n['url']}]({n['url']})")
                st.write(n["content"])

# ============ TAB 3: メモ追加 ============
with tab_notes:
    st.subheader("📝 手動メモを追加")
    st.caption("ネット記事の要約や自分の調査メモを保存。チャットで参照される。")
    with st.form("note_form", clear_on_submit=True):
        note_ticker = st.selectbox("対象銘柄", TICKERS, key="note_ticker")
        note_title = st.text_input("タイトル（任意）")
        note_url = st.text_input("URL（任意）")
        note_source = st.selectbox("ソース種別", ["article", "blog", "report", "memo", "other"])
        note_content = st.text_area("内容（必須）", height=200)
        if st.form_submit_button("💾 保存", type="primary"):
            if not note_content.strip():
                st.error("内容は必須です")
            else:
                try:
                    insert_manual_note(
                        ticker=note_ticker,
                        content=note_content.strip(),
                        title=note_title.strip(),
                        url=note_url.strip(),
                        source=note_source,
                    )
                    st.success(f"{note_ticker} にメモを追加しました")
                except Exception as e:
                    st.error(f"保存失敗: {e}")
