"""
設定値の抽象化レイヤー
- Streamlit Cloud では st.secrets から取得
- ローカル環境では os.getenv (.env) から取得
両方の環境でコードを変えずに動くようにする。
"""
import os


def get_secret(key: str, default: str | None = None) -> str | None:
    # Streamlit 実行時のみ st.secrets を試す
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)
