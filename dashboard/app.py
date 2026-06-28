import base64
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
    insert_manual_note_group,
    fetch_all_manual_notes_grouped,
    update_manual_note_group,
    delete_manual_note_group,
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

tab_chat, tab_data, tab_notes = st.tabs(["💬 AIに質問", "📊 データ閲覧", "📝 メモ管理"])

# ============ TAB 1: チャット ============
st.markdown(
    """
<style>
#scroll-to-bottom-btn {
  position: fixed;
  bottom: 110px;
  right: 18px;
  z-index: 9999;
  background: #10b981;
  color: white;
  border: none;
  border-radius: 50%;
  width: 44px;
  height: 44px;
  font-size: 22px;
  line-height: 44px;
  text-align: center;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  opacity: 0.85;
  padding: 0;
}
#scroll-to-bottom-btn:hover { opacity: 1; }
</style>
<button id="scroll-to-bottom-btn"
        title="一番下へ"
        onclick="(window.parent || window).scrollTo({top: 1e9, behavior: 'smooth'});
                 var d=(window.parent || window).document;
                 var main=d.querySelector('section.main') || d.querySelector('[data-testid=\\'stAppViewContainer\\']');
                 if(main) main.scrollTo({top: 1e9, behavior: 'smooth'});">↓</button>
""",
    unsafe_allow_html=True,
)

MAX_PREVIEW_LINES = 5


def render_truncated(text: str, key: str) -> None:
    lines = (text or "").split("\n")
    if len(lines) <= MAX_PREVIEW_LINES:
        st.markdown(text)
        return
    st.markdown("\n".join(lines[:MAX_PREVIEW_LINES]))
    with st.expander(f"📖 全部表示（あと{len(lines) - MAX_PREVIEW_LINES}行）"):
        st.markdown("\n".join(lines[MAX_PREVIEW_LINES:]))


with tab_chat:
    st.caption("💡 質問例: 「SNDUどう思う?」「@elonmuskの今日のツイート取って」「MUUの株価は？」")

    for idx, msg in enumerate(st.session_state.chat_history):
        if msg["role"] == "user":
            with st.chat_message("user"):
                for img in msg.get("images") or []:
                    try:
                        st.image(base64.b64decode(img["data"]))
                    except Exception:
                        st.caption("[画像表示失敗]")
                render_truncated(msg["display"], key=f"u_{idx}")
        elif msg["role"] == "assistant_display":
            with st.chat_message("assistant"):
                render_truncated(msg["display"], key=f"a_{idx}")
                if msg.get("tools"):
                    st.caption(f"🔧 使用ツール: {', '.join(msg['tools'])}")

    chat_value = st.chat_input(
        "質問を入力...",
        accept_file="multiple",
        file_type=["png", "jpg", "jpeg", "webp", "gif"],
    )
    if chat_value:
        prompt = (chat_value.text or "").strip() if hasattr(chat_value, "text") else str(chat_value)
        raw_files = getattr(chat_value, "files", None) or []

        images = []
        for f in raw_files:
            try:
                data_b64 = base64.b64encode(f.getvalue()).decode("ascii")
                mime = f.type or "image/png"
                if not mime.startswith("image/"):
                    continue
                images.append({"media_type": mime, "data": data_b64})
            except Exception as e:
                st.warning(f"画像読み込み失敗: {e}")

        if not prompt and images:
            prompt = "添付した画像を分析してください"

        if not prompt and not images:
            st.stop()

        st.session_state.chat_history.append({
            "role": "user",
            "display": prompt,
            "images": images,
        })
        with st.chat_message("user"):
            for img in images:
                st.image(base64.b64decode(img["data"]))
            render_truncated(prompt, key="u_new")

        api_history = st.session_state.get("api_history", [])
        with st.chat_message("assistant"):
            with st.spinner("考え中..."):
                answer, new_api_history, tool_trace = run_agent(
                    prompt,
                    history=api_history,
                    model=AVAILABLE_MODELS[st.session_state.selected_model_label],
                    images=images,
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

# ============ TAB 3: メモ管理 ============
SOURCE_OPTIONS = ["article", "blog", "report", "memo", "other"]

with tab_notes:
    st.subheader("📚 共通ナレッジ一覧")
    st.caption("全銘柄共通の知識として保存・参照されます")
    try:
        groups = fetch_all_manual_notes_grouped(limit=200)
    except Exception as e:
        groups = []
        st.error(f"メモ取得失敗: {e}")

    if not groups:
        st.info("該当メモなし。下のフォームから追加してください。")
    else:
        st.caption(f"{len(groups)} 件")
        for g in groups:
            gid = g["group_id"]
            label = f"{g['title'] or '(無題)'} — {(g['added_at'] or '')[:16]}"
            with st.expander(label):
                edit_key = f"edit_{gid}"
                if st.session_state.get(edit_key, False):
                    new_title = st.text_input("タイトル", value=g["title"] or "", key=f"eti_{gid}")
                    new_url = st.text_input("URL", value=g["url"] or "", key=f"eu_{gid}")
                    src_idx = SOURCE_OPTIONS.index(g["source"]) if g["source"] in SOURCE_OPTIONS else 3
                    new_source = st.selectbox(
                        "ソース", SOURCE_OPTIONS, index=src_idx, key=f"es_{gid}"
                    )
                    new_content = st.text_area("内容", value=g["content"] or "", height=200, key=f"ec_{gid}")
                    c1, c2 = st.columns(2)
                    if c1.button("💾 保存", key=f"sv_{gid}", type="primary"):
                        if not new_content.strip():
                            st.error("内容は必須です")
                        else:
                            try:
                                update_manual_note_group(
                                    gid, TICKERS, new_content.strip(),
                                    new_title.strip(), new_source, new_url.strip(),
                                )
                                st.session_state[edit_key] = False
                                st.success("更新しました")
                                st.rerun()
                            except Exception as e:
                                st.error(f"更新失敗: {e}")
                    if c2.button("キャンセル", key=f"cn_{gid}"):
                        st.session_state[edit_key] = False
                        st.rerun()
                else:
                    if g["url"]:
                        st.markdown(f"🔗 [{g['url']}]({g['url']})")
                    st.caption(f"ソース: {g['source']}")
                    st.write(g["content"])
                    c1, c2 = st.columns(2)
                    if c1.button("✏️ 編集", key=f"ed_{gid}"):
                        st.session_state[edit_key] = True
                        st.rerun()
                    if c2.button("🗑 削除", key=f"dl_{gid}"):
                        try:
                            delete_manual_note_group(gid)
                            st.rerun()
                        except Exception as e:
                            st.error(f"削除失敗: {e}")

    st.divider()
    st.subheader("➕ 新規ナレッジを追加")
    st.caption("ネット記事の要約・投資方針・業界知識など。全銘柄共通として保存され、チャットで自動参照されます。")
    with st.form("note_form", clear_on_submit=True):
        note_title = st.text_input("タイトル（任意）")
        note_url = st.text_input("URL（任意）")
        note_source = st.selectbox("ソース種別", SOURCE_OPTIONS)
        note_content = st.text_area("内容（必須）", height=200)
        if st.form_submit_button("💾 保存", type="primary"):
            if not note_content.strip():
                st.error("内容は必須です")
            else:
                try:
                    insert_manual_note_group(
                        tickers=TICKERS,
                        content=note_content.strip(),
                        title=note_title.strip(),
                        url=note_url.strip(),
                        source=note_source,
                    )
                    st.success("ナレッジを追加しました")
                except Exception as e:
                    st.error(f"保存失敗: {e}")
